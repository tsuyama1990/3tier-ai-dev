#!/usr/bin/env python3
"""
DSC Stage 2: Source & CI Miner

Fetches tests/ and examples/ from a package's source repository using a
tiered, provider-agnostic strategy, then scores each file by Trust Score.

Fetch strategy (tried in order, first success wins):
  1. Sparse checkout  — git clone --no-checkout --depth 1 --filter=blob:none
                        then git sparse-checkout set <discovered_dirs>
                        Requires git >= 2.25. Downloads only requested trees.
  2. Full shallow     — git clone --depth 1
                        Fallback when sparse fails (old git, shallow not supported).

Provider-agnostic: uses standard git protocol only, no API tokens required.
GitHub/GitLab/Gitea/self-hosted instances are all treated identically.

Heuristic directory discovery (Challenge #2):
  Ranks directories by weighted indicators to handle non-standard layouts
  (src/pkg/tests/, docs/examples/, scattered test_*.py, etc.).

Trust Score assignment (architecture_design.md §4):
  smoke_tests / ci_tests : 1.0
  examples               : 0.9
  type stubs (*.pyi)     : 0.7  [not extracted, noted for future]
  README (*.md)          : 0.4  [not extracted here]

Usage:
  python3 dsc/source_miner.py --manifest manifest.json --output ~/.knowledge-cache/ase/3.28.0
  python3 dsc/source_miner.py --url https://gitlab.com/ase/ase.git --name ase --version 3.28.0 \\
      --output ~/.knowledge-cache/ase/3.28.0
"""

import sys
import os
import json
import shutil
import argparse
import subprocess
import tempfile
import re
from pathlib import Path
from typing import Optional


from dsc.config import KNOWLEDGE_CACHE
from dsc.utils import load_manifest

# Directories scored for "test" content
_TEST_DIR_NAMES  = {"tests", "test", "testing", "pytests", "specs", "spec"}
_EXAMPLE_DIR_NAMES = {"examples", "example", "demos", "demo", "notebooks",
                      "tutorials", "tutorial", "samples", "sample"}
_SKIP_DIRS = {
    ".git", ".github", ".gitlab", ".venv", "venv", "__pycache__",
    "build", "dist", "node_modules", ".eggs", ".tox", ".mypy_cache",
    ".pytest_cache", "htmlcov", ".hypothesis",
}

# Minimum Trust Score to include a file in the output
TRUST_SCORE_THRESHOLD = 0.9

# ── git version check ─────────────────────────────────────────────────────────

def _git_version() -> tuple:
    """Return git version as (major, minor, patch) integers."""
    try:
        out = subprocess.check_output(
            ["git", "--version"], text=True, stderr=subprocess.DEVNULL
        )
        # e.g. "git version 2.43.0"
        m = re.search(r"(\d+)\.(\d+)\.?(\d*)", out)
        if m:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    except Exception:
        pass
    return (0, 0, 0)


_GIT_VERSION = _git_version()
_SPARSE_CHECKOUT_SUPPORTED = _GIT_VERSION >= (2, 25, 0)


# ── Heuristic directory discovery (Challenge #2) ──────────────────────────────

class _DirScore:
    """Scoring container for a candidate directory."""
    __slots__ = ("path", "score", "category")

    def __init__(self, path: Path, score: float, category: str):
        self.path     = path
        self.score    = score
        self.category = category   # 'test' | 'example'


def _score_directory(d: Path) -> Optional[_DirScore]:
    """
    Assign a relevance score to a directory based on heuristics.

    Returns None if the directory should be skipped.
    Heuristics (additive):
      +3.0  exact name match in _TEST_DIR_NAMES / _EXAMPLE_DIR_NAMES
      +1.5  name contains 'test' or 'example' substring
      +0.5  directory contains test_*.py or *_test.py files
      -5.0  directory is in _SKIP_DIRS (→ None returned)
    """
    name = d.name.lower()
    if name in _SKIP_DIRS or name.startswith("."):
        return None

    score    = 0.0
    category = None

    if name in _TEST_DIR_NAMES:
        score += 3.0
        category = "test"
    elif name in _EXAMPLE_DIR_NAMES:
        score += 3.0
        category = "example"
    elif "test" in name:
        score += 1.5
        category = "test"
    elif "example" in name or "demo" in name or "sample" in name:
        score += 1.5
        category = "example"

    if category is None:
        return None

    # Bonus: directory actually contains Python files
    try:
        py_files = list(d.glob("*.py"))
        if py_files:
            score += 0.5
        # Extra bonus for test-named files inside
        if any(f.name.startswith("test_") or f.name.endswith("_test.py")
               for f in py_files):
            score += 0.5
    except PermissionError:
        pass

    return _DirScore(path=d, score=score, category=category)


