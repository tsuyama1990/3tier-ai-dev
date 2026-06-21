#!/usr/bin/env python3
"""
DSC Stage 5: Deploy

Copies verified knowledge assets from the global knowledge cache
(~/.knowledge-cache/{pkg}/{version}/) into a target project's local
directories, establishing the "Hard Copy" contract defined in §2.2 of
the architecture design.

Deployment mapping:
  cache/integration_graph.md   → project/.ai-knowledge/{pkg}.md
  cache/workflow_graph.md      → project/.ai-knowledge/workflow_graph.md  (merged)
  cache/verified_examples/*.py → project/verified_examples/
  cache/verified_tests/*.py    → project/verified_tests/

Additionally generates (or updates) api_schema.yaml with the deployed
packages added to the allowed_imports list.

Usage:
    # Deploy a single package (version auto-detected from manifest or cache)
    python3 dsc/deploy.py --project /path/to/project --packages mesa=3.5.1

    # Deploy multiple packages (workflow_graph.md is merged)
    python3 dsc/deploy.py --project /path/to/project \\
        --packages ase=3.28.0 pacemaker=0.8.4

    # From a package_inspector manifest (uses the first matching package)
    python3 dsc/deploy.py --project /path/to/project \\
        --manifest /tmp/mesa_manifest.json

    # Preview changes without writing
    python3 dsc/deploy.py --project /path/to/project \\
        --packages mesa=3.5.1 --dry-run

    # Overwrite existing files (including api_schema.yaml)
    python3 dsc/deploy.py --project /path/to/project \\
        --packages mesa=3.5.1 --force
"""

import sys
import re
import json
import shutil
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dsc.config import KNOWLEDGE_CACHE
from dsc.utils import load_manifest

# ── Constants ─────────────────────────────────────────────────────────────────

# Standard library modules always allowed in api_schema.yaml
_STDLIB_ALLOWED = [
    "os",
    "sys",
    "json",
    "math",
    "re",
    "pathlib",
    "typing",
    "dataclasses",
    "collections",
    "itertools",
    "functools",
    "copy",
    "abc",
    "io",
    "time",
    "datetime",
    "string",
    "random",
    "warnings",
]

# Test infrastructure
_TEST_ALLOWED = ["pytest", "unittest"]


# ── Exceptions ────────────────────────────────────────────────────────────────


class CacheNotFoundError(RuntimeError):
    """Raised when the requested package/version is not in the cache."""


