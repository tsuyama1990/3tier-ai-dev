#!/usr/bin/env python3
"""
DSC Stage 3: Smoke Tracer

Post-processes the files written by source_miner into the knowledge cache.
Reduces a large set of mined files to a curated set of high-quality,
verified API demonstrations using three sequential stages:

  Stage 1 — Static Scoring (AST):
    Score every .py file in the cache by API usage signals.
    Reward: target imports, class instantiations, __main__ block.
    Penalize: external hard deps, internal/private modules, subprocess calls,
              pytest infrastructure (fixtures, mocks).

  Stage 2 — Prioritization:
    Keep top-N files by raw_score (default: 30).
    Hard filter: raw_score >= 2.0.

  Stage 3 — Snippet Extraction + Isolated Execution:
    Extract a minimal runnable snippet from each candidate.
    Run it in the project's venv via subprocess with a strict timeout.
    Classify result: CLEAN (1.0) | EXTERNAL_IMPORT_ERROR (0.9) |
                     TIMEOUT (0.7) | RUNTIME_ERROR / TARGET_IMPORT_ERROR (0.0)

  Output:
    Files with final_score >= 0.9 are kept in the cache.
    Losers are removed. smoke_trace_report.json is written.

Usage:
    python3 dsc/smoke_tracer.py --manifest /tmp/ase_manifest.json
    python3 dsc/smoke_tracer.py --manifest /tmp/ase_manifest.json --top-n 50 --dry-run
    python3 dsc/smoke_tracer.py \\
        --cache-dir ~/.knowledge-cache/ase/3.28.0 \\
        --target ase --venv /path/to/project/.venv
"""

import sys
import os
import ast
import json
import shutil
import argparse
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ── Constants ─────────────────────────────────────────────────────────────────

KNOWLEDGE_CACHE = Path.home() / ".knowledge-cache"

# External hard dependencies — make execution impossible in typical local env
_EXTERNAL_HARD_DEPS = frozenset({
    "mpi4py", "lammps", "vasp", "gpaw", "siesta", "abinit",
    "cp2k", "elk", "exciting", "fleur", "nwchem", "psi4",
    "pyscf", "turbomole", "castep", "dftb", "dftbplus",
    "phonopy", "phono3py", "espresso", "wannier90",
})

# Pytest infrastructure attribute names that indicate test plumbing, not API docs
_PYTEST_INFRA_ATTRS = frozenset({
    "fixture", "mark", "skip", "parametrize", "raises",
    "warns", "approx", "xfail",
})

# Subprocess/shell invocation attributes
_SUBPROCESS_ATTRS = frozenset({"run", "call", "Popen", "check_output",
                                "check_call", "system", "popen"})

# Scoring weights (see implementation_plan.md)
_W_TARGET_IMPORT    =  2.0
_W_INSTANTIATION    =  1.5
_W_HAS_MAIN         =  1.0
_W_SWEET_LINES      =  1.0   # 20–150 lines
_W_EXAMPLE_DIR      =  0.5
_W_HARD_DEP         = -3.0
_W_INTERNAL_DEP     = -2.0
_W_SUBPROCESS_CALL  = -1.0
_W_PYTEST_INFRA     = -1.5
_W_MOCK_USAGE       = -1.0

_MAX_STATIC_SCORE   = 10.0   # normalization denominator
_STATIC_THRESHOLD   = 2.0    # Stage 2 hard filter
_SNIPPET_MAX_LINES  = 80
_EXECUTION_TIMEOUT  = 3      # seconds
_FINAL_THRESHOLD    = 0.9    # minimum final_score to keep in cache


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class StaticScore:
    """Result of Stage 1 static AST analysis for one file."""
    file_path: Path
    rel_path: str
    category: str               # 'test' | 'example'
    target_imports: int = 0
    instantiations: int = 0
    has_main: bool = False
    line_count: int = 0
    external_hard_deps: list = field(default_factory=list)
    internal_deps: list = field(default_factory=list)
    subprocess_calls: int = 0
    pytest_infra_signals: int = 0
    mock_usage: int = 0
    raw_score: float = 0.0
    parse_error: Optional[str] = None
    # Q3: fully-qualified API names called in this file → consumed by Asset Synthesizer
    # e.g. ["ase.io.read", "ase.Atoms", "ase.calculators.emt.EMT"]
    api_surface: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["file_path"] = str(self.file_path)
        return d