def discover_target_dirs(repo_root: Path, max_depth: int = 4, target_name: Optional[str] = None) -> list:
    """
    Walk the repo tree up to `max_depth` levels, scoring directories.

    If target_name is provided, we prioritize directories whose path contains
    the target_name (case-insensitive) to handle monorepos correctly. If no such
    directories are found, we fall back to searching all directories.

    Returns a sorted list of (relative_path_str, category) tuples for
    directories with score > 0, ordered by score descending.
    """
    candidates: list[_DirScore] = []

    def _walk(current: Path, depth: int):
        if depth > max_depth:
            return
        try:
            entries = sorted(current.iterdir())
        except PermissionError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            scored = _score_directory(entry)
            if scored is not None:
                candidates.append(scored)
                _walk(entry, depth + 1)
            elif entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                # Recurse into neutral dirs (e.g. "src/") to find nested tests
                _walk(entry, depth + 1)

    _walk(repo_root, 1)

    # Monorepo filter: if target_name is specified, filter candidates
    if target_name:
        name_lower = target_name.lower()
        filtered = []
        for c in candidates:
            # Check if target_name is in the path components
            rel_parts = [p.lower() for p in c.path.relative_to(repo_root).parts]
            if any(name_lower in p for p in rel_parts):
                filtered.append(c)
        if filtered:
            candidates = filtered

    candidates.sort(key=lambda c: c.score, reverse=True)

    # Deduplicate: drop a path if an ancestor is already included
    result = []
    included_paths: set[Path] = set()
    for c in candidates:
        rel = c.path.relative_to(repo_root)
        # Skip if any parent is already collected
        dominated = any(
            str(rel).startswith(str(inc) + os.sep) for inc in included_paths
        )
        if not dominated:
            included_paths.add(rel)
            result.append((str(rel), c.category))
    return result


# ── Fetch strategies ──────────────────────────────────────────────────────────