class DeployError(RuntimeError):
    """Generic deploy failure."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    print(f"[Deploy] {msg}", file=sys.stderr)


def _copy_file(src: Path, dst: Path, dry_run: bool, force: bool) -> Optional[str]:
    """
    Copy src → dst.

    Returns:
        'copied'   — file was written
        'skipped'  — dst exists and force=False
        'dry_run'  — would have been copied, dry_run=True
        None       — src does not exist (silently ignored)
    """
    if not src.exists():
        return None

    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists() and not force:
        _log(f"  SKIP (exists, use --force to overwrite): {dst}")
        return "skipped"

    if dry_run:
        _log(f"  DRY-RUN copy: {src} → {dst}")
        return "dry_run"

    shutil.copy2(src, dst)
    _log(f"  COPY: {src} → {dst}")
    return "copied"


def _copy_tree(
    src_dir: Path, dst_dir: Path, dry_run: bool, force: bool, glob: str = "**/*.py"
) -> list[str]:
    """
    Copy all files matching `glob` under src_dir into dst_dir,
    preserving the relative sub-directory structure.

    Returns list of destination paths that were copied (or would be, in dry-run).
    """
    if not src_dir.exists():
        return []

    results = []
    for src in sorted(src_dir.glob(glob)):
        rel = src.relative_to(src_dir)
        dst = dst_dir / rel
        outcome = _copy_file(src, dst, dry_run, force)
        if outcome in ("copied", "dry_run"):
            results.append(str(dst))
    return results


# ── api_schema.yaml generation ────────────────────────────────────────────────


def _load_existing_schema(schema_path: Path) -> list[str]:
    """
    Parse the allowed_imports list from an existing api_schema.yaml.
    Returns an empty list if the file does not exist or cannot be parsed.
    """
    if not schema_path.exists():
        return []
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return list(data.get("allowed_imports", []))
    except Exception:
        pass
    # Fallback: regex parse for environments without PyYAML
    allowed: list[str] = []
    in_list = False
    for line in schema_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("allowed_imports:"):
            in_list = True
            continue
        if in_list:
            m = re.match(r"^\s+-\s+(\S+)", line)
            if m:
                allowed.append(m.group(1).split("#")[0].strip())
            elif line.strip() and not line.strip().startswith("#"):
                break  # end of list
    return allowed


def generate_api_schema(
    packages: list[tuple[str, str]],
    existing_path: Optional[Path] = None,
    force: bool = False,
) -> str:
    """
    Build the content of api_schema.yaml.

    If `existing_path` exists and force=False, new package names are
    appended to the existing list (preserving manual additions).
    If force=True or the file does not exist, a fresh schema is generated.
    """
    pkg_names = [name.lower() for name, _ in packages]
    pkg_versions = {name.lower(): ver for name, ver in packages}

    if existing_path and existing_path.exists() and not force:
        existing = _load_existing_schema(existing_path)
        # Add new packages not yet in the list
        combined = list(existing)
        added = []
        for name in pkg_names:
            if name not in combined:
                combined.append(name)
                added.append(name)
        if not added:
            return existing_path.read_text(encoding="utf-8")
        pkg_section = combined
    else:
        pkg_section = list(pkg_names)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pkg_comment = ", ".join(f"{n}={pkg_versions.get(n, '?')}" for n in pkg_names)

    lines = [
        "# auto-generated by dsc/deploy.py — manual additions are preserved on re-run",
        f"# generated: {now}",
        f"# source packages: {pkg_comment}",
        "allowed_imports:",
    ]

    # Package-specific imports (with version comments)
    for name in pkg_section:
        ver = pkg_versions.get(name)
        comment = f"  # {name}={ver}" if ver else ""
        lines.append(f"  - {name}{comment}")

    # Standard library
    lines.append("  # standard library")
    for mod in _STDLIB_ALLOWED:
        lines.append(f"  - {mod}")

    # Test infrastructure
    lines.append("  # test infrastructure")
    for mod in _TEST_ALLOWED:
        lines.append(f"  - {mod}")

    lines.append("")  # trailing newline
    return "\n".join(lines)


# ── workflow_graph.md merge ────────────────────────────────────────────────────


def _extract_sections(content: str) -> dict[str, str]:
    """
    Split a workflow_graph.md into named sections keyed by ## headings.
    Returns {heading_text: section_content}.
    """
    sections: dict[str, str] = {}
    current_key: Optional[str] = None
    current_lines: list[str] = []

    for line in content.splitlines(keepends=True):
        if line.startswith("## "):
            if current_key is not None:
                sections[current_key] = "".join(current_lines)
            current_key = line[3:].rstrip()
            current_lines = [line]
        else:
            if current_key is not None:
                current_lines.append(line)
            # lines before the first ## heading are part of the preamble
    if current_key is not None:
        sections[current_key] = "".join(current_lines)
    return sections


def merge_workflow_graphs(
    contents: list[tuple[str, str, str]]
) -> str:
    """
    Merge multiple workflow_graph.md files into one.

    contents: [(pkg_name, version, markdown_text), ...]

    Strategy:
      - Single package → return as-is (no merge needed).
      - Multiple packages → concatenate with per-package headers.
        The merged file gets a combined title and Mermaid diagram placeholder.
    """
    if not contents:
        return ""
    if len(contents) == 1:
        pkg, ver, text = contents[0]
        return text

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pkg_labels = " + ".join(f"{p} {v}" for p, v, _ in contents)

    lines = [
        "# Multi-Package Workflow Graph",
        "",
        f"Packages: {pkg_labels}",
        f"Merged by dsc/deploy.py at {now}",
        "",
        "> [!NOTE]",
        "> Cross-package boundary conditions (e.g. ASE → pacemaker → LAMMPS)",
        "> are not yet defined here. Run `dsc/asset_synthesizer.py --llm` to",
        "> generate semantic inter-package workflow documentation.",
        "",
        "---",
        "",
    ]

    for pkg, ver, text in contents:
        lines.append(f"## {pkg} {ver}")
        lines.append("")
        # Strip the top-level # title to avoid double headings
        body = re.sub(r"^#[^#].*\n", "", text, count=1)
        lines.append(body.strip())
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ── Core deploy logic ─────────────────────────────────────────────────────────


def deploy(
    project: Path,
    packages: list[tuple[str, str]],
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """
    Full deployment pipeline for one or more packages.

    For each (name, version):
      1. Locate cache dir  → raise CacheNotFoundError if absent
      2. Copy integration_graph.md → .ai-knowledge/{name}.md
      3. Collect workflow_graph.md content
      4. Copy verified_examples/*.py → project/verified_examples/
      5. Copy verified_tests/*.py   → project/verified_tests/
    Then:
      6. Merge workflow_graphs → .ai-knowledge/workflow_graph.md
      7. Generate/update api_schema.yaml

    Returns a structured result dict.
    """
    ai_knowledge_dir = project / ".ai-knowledge"
    verified_examples_dir = project / "verified_examples"
    verified_tests_dir = project / "verified_tests"
    schema_path = project / "api_schema.yaml"

    result: dict = {
        "project": str(project),
        "packages": [],
        "workflow_graph_written": False,
        "api_schema_written": False,
        "dry_run": dry_run,
    }

    workflow_contents: list[tuple[str, str, str]] = []

    for pkg_name, version in packages:
        cache_dir = KNOWLEDGE_CACHE / pkg_name.lower() / version
        pkg_result: dict = {
            "name": pkg_name,
            "version": version,
            "cache_dir": str(cache_dir),
            "integration_graph": None,
            "verified_examples": [],
            "verified_tests": [],
            "error": None,
        }

        if not cache_dir.exists():
            msg = (
                f"Cache not found: {cache_dir}\n"
                f"  Run the DSC pipeline first:\n"
                f"    python3 dsc/package_inspector.py --project {project} "
                f"--target {pkg_name} --output /tmp/{pkg_name}_manifest.json\n"
                f"    python3 dsc/source_miner.py --manifest /tmp/{pkg_name}_manifest.json\n"
                f"    python3 dsc/smoke_tracer.py --manifest /tmp/{pkg_name}_manifest.json\n"
                f"    python3 dsc/asset_synthesizer.py --manifest /tmp/{pkg_name}_manifest.json"
            )
            _log(f"ERROR: {msg}")
            pkg_result["error"] = msg
            result["packages"].append(pkg_result)
            continue

        _log(f"Deploying {pkg_name}=={version} from {cache_dir}")

        # ── 1. integration_graph.md → .ai-knowledge/{name}.md ─────────────
        ig_src = cache_dir / "integration_graph.md"
        ig_dst = ai_knowledge_dir / f"{pkg_name.lower()}.md"
        outcome = _copy_file(ig_src, ig_dst, dry_run, force)
        pkg_result["integration_graph"] = outcome

        # ── 2. Collect workflow_graph.md ───────────────────────────────────
        wg_src = cache_dir / "workflow_graph.md"
        if wg_src.exists():
            workflow_contents.append((pkg_name, version, wg_src.read_text(encoding="utf-8")))

        # ── 3. verified_examples/ ──────────────────────────────────────────
        copied_examples = _copy_tree(
            cache_dir / "verified_examples",
            verified_examples_dir,
            dry_run,
            force,
        )
        pkg_result["verified_examples"] = copied_examples

        # ── 4. verified_tests/ ────────────────────────────────────────────
        copied_tests = _copy_tree(
            cache_dir / "verified_tests",
            verified_tests_dir,
            dry_run,
            force,
        )
        pkg_result["verified_tests"] = copied_tests

        result["packages"].append(pkg_result)

    # ── 5. Merge and write workflow_graph.md ──────────────────────────────
    if workflow_contents:
        merged = merge_workflow_graphs(workflow_contents)
        wg_dst = ai_knowledge_dir / "workflow_graph.md"
        wg_dst.parent.mkdir(parents=True, exist_ok=True)

        if wg_dst.exists() and not force:
            _log(f"  SKIP workflow_graph.md (exists, use --force): {wg_dst}")
        elif dry_run:
            _log(f"  DRY-RUN write: {wg_dst} ({len(merged)} chars)")
            result["workflow_graph_written"] = True
        else:
            wg_dst.write_text(merged, encoding="utf-8")
            _log(f"  WRITE workflow_graph.md → {wg_dst}")
            result["workflow_graph_written"] = True

    # ── 6. api_schema.yaml ────────────────────────────────────────────────
    deployed_packages = [
        (r["name"], r["version"])
        for r in result["packages"]
        if r.get("error") is None
    ]

    if deployed_packages:
        schema_content = generate_api_schema(
            deployed_packages,
            existing_path=schema_path,
            force=force,
        )
        if schema_path.exists() and not force:
            # Check if content changed (new packages appended)
            existing = schema_path.read_text(encoding="utf-8")
            if existing.strip() == schema_content.strip():
                _log(f"  SKIP api_schema.yaml (unchanged): {schema_path}")
            elif dry_run:
                _log(f"  DRY-RUN update api_schema.yaml: {schema_path}")
                result["api_schema_written"] = True
            else:
                schema_path.write_text(schema_content, encoding="utf-8")
                _log(f"  UPDATE api_schema.yaml → {schema_path}")
                result["api_schema_written"] = True
        elif dry_run:
            _log(f"  DRY-RUN write api_schema.yaml: {schema_path}")
            result["api_schema_written"] = True
        else:
            schema_path.parent.mkdir(parents=True, exist_ok=True)
            schema_path.write_text(schema_content, encoding="utf-8")
            _log(f"  WRITE api_schema.yaml → {schema_path}")
            result["api_schema_written"] = True

    return result


# ── Package spec parsing ──────────────────────────────────────────────────────


def _parse_package_spec(spec: str) -> tuple[str, str]:
    """
    Parse 'name=version' or 'name==version' into (name, version).
    Raises ValueError on invalid format.
    """
    for sep in ("==", "="):
        if sep in spec:
            parts = spec.split(sep, 1)
            name = parts[0].strip()
            version = parts[1].strip()
            if name and version:
                return name, version
    raise ValueError(
        f"Invalid package spec '{spec}'. Expected 'name=version' (e.g. mesa=3.5.1)."
    )


def _auto_detect_version(pkg_name: str) -> Optional[str]:
    """
    If only a package name is given (no version), scan the cache for
    the most recently modified version directory.

    Returns the version string or None if nothing is found.
    """
    pkg_cache = KNOWLEDGE_CACHE / pkg_name.lower()
    if not pkg_cache.exists():
        return None
    versions = [d for d in pkg_cache.iterdir() if d.is_dir()]
    if not versions:
        return None
    # Sort by mtime descending, return newest
    versions.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return versions[0].name


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy",
        description=(
            "DSC Stage 5 — copy verified knowledge assets from the global "
            "~/.knowledge-cache/ into a project's local .ai-knowledge/, "
            "verified_examples/, and verified_tests/ directories."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Deploy a single package
  python3 dsc/deploy.py --project ~/project/001_abm/test --packages mesa=3.5.1

  # Deploy multiple packages (workflow_graph.md is merged)
  python3 dsc/deploy.py --project ~/project/002_mlip --packages ase=3.28.0 pacemaker=0.8.4

  # Preview without writing
  python3 dsc/deploy.py --project ~/project/001_abm/test --packages mesa=3.5.1 --dry-run

  # Overwrite existing files
  python3 dsc/deploy.py --project ~/project/001_abm/test --packages mesa=3.5.1 --force

  # From a package_inspector manifest (version auto-detected)
  python3 dsc/deploy.py --project ~/project/001_abm/test --manifest manifest.json
        """,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--packages",
        nargs="+",
        metavar="PKG=VER",
        help="One or more package specs in 'name=version' format (e.g. mesa=3.5.1)",
    )
    src.add_argument(
        "--manifest",
        metavar="FILE",
        help="JSON manifest from package_inspector.py (all found packages are deployed)",
    )
    p.add_argument(
        "--project",
        required=True,
        metavar="DIR",
        help="Absolute path to the target project directory",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be copied without writing any files",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files (including api_schema.yaml)",
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact (non-pretty) JSON result",
    )
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        print(f"ERROR: project path does not exist: {project}", file=sys.stderr)
        sys.exit(1)

    # Resolve package list
    packages: list[tuple[str, str]] = []

    if args.manifest:
        try:
            mf = load_manifest(args.manifest)
        except Exception as exc:
            print(f"ERROR: cannot read manifest: {exc}", file=sys.stderr)
            sys.exit(1)
        for pkg in mf.get("packages", []):
            if not pkg.get("found"):
                continue
            name = pkg.get("name", "")
            version = pkg.get("version", "")
            if name and version and version != "unknown":
                packages.append((name, version))
        if not packages:
            print("ERROR: no usable packages found in manifest.", file=sys.stderr)
            sys.exit(1)
    else:
        for spec in args.packages:
            if "=" in spec:
                try:
                    packages.append(_parse_package_spec(spec))
                except ValueError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    sys.exit(1)
            else:
                # No version given: auto-detect from cache
                ver = _auto_detect_version(spec)
                if ver is None:
                    print(
                        f"ERROR: No cached version found for '{spec}'. "
                        f"Specify version explicitly (e.g. {spec}=1.0.0).",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                _log(f"Auto-detected version for {spec}: {ver}")
                packages.append((spec, ver))

    _log(
        f"Deploying {len(packages)} package(s) into {project}"
        + (" [DRY-RUN]" if args.dry_run else "")
    )

    result = deploy(
        project=project,
        packages=packages,
        dry_run=args.dry_run,
        force=args.force,
    )

    indent = None if args.compact else 2
    print(json.dumps(result, indent=indent, ensure_ascii=False))

    # Exit non-zero if any package had an error
    if any(r.get("error") for r in result["packages"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