@dataclass
class RunResult:
    """Result of Stage 3 isolated execution."""
    result_type: str    # CLEAN | EXTERNAL_IMPORT_ERROR | TARGET_IMPORT_ERROR | TIMEOUT | RUNTIME_ERROR
    runtime_score: float
    exit_code: Optional[int] = None
    stderr_snippet: str = ""    # first 300 chars of stderr
    snippet_lines: int = 0


@dataclass
class TraceRecord:
    """Final per-file record written into smoke_trace_report.json."""
    rel_path: str
    category: str
    static_raw_score: float
    runtime_result: str
    runtime_score: float
    final_score: float
    snippet_lines: int
    written: bool
    error: Optional[str] = None
    # Fully-qualified APIs observed in this file (for Asset Synthesizer)
    api_surface: list = field(default_factory=list)


# ── Stage 1: Static Analyzer ──────────────────────────────────────────────────

def _is_internal(module: str, target_pkg: str) -> bool:
    """Return True if module path indicates internal/private target code."""
    parts = module.split(".")
    if any(p.startswith("_") for p in parts[1:]):
        return True
    if len(parts) > 1 and parts[1] == "test":
        return True
    return False


def _dotted_name(node) -> Optional[str]:
    """
    Reconstruct a dotted name from nested ast.Attribute / ast.Name nodes.

    Examples:
      ast.Name('ase')                          → 'ase'
      ast.Attribute(ast.Name('ase'), 'io')     → 'ase.io'
      ast.Attribute(ast.Attribute(...), 'read')→ 'ase.io.read'

    Returns None if the node structure is not a plain attribute chain.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base is not None:
            return f"{base}.{node.attr}"
    return None


def analyze_static(file_path: Path, target_pkg: str,
                   rel_path: str, category: str) -> StaticScore:
    """
    Walk the AST of `file_path` and compute a StaticScore.

    Builds two maps during import analysis:
      - target_name_to_fqn: {local_name → fully_qualified_name}
        e.g. 'read'  → 'ase.io.read'
             'Atoms' → 'ase.Atoms'
             'ase'   → 'ase'
      - target_names: set of local names (for fast membership tests)

    These maps are then used during Call node analysis to resolve
    which target-package APIs are actually invoked (Q3 api_surface).
    """
    score = StaticScore(file_path=file_path, rel_path=rel_path, category=category)

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        score.parse_error = str(exc)
        return score

    score.line_count = len(source.splitlines())

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        score.parse_error = str(exc)
        return score

    # local_name → fully-qualified target-package name
    target_name_to_fqn: dict[str, str] = {}
    api_surface_set: set[str] = set()

    for node in ast.walk(tree):

        # ── Import statements ──────────────────────────────────────────────
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root == target_pkg or alias.name.startswith(target_pkg + "."):
                    score.target_imports += 1
                    local = alias.asname or alias.name.rsplit(".", 1)[-1]
                    # 'import ase.io as io_mod'  → io_mod → 'ase.io'
                    target_name_to_fqn[local] = alias.name
                elif root in _EXTERNAL_HARD_DEPS:
                    if root not in score.external_hard_deps:
                        score.external_hard_deps.append(root)
                elif alias.name.startswith("unittest.mock") or alias.name == "mock":
                    score.mock_usage += 1

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root   = module.split(".")[0] if module else ""

            if root == target_pkg:
                score.target_imports += 1
                if _is_internal(module, target_pkg):
                    if module not in score.internal_deps:
                        score.internal_deps.append(module)
                else:
                    for alias in node.names:
                        local = alias.asname or alias.name
                        # 'from ase.io import read' → read → 'ase.io.read'
                        fqn = f"{module}.{alias.name}" if module else alias.name
                        target_name_to_fqn[local] = fqn
            elif root in _EXTERNAL_HARD_DEPS:
                if root not in score.external_hard_deps:
                    score.external_hard_deps.append(root)
            elif module in ("unittest.mock", "mock") or root == "mock":
                score.mock_usage += 1

        # ── Call nodes ────────────────────────────────────────────────────
        elif isinstance(node, ast.Call):
            func = node.func
            dotted = _dotted_name(func)

            if dotted:
                parts = dotted.split(".")
                root_local = parts[0]

                if root_local in target_name_to_fqn:
                    # Resolve local alias → FQN
                    fqn_root = target_name_to_fqn[root_local]
                    fqn = fqn_root + ("." + ".".join(parts[1:]) if len(parts) > 1 else "")
                    api_surface_set.add(fqn)

                    # Instantiation: capital first letter of the leaf name
                    leaf = parts[-1]
                    if leaf[0].isupper():
                        score.instantiations += 1

            # Subprocess / shell calls (separate from target API tracking)
            if isinstance(func, ast.Attribute):
                val = func.value
                if isinstance(val, ast.Name):
                    if val.id in ("subprocess", "os") and func.attr in _SUBPROCESS_ATTRS:
                        score.subprocess_calls += 1
                    # pytest infrastructure
                    if val.id == "pytest" and func.attr in _PYTEST_INFRA_ATTRS:
                        score.pytest_infra_signals += 1
                elif isinstance(val, ast.Attribute):
                    if (isinstance(val.value, ast.Name) and val.value.id == "pytest"
                            and val.attr in _PYTEST_INFRA_ATTRS):
                        score.pytest_infra_signals += 1

        # ── if __name__ == '__main__': ─────────────────────────────────────
        elif isinstance(node, ast.If):
            t = node.test
            if (isinstance(t, ast.Compare)
                    and isinstance(t.left, ast.Name) and t.left.id == "__name__"
                    and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq)
                    and len(t.comparators) == 1
                    and isinstance(t.comparators[0], ast.Constant)
                    and t.comparators[0].value == "__main__"):
                score.has_main = True

    score.api_surface = sorted(api_surface_set)

    # ── Compute raw score ──────────────────────────────────────────────────
    raw = 0.0
    raw += min(score.target_imports, 3) * _W_TARGET_IMPORT
    raw += min(score.instantiations, 3) * _W_INSTANTIATION
    raw += _W_HAS_MAIN if score.has_main else 0.0
    raw += _W_SWEET_LINES if 20 <= score.line_count <= 150 else 0.0
    raw += _W_EXAMPLE_DIR if category == "example" else 0.0
    raw += len(score.external_hard_deps) * _W_HARD_DEP
    raw += len(score.internal_deps) * _W_INTERNAL_DEP
    raw += score.subprocess_calls * _W_SUBPROCESS_CALL
    raw += min(score.pytest_infra_signals, 3) * _W_PYTEST_INFRA
    raw += min(score.mock_usage, 2) * _W_MOCK_USAGE

    score.raw_score = raw
    return score


# ── Stage 2: Prioritization ───────────────────────────────────────────────────

def prioritize(scores: list[StaticScore], top_n: int = 30) -> list[StaticScore]:
    """
    Filter by static threshold and return top-N by raw_score.
    Files with raw_score < _STATIC_THRESHOLD are unconditionally excluded.
    """
    passed = [s for s in scores if s.raw_score >= _STATIC_THRESHOLD and not s.parse_error]
    return sorted(passed, key=lambda s: s.raw_score, reverse=True)[:top_n]


# ── Stage 3a: Snippet Extraction ──────────────────────────────────────────────

def extract_snippet(source: str, target_pkg: str,
                    max_lines: int = _SNIPPET_MAX_LINES) -> str:
    """
    Extract a minimal runnable snippet from `source`.

    Priority:
      1. `if __name__ == '__main__':` block (most self-contained)
      2. Top-level statements outside function/class defs
      3. First max_lines of file (fallback)

    Import statements from the target package are always prepended.
    The result is validated with ast.parse(); if invalid, the raw
    first max_lines are returned.
    """
    lines = source.splitlines()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "\n".join(lines[:max_lines])

    # Collect import lines (1-indexed) for the target package
    import_line_nums: set[int] = set()
    main_start: Optional[int] = None
    main_end: Optional[int] = None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target_pkg or alias.name.startswith(target_pkg + "."):
                    for ln in range(node.lineno, node.end_lineno + 1):
                        import_line_nums.add(ln)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == target_pkg or module.startswith(target_pkg + "."):
                for ln in range(node.lineno, node.end_lineno + 1):
                    import_line_nums.add(ln)
        elif isinstance(node, ast.If):
            t = node.test
            if (isinstance(t, ast.Compare)
                    and isinstance(t.left, ast.Name) and t.left.id == "__name__"
                    and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq)
                    and len(t.comparators) == 1
                    and isinstance(t.comparators[0], ast.Constant)
                    and t.comparators[0].value == "__main__"):
                main_start = node.lineno
                main_end   = node.end_lineno

    result_lines: list[str] = []

    # Always include target imports first
    for ln in sorted(import_line_nums):
        result_lines.append(lines[ln - 1])
    if result_lines:
        result_lines.append("")

    if main_start is not None:
        # Append the __main__ block (cap at max_lines)
        block = lines[main_start - 1 : main_end]
        remaining = max_lines - len(result_lines)
        result_lines.extend(block[:remaining])
    else:
        # Q2: AST boundary-safe trimming.
        # Walk top-level statements and collect lines up to the last statement
        # whose end_lineno fits within the remaining budget.  This guarantees
        # we never cut a function/loop body mid-way, avoiding guaranteed
        # SyntaxErrors in the isolated execution stage.
        budget    = max_lines - len(result_lines)
        last_safe = 0   # last line (1-indexed) safe to include

        for stmt in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue  # already in result_lines
            if not (hasattr(stmt, "lineno") and hasattr(stmt, "end_lineno")):
                continue
            if stmt.end_lineno <= budget:
                last_safe = stmt.end_lineno
            else:
                break   # statements are ordered; once we exceed budget, stop

        if last_safe > 0:
            # Include all lines from 1 to last_safe (minus already-included imports)
            included_line_set = import_line_nums   # already in result_lines
            for ln in range(1, last_safe + 1):
                if ln not in included_line_set:
                    result_lines.append(lines[ln - 1])
        # else: budget too small; fallback below handles it

    # Fallback: if we harvested very little, just use raw head
    if len(result_lines) < 5:
        return "\n".join(lines[:max_lines])

    snippet = "\n".join(result_lines)

    # Validate syntax; if broken (e.g. mid-decorator cut), fall back to raw head
    try:
        ast.parse(snippet)
    except SyntaxError:
        return "\n".join(lines[:max_lines])

    return snippet


# ── Stage 3b: Isolated Execution ──────────────────────────────────────────────

def run_snippet(snippet: str, target_pkg: str, python_bin: Path,
                timeout: int = _EXECUTION_TIMEOUT) -> RunResult:
    """
    Execute `snippet` in an isolated subprocess using `python_bin`.

    Uses subprocess.DEVNULL for stdin to prevent interactive hangs.
    Classifies the result into one of 5 outcome types.
    """
    n_lines = len(snippet.splitlines())

    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(snippet)
        tmp_path = f.name

    try:
        res = subprocess.run(
            [str(python_bin), tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        stderr = res.stderr[:500]   # cap for storage

        if res.returncode == 0:
            return RunResult("CLEAN", 1.0, res.returncode, "", n_lines)

        # Classify import errors
        if "ImportError" in res.stderr or "ModuleNotFoundError" in res.stderr:
            if (f"No module named '{target_pkg}'" in res.stderr
                    or f'No module named "{target_pkg}"' in res.stderr):
                return RunResult("TARGET_IMPORT_ERROR", 0.0, res.returncode, stderr, n_lines)
            return RunResult("EXTERNAL_IMPORT_ERROR", 0.9, res.returncode, stderr, n_lines)

        return RunResult("RUNTIME_ERROR", 0.0, res.returncode, stderr, n_lines)

    except subprocess.TimeoutExpired:
        return RunResult("TIMEOUT", 0.7, None, "Execution timed out", n_lines)

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Main orchestrator ─────────────────────────────────────────────────────────

def smoke_trace(
    cache_dir: Path,
    target_pkg: str,
    python_bin: Path,
    top_n: int = 30,
    timeout: int = _EXECUTION_TIMEOUT,
    dry_run: bool = False,
) -> dict:
    """
    Full Smoke Trace pipeline over the files in `cache_dir`.

    Reads from: cache_dir/verified_tests/ + cache_dir/verified_examples/
    Writes to:  cache_dir/ (removes losers, writes smoke_trace_report.json)

    Returns a structured report dict.
    """
    def log(msg: str):
        print(f"[Tracer] {msg}", file=sys.stderr)

    report = {
        "target": target_pkg,
        "cache_dir": str(cache_dir),
        "python_bin": str(python_bin),
        "top_n": top_n,
        "timeout_sec": timeout,
        "total_input": 0,
        "after_static_filter": 0,
        "after_execution": 0,
        "files": [],
    }

    # Discover all .py files in the cache
    all_files: list[tuple[Path, str]] = []   # (abs_path, category)
    for subdir, category in [("verified_tests", "test"), ("verified_examples", "example")]:
        src_dir = cache_dir / subdir
        if src_dir.exists():
            for py in sorted(src_dir.rglob("*.py")):
                all_files.append((py, category))

    report["total_input"] = len(all_files)
    log(f"Found {len(all_files)} .py files in cache")

    if not all_files:
        log("No files to trace. Exiting.")
        return report

    # Stage 1: Static analysis
    log("Stage 1: Static AST scoring …")
    static_scores: list[StaticScore] = []
    for abs_path, category in all_files:
        rel = str(abs_path.relative_to(cache_dir))
        s = analyze_static(abs_path, target_pkg, rel, category)
        static_scores.append(s)

    # Stage 2: Prioritize
    log("Stage 2: Prioritizing top candidates …")
    candidates = prioritize(static_scores, top_n=top_n)
    candidate_paths = {s.file_path for s in candidates}
    report["after_static_filter"] = len(candidates)
    log(f"  {len(candidates)} candidates pass static filter (raw_score ≥ {_STATIC_THRESHOLD})")

    # Stage 3: Snippet + Execution
    log("Stage 3: Snippet extraction + isolated execution …")
    records: list[TraceRecord] = []
    keep_paths: set[Path] = set()

    for sc in candidates:
        try:
            source = sc.file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            records.append(TraceRecord(
                rel_path=sc.rel_path, category=sc.category,
                static_raw_score=sc.raw_score,
                runtime_result="READ_ERROR", runtime_score=0.0,
                final_score=0.0, snippet_lines=0, written=False,
                error=str(exc),
            ))
            continue

        snippet = extract_snippet(source, target_pkg)
        run    = run_snippet(snippet, target_pkg, python_bin, timeout=timeout)

        # Final score = normalized static × runtime
        static_norm  = min(sc.raw_score / _MAX_STATIC_SCORE, 1.0)
        final_score  = round(static_norm * run.runtime_score, 3)
        passed       = final_score >= _FINAL_THRESHOLD

        if passed:
            keep_paths.add(sc.file_path)

        log(f"  {'✅' if passed else '❌'} {sc.rel_path[:60]:<60} "
            f"static={sc.raw_score:.1f} {run.result_type} final={final_score:.2f}")

        records.append(TraceRecord(
            rel_path=sc.rel_path, category=sc.category,
            static_raw_score=sc.raw_score,
            runtime_result=run.result_type,
            runtime_score=run.runtime_score,
            final_score=final_score,
            snippet_lines=run.snippet_lines,
            written=passed and not dry_run,
            api_surface=sc.api_surface,   # Q3: forward to Asset Synthesizer
        ))

    report["after_execution"] = len(keep_paths)

    # Q3: aggregate api_surface across all passing files → single deduped list
    # Asset Synthesizer reads this to build integration_graph.md without re-parsing
    aggregate_api: set[str] = set()
    for rec in records:
        if rec.written or dry_run:
            aggregate_api.update(rec.api_surface)
    report["api_surface"] = sorted(aggregate_api)

    if dry_run:
        log("Dry-run: skipping cache modification")
    else:
        # Remove losers
        removed = 0
        for abs_path, _ in all_files:
            if abs_path not in candidate_paths or abs_path not in keep_paths:
                abs_path.unlink(missing_ok=True)
                removed += 1
        log(f"Removed {removed} files from cache (below threshold)")

        # Prune empty directories
        for subdir in ("verified_tests", "verified_examples"):
            src_dir = cache_dir / subdir
            if src_dir.exists():
                for d in sorted(src_dir.rglob("*"), reverse=True):
                    if d.is_dir():
                        try:
                            d.rmdir()  # only succeeds if empty
                        except OSError:
                            pass

        # Write report
        report_path = cache_dir / "smoke_trace_report.json"
        report["files"] = [asdict(r) for r in records]
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        log(f"Report written → {report_path}")

    log(f"Done. {len(keep_paths)}/{len(candidates)} candidates passed "
        f"(final_score ≥ {_FINAL_THRESHOLD})")
    return report


# ── Helpers ───────────────────────────────────────────────────────────────────

def find_venv_python(venv: Path) -> Optional[Path]:
    """Locate the python binary inside a .venv directory."""
    for candidate in ("bin/python3", "bin/python", "Scripts/python.exe"):
        p = venv / candidate
        if p.exists():
            return p
    return None


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="smoke_tracer",
        description=(
            "DSC Stage 3 — static-score, extract minimal snippets, "
            "and execute them to verify API examples in the knowledge cache."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # From a package_inspector manifest (derives venv + cache dir automatically)
  python3 dsc/smoke_tracer.py --manifest /tmp/ase_manifest.json

  # Explicit mode
  python3 dsc/smoke_tracer.py \\
      --cache-dir ~/.knowledge-cache/ase/3.28.0 \\
      --target ase \\
      --venv /home/tomo/project/002_mlip_pipeline/.venv

  # Dry run (score and report, don't modify cache)
  python3 dsc/smoke_tracer.py --manifest /tmp/ase_manifest.json --dry-run
        """,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--manifest", metavar="FILE",
        help="JSON manifest from package_inspector.py (first package is processed)",
    )
    src.add_argument(
        "--cache-dir", metavar="DIR",
        help="Path to the knowledge cache entry (e.g. ~/.knowledge-cache/ase/3.28.0)",
    )
    p.add_argument("--target",  metavar="PKG", help="Target package name (required with --cache-dir)")
    p.add_argument("--venv",    metavar="DIR", help="Path to .venv (required with --cache-dir)")
    p.add_argument("--top-n",   metavar="N",   type=int, default=30,
                   help="Maximum number of files to pass to execution stage (default: 30)")
    p.add_argument("--timeout", metavar="SEC", type=int, default=_EXECUTION_TIMEOUT,
                   help=f"Execution timeout in seconds (default: {_EXECUTION_TIMEOUT})")
    p.add_argument("--dry-run", action="store_true",
                   help="Score and report without modifying the cache")
    p.add_argument("--compact", action="store_true",
                   help="Emit compact JSON to stdout")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.manifest:
        mf = json.loads(Path(args.manifest).expanduser().read_text())
        pkg      = mf["packages"][0]
        target   = pkg["name"].lower()
        version  = pkg["version"]
        project  = Path(mf["project"])
        venv     = project / ".venv"
        cache_dir = Path(pkg["cache_path"]).expanduser()
    else:
        if not (args.target and args.venv):
            print("ERROR: --target and --venv are required with --cache-dir",
                  file=sys.stderr)
            sys.exit(1)
        target    = args.target
        venv      = Path(args.venv).expanduser().resolve()
        cache_dir = Path(args.cache_dir).expanduser().resolve()

    python_bin = find_venv_python(venv)
    if python_bin is None:
        print(f"ERROR: Cannot find python binary under {venv}", file=sys.stderr)
        sys.exit(1)

    if not cache_dir.exists():
        print(f"ERROR: Cache directory does not exist: {cache_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"[Tracer] target={target}  venv={venv}  cache={cache_dir}", file=sys.stderr)
    print(f"[Tracer] python_bin={python_bin}", file=sys.stderr)

    report = smoke_trace(
        cache_dir=cache_dir,
        target_pkg=target,
        python_bin=python_bin,
        top_n=args.top_n,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )

    indent  = None if args.compact else 2
    summary = {k: v for k, v in report.items() if k != "files"}
    print(json.dumps(summary, indent=indent, ensure_ascii=False))

    sys.exit(0 if report.get("after_execution", 0) >= 0 else 1)


if __name__ == "__main__":
    main()