def _run(cmd: list, cwd: Optional[Path] = None, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a command, capturing output. Raises on non-zero exit."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _list_tree_dirs(tmpdir: Path) -> list[str]:
    """
    List all directories present in the repo tree via git ls-tree.

    Used during Phase A of sparse checkout (before any files are downloaded)
    to discover the actual directory layout without relying on guessed names.
    Returns relative path strings for directories only.
    """
    res = _run(
        ["git", "ls-tree", "-r", "--name-only", "-d", "HEAD"],
        cwd=tmpdir,
    )
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def _fetch_sparse(url: str, tmpdir: Path) -> bool:
    """
    Strategy 1: Two-phase sparse checkout.

    Phase 1 — clone without file content (tree-only):
        git clone --no-checkout --depth 1 --filter=blob:none <url> <dir>

    Phase 1b — discover actual target directories from the tree:
        git ls-tree -r --name-only -d HEAD  →  find test/example dirs

    Phase 2 — sparse-checkout exactly the discovered dirs:
        git sparse-checkout init --cone
        git sparse-checkout set <discovered_dirs>
        git checkout

    This correctly handles non-standard layouts (e.g. ase/test/, src/pkg/tests/)
    without guessing directory names up front.

    Requires git >= 2.25. Returns True on success.
    """
    if not _SPARSE_CHECKOUT_SUPPORTED:
        return False

    # Phase 1: tree-only clone (no blobs downloaded yet)
    clone_cmd = [
        "git", "clone",
        "--no-checkout",
        "--depth", "1",
        "--filter=blob:none",
        url, str(tmpdir),
    ]
    res = _run(clone_cmd)
    if res.returncode != 0:
        return False

    # Phase 1b: list all directories in the tree
    all_dirs = _list_tree_dirs(tmpdir)

    # Score directories against heuristics using their names only
    # (no filesystem content yet, so bonus scoring is skipped)
    target_dirs = []
    for d in all_dirs:
        parts = Path(d).parts
        # Evaluate the leaf directory name
        leaf = parts[-1].lower() if parts else ""
        if leaf in _TEST_DIR_NAMES:
            target_dirs.append(d)
        elif leaf in _EXAMPLE_DIR_NAMES:
            target_dirs.append(d)
        elif "test" in leaf or "example" in leaf or "demo" in leaf or "sample" in leaf:
            target_dirs.append(d)

    if not target_dirs:
        # Fallback: use the common name set
        target_dirs = list(_TEST_DIR_NAMES | _EXAMPLE_DIR_NAMES)

    # Phase 2: initialize sparse-checkout and fetch only target dirs
    res = _run(["git", "sparse-checkout", "init", "--cone"], cwd=tmpdir)
    if res.returncode != 0:
        return False

    res = _run(["git", "sparse-checkout", "set"] + target_dirs, cwd=tmpdir)
    if res.returncode != 0:
        return False

    res = _run(["git", "checkout"], cwd=tmpdir)
    return res.returncode == 0


def _fetch_shallow(url: str, tmpdir: Path) -> bool:
    """
    Strategy 2: Full shallow clone (fallback).

    git clone --depth 1 <url> <dir>

    Downloads all files at HEAD but no history.
    Returns True on success.
    """
    res = _run(["git", "clone", "--depth", "1", url, str(tmpdir)])
    return res.returncode == 0


def fetch_repository(url: str, tmpdir: Path) -> tuple[bool, str]:
    """
    Fetch the repository into `tmpdir` using the tiered strategy.

    Phase A: Two-phase sparse checkout (tree-scan then targeted blob fetch).
    Phase B: If sparse fails, fall back to full shallow clone.

    Returns (success: bool, strategy_used: str).
    """
    # Phase A: 2-phase sparse checkout
    if _fetch_sparse(url, tmpdir):
        return True, "sparse_checkout"

    # Phase A failed — clean up the partial clone before retrying
    shutil.rmtree(tmpdir, ignore_errors=True)
    tmpdir.mkdir(parents=True, exist_ok=True)

    # Phase B: full shallow clone
    if _fetch_shallow(url, tmpdir):
        return True, "shallow_clone"

    return False, "failed"



# ── Trust Score assignment ────────────────────────────────────────────────────

def assign_trust_score(fpath: Path, category: str) -> float:
    """
    Assign a Trust Score per architecture_design.md §4.

    category 'test'    → 1.0 (absolute truth)
    category 'example' → 0.9 (implementation template)

    Files matching smoke / ci patterns always get 1.0.
    """
    name = fpath.name.lower()
    if name.startswith("smoke_") or name.startswith("test_") or name.endswith("_test.py"):
        return 1.0
    if category == "test":
        return 1.0
    if category == "example":
        return 0.9
    return 0.9  # default for unclassified files in target dirs


# ── Extraction ────────────────────────────────────────────────────────────────

def extract_sources(repo_root: Path, target_dirs: list) -> list[dict]:
    """
    Collect all .py files from `target_dirs`, assigning Trust Scores.

    Returns a list of dicts:
        path      : absolute path in the cloned repo
        rel_path  : path relative to repo_root
        category  : 'test' | 'example'
        trust_score: float
    """
    records = []
    for rel_dir, category in target_dirs:
        src_dir = repo_root / rel_dir
        if not src_dir.exists():
            continue
        for py_file in sorted(src_dir.rglob("*.py")):
            # Skip __pycache__ and hidden dirs
            parts = py_file.relative_to(repo_root).parts
            if any(p.startswith("__pycache__") or p.startswith(".") for p in parts):
                continue
            score = assign_trust_score(py_file, category)
            if score >= TRUST_SCORE_THRESHOLD:
                records.append({
                    "path":        str(py_file),
                    "rel_path":    str(py_file.relative_to(repo_root)),
                    "category":    category,
                    "trust_score": score,
                })
    return records


# ── Output stage: copy to knowledge cache ────────────────────────────────────

def materialize_to_cache(records: list[dict], repo_root: Path, cache_dir: Path) -> list[str]:
    """
    Copy extracted files into the cache, mirroring their relative paths.

    Tests → cache_dir/verified_tests/
    Examples → cache_dir/verified_examples/

    Returns list of written cache paths.
    """
    written = []
    for rec in records:
        src = Path(rec["path"])
        subdir = "verified_tests" if rec["category"] == "test" else "verified_examples"
        dest = cache_dir / subdir / rec["rel_path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        written.append(str(dest.relative_to(cache_dir)))
    return written


# ── Main orchestrator ─────────────────────────────────────────────────────────

def mine(source_url: str, name: str, version: str,
         output_dir: Optional[Path] = None,
         dry_run: bool = False) -> dict:
    """
    Full source mining pipeline for one package.

    1. Create temp dir
    2. Fetch repository (tiered strategy)
    3. Discover target directories (heuristic)
    4. Extract and score .py files
    5. Materialize to ~/.knowledge-cache/{name}/{version}/
    6. Return structured result report
    """
    cache_dir = output_dir or (KNOWLEDGE_CACHE / name.lower() / version)

    report = {
        "name":         name,
        "version":      version,
        "source_url":   source_url,
        "cache_dir":    str(cache_dir),
        "git_version":  ".".join(str(x) for x in _GIT_VERSION),
        "sparse_checkout_supported": _SPARSE_CHECKOUT_SUPPORTED,
        "fetch_strategy": None,
        "discovered_dirs": [],
        "extracted_files": [],
        "written_assets":  [],
        "success": False,
        "error": None,
    }

    with tempfile.TemporaryDirectory(prefix="dsc_miner_") as td:
        tmpdir = Path(td)

        # Step 1: Fetch
        print(f"[Miner] Fetching {source_url} ...", file=sys.stderr)
        success, strategy = fetch_repository(source_url, tmpdir)
        report["fetch_strategy"] = strategy

        if not success:
            report["error"] = f"All fetch strategies failed for {source_url}"
            return report

        print(f"[Miner] Fetch complete (strategy: {strategy})", file=sys.stderr)

        # Step 2: Discover directories
        target_dirs = discover_target_dirs(tmpdir, target_name=name)
        report["discovered_dirs"] = [
            {"rel_path": rd, "category": cat} for rd, cat in target_dirs
        ]
        print(
            f"[Miner] Discovered {len(target_dirs)} target directories: "
            + ", ".join(rd for rd, _ in target_dirs),
            file=sys.stderr,
        )

        if not target_dirs:
            report["error"] = "No test or example directories found in repository"
            return report

        # Step 3: Extract and score files
        records = extract_sources(tmpdir, target_dirs)
        report["extracted_files"] = [
            {k: v for k, v in r.items() if k != "path"}  # omit temp path
            for r in records
        ]
        print(
            f"[Miner] Extracted {len(records)} files "
            f"(trust_score >= {TRUST_SCORE_THRESHOLD})",
            file=sys.stderr,
        )

        # Step 4: Materialize to cache
        if not dry_run and records:
            cache_dir.mkdir(parents=True, exist_ok=True)
            written = materialize_to_cache(records, tmpdir, cache_dir)
            report["written_assets"] = written
            print(
                f"[Miner] Written {len(written)} assets to {cache_dir}",
                file=sys.stderr,
            )
        elif dry_run:
            print("[Miner] Dry-run: skipping cache write", file=sys.stderr)

        report["success"] = True

    return report


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="source_miner",
        description=(
            "DSC Stage 2 — fetch tests/ and examples/ from a package's "
            "source repo and populate ~/.knowledge-cache/{name}/{version}/."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # From a package_inspector manifest
  python3 dsc/source_miner.py --manifest /tmp/ase_manifest.json

  # From explicit URL + identity
  python3 dsc/source_miner.py \\
      --url https://gitlab.com/ase/ase.git \\
      --name ase --version 3.28.0

  # Dry run (inspect without writing)
  python3 dsc/source_miner.py \\
      --url https://gitlab.com/ase/ase.git \\
      --name ase --version 3.28.0 --dry-run
        """,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--manifest",
        metavar="FILE",
        help="JSON manifest produced by package_inspector.py (processes first package)",
    )
    src.add_argument(
        "--url",
        metavar="URL",
        help="Git clone URL of the source repository",
    )
    p.add_argument("--name",    metavar="NAME",    help="Package name (required with --url)")
    p.add_argument("--version", metavar="VERSION", help="Package version (required with --url)")
    p.add_argument(
        "--output",
        metavar="DIR",
        help="Output cache directory (default: ~/.knowledge-cache/NAME/VERSION/)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and extract files but do not write to cache",
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON report",
    )
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.manifest:
        mf = load_manifest(args.manifest)
        pkg = mf["packages"][0]
        url     = pkg.get("github_url") or pkg.get("source_url")
        name    = pkg["name"]
        version = pkg["version"]
        if not url:
            print(f"ERROR: no source URL found in manifest for {name}", file=sys.stderr)
            sys.exit(1)
    else:
        url     = args.url
        name    = args.name
        version = args.version
        if not (name and version):
            print("ERROR: --name and --version are required with --url", file=sys.stderr)
            sys.exit(1)

    output_dir = Path(args.output).expanduser().resolve() if args.output else None

    print(f"[Miner] git {'.'.join(str(x) for x in _GIT_VERSION)} "
          f"| sparse-checkout supported: {_SPARSE_CHECKOUT_SUPPORTED}", file=sys.stderr)

    report = mine(
        source_url=url,
        name=name,
        version=version,
        output_dir=output_dir,
        dry_run=args.dry_run,
    )

    indent = None if args.compact else 2
    print(json.dumps(report, indent=indent, ensure_ascii=False))

    sys.exit(0 if report["success"] else 1)


if __name__ == "__main__":
    main()
