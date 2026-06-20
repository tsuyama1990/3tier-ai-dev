#!/usr/bin/env python3
"""
DSC Stage 4: Asset Synthesizer

Reads smoke_trace_report.json (api_surface pre-extracted by Stage 3 Smoke Tracer)
and generates two Markdown knowledge assets:

  - integration_graph.md:  API dependency table grouped by module
  - workflow_graph.md:     Mermaid flow diagram with code examples

Usage:
    python3 dsc/asset_synthesizer.py --manifest /tmp/ase_manifest.json
    python3 dsc/asset_synthesizer.py \\
        --cache-dir ~/.knowledge-cache/ase/3.28.0 \\
        --target ase --version 3.28.0
    python3 dsc/asset_synthesizer.py --manifest /tmp/ase_manifest.json --dry-run
"""

import sys
import json
import argparse
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


KNOWLEDGE_CACHE = Path.home() / ".knowledge-cache"


# ── Data models ────────────────────────────────────────────────────────────────


class ApiEntry(BaseModel):
    """One fully-qualified API name observed across verified files."""

    fqn: str  # e.g. "ase.io.read"
    module: str  # e.g. "ase.io"
    name: str  # e.g. "read"
    used_in: list[str] = Field(default_factory=list)  # rel_paths
    max_trust_score: float = 0.0


# ── Report loading ────────────────────────────────────────────────────────────


def load_report(cache_dir: Path) -> dict:
    """
    Load smoke_trace_report.json from cache_dir.

    If the file doesn't exist, scan the cache directory to build a minimal
    report (handles the case where only source_miner was run, skipping smoke_tracer).
    """
    report_path = cache_dir / "smoke_trace_report.json"
    if report_path.exists():
        return json.loads(report_path.read_text(encoding="utf-8"))

    # Fallback: build a minimal report from cache directory contents
    files = []
    api_surface_set: set[str] = set()

    for subdir in ("verified_examples", "verified_tests"):
        src_dir = cache_dir / subdir
        if src_dir.exists():
            for py_file in sorted(src_dir.rglob("*.py")):
                rel = str(py_file.relative_to(cache_dir))
                files.append(
                    {
                        "rel_path": rel,
                        "category": "example"
                        if subdir == "verified_examples"
                        else "test",
                        "final_score": 0.0,
                        "api_surface": [],
                    }
                )

    return {
        "target": cache_dir.parent.name,
        "cache_dir": str(cache_dir),
        "files": files,
        "api_surface": sorted(api_surface_set),
    }


# ── API Index ──────────────────────────────────────────────────────────────────


def build_api_index(report: dict) -> dict[str, ApiEntry]:
    """
    Build an FQN -> ApiEntry index from report["files"][].api_surface.

    Files with empty api_surface are supplemented with the aggregate
    report["api_surface"].
    """
    api_index: dict[str, ApiEntry] = {}

    for f in report.get("files", []):
        api_surface = f.get("api_surface", [])
        if not api_surface:
            # Fall back to aggregate api_surface
            api_surface = report.get("api_surface", [])

        for fqn in api_surface:
            if fqn not in api_index:
                parts = fqn.split(".")
                name = parts[-1]
                module = ".".join(parts[:-1]) if len(parts) > 1 else ""
                api_index[fqn] = ApiEntry(
                    fqn=fqn,
                    module=module,
                    name=name,
                )
            entry = api_index[fqn]
            rel_path = f.get("rel_path", "")
            if rel_path and rel_path not in entry.used_in:
                entry.used_in.append(rel_path)
            trust = f.get("final_score", f.get("runtime_score", 0.0))
            if trust > entry.max_trust_score:
                entry.max_trust_score = trust

    return api_index


# ── Module display name ────────────────────────────────────────────────────────


def _module_display(target_pkg: str, module: str) -> str:
    """Return a display name for a module path.

    - "ase" (bare package)        -> "ase (core)"
    - "ase.io"                    -> "ase.io"
    - "ase.calculators.emt"       -> "ase.calculators.emt"
    """
    if not module or module == target_pkg:
        return f"{target_pkg} (core)"
    return module


# ── Integration Graph ──────────────────────────────────────────────────────────


def generate_integration_graph(
    api_index: dict[str, ApiEntry],
    report: dict,
    cache_dir: Path,
    target_pkg: str = "",
) -> str:
    """
    Group api_index by module and produce a Markdown table.

    Grouping key:
        "ase.Atoms"                -> "ase (core)"
        "ase.io.read"              -> "ase.io"
        "ase.calculators.emt.EMT"  -> "ase.calculators.emt"
    """
    if not target_pkg:
        target_pkg = report.get("target", cache_dir.parent.name)

    # Group by module
    modules: dict[str, list[ApiEntry]] = {}
    for entry in api_index.values():
        mod = _module_display(target_pkg, entry.module)
        modules.setdefault(mod, []).append(entry)

    # Sort modules: core first, then alphabetical
    core_key = f"{target_pkg} (core)"
    sorted_modules = sorted(modules.keys(), key=lambda m: (m != core_key, m))

    lines = [
        f"# {target_pkg.upper()} {report.get('version', '')} \u2014 Integration Graph",
        "",
        "Generated by DSC Asset Synthesizer.",
        "Source: smoke_trace_report.json (api_surface extracted by Smoke Tracer Stage 3)",
        "",
        "## API Surface by Module",
        "",
    ]

    for mod in sorted_modules:
        entries = modules[mod]
        # Sort entries alphabetically by name
        entries.sort(key=lambda e: e.name)

        lines.append(f"### {mod}")
        lines.append("| API | Used in | Trust Score |")
        lines.append("|-----|---------|-------------|")
        for entry in entries:
            used_str = ", ".join(entry.used_in) if entry.used_in else "\u2014"
            lines.append(f"| `{entry.fqn}` | {used_str} | {entry.max_trust_score} |")
        lines.append("")

    # Summary statistics
    total_files = len(report.get("files", []))
    total_unique_apis = len(api_index)
    all_modules = list(modules.keys())
    top_modules = [m for m in all_modules if m != core_key]
    summary_modules = (
        [core_key] + top_modules[:3] if core_key in modules else top_modules[:3]
    )

    lines.extend(
        [
            "## Summary Statistics",
            f"- Total verified files: {total_files}",
            f"- Unique APIs observed: {total_unique_apis}",
            f"- Top-level modules covered: {', '.join(summary_modules)}",
            "",
        ]
    )

    return "\n".join(lines)


# ── Workflow Graph ─────────────────────────────────────────────────────────────


def _snippet_from_file(file_path: Path, max_lines: int = 20) -> str:
    """Extract the first ``max_lines`` lines from a Python file as a code snippet."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        snippet_lines = source.splitlines()
        snippet = "\n".join(snippet_lines[:max_lines])
        return snippet
    except Exception:
        return "# (could not read file)"


def generate_workflow_graph(
    api_index: dict[str, ApiEntry],
    report: dict,
    cache_dir: Path,
    target_pkg: str = "",
) -> str:
    """
    Generate Mermaid workflow diagram and code examples from verified_examples/.

    Selects up to 5 files by final_score (descending), builds a Mermaid
    graph TD from their api_surface, and appends code snippets.
    Edges are limited to a maximum of 15.
    """
    if not target_pkg:
        target_pkg = report.get("target", cache_dir.parent.name)

    # Collect verified_examples files with their final_score and api_surface
    file_scores: list[tuple[float, str, list[str]]] = []
    for f in report.get("files", []):
        if not f.get("rel_path", "").startswith("verified_examples"):
            continue
        api_surface = f.get("api_surface", [])
        if not api_surface:
            api_surface = report.get("api_surface", [])
        file_scores.append(
            (
                f.get("final_score", 0.0),
                f["rel_path"],
                list(api_surface),
            )
        )

    # Sort by final_score descending, take top 5
    file_scores.sort(key=lambda x: x[0], reverse=True)
    top_files = file_scores[:5]

    lines = [
        f"# {target_pkg.upper()} {report.get('version', '')} \u2014 Workflow Graph",
        "",
        "## Typical Usage Flow",
        "",
        "```mermaid",
        "graph TD",
    ]

    # Build Mermaid nodes and edges from api_surface chains
    node_ids: dict[str, str] = {}
    node_counter = 0
    edge_count = 0
    max_edges = 15

    for _score, _rel_path, api_surface in top_files:
        prev_id: Optional[str] = None
        for fqn in api_surface:
            if fqn not in node_ids:
                node_counter += 1
                node_id = f"N{node_counter}"
                node_ids[fqn] = node_id
                lines.append(f'    {node_id}["{fqn}"]')

            current_id = node_ids[fqn]

            if prev_id is not None and edge_count < max_edges:
                lines.append(f"    {prev_id} --> {current_id}")
                edge_count += 1

            prev_id = current_id

        # Reset prev_id between files (each file represents a separate workflow)
        prev_id = None

    lines.extend(
        [
            "```",
            "",
            "## Code Examples by Workflow Stage",
            "",
        ]
    )

    for i, (score, rel_path, _api_surface) in enumerate(top_files, 1):
        file_path = cache_dir / rel_path
        snippet = _snippet_from_file(file_path)
        lines.append(f"### Stage {i}: {Path(rel_path).stem}")
        lines.append(f"**Source**: {rel_path} (trust_score={score})")
        lines.append("```python")
        lines.append(snippet)
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# ── Orchestrator ───────────────────────────────────────────────────────────────


def synthesize(
    cache_dir: Path,
    target_pkg: str,
    version: str,
    dry_run: bool = False,
) -> dict:
    """
    Full Asset Synthesizer pipeline.

    1. load_report()
    2. build_api_index()
    3. generate_integration_graph()  -> integration_graph.md
    4. generate_workflow_graph()     -> workflow_graph.md
    5. Return result report
    """

    def log(msg: str):
        print(f"[Synthesizer] {msg}", file=sys.stderr)

    log(f"Synthesizing assets for {target_pkg} {version} -> {cache_dir}")

    # Step 1: Load report
    report = load_report(cache_dir)
    log(f"Loaded report: {len(report.get('files', []))} files")

    if not report.get("files"):
        log("No files found in report. Nothing to generate.")
        return {
            "target": target_pkg,
            "version": version,
            "cache_dir": str(cache_dir),
            "integration_graph_written": False,
            "workflow_graph_written": False,
            "files_found": 0,
        }

    # Step 2: Build API index
    api_index = build_api_index(report)
    log(f"Built API index: {len(api_index)} unique APIs")

    # Step 3: Generate integration graph
    ig_content = generate_integration_graph(api_index, report, cache_dir, target_pkg)
    if not dry_run:
        ig_path = cache_dir / "integration_graph.md"
        ig_path.write_text(ig_content + "\n", encoding="utf-8")
        log(f"Written -> {ig_path}")
    else:
        log("Dry-run: skipping integration_graph.md write")

    # Step 4: Generate workflow graph
    wg_content = generate_workflow_graph(api_index, report, cache_dir, target_pkg)
    if not dry_run:
        wg_path = cache_dir / "workflow_graph.md"
        wg_path.write_text(wg_content + "\n", encoding="utf-8")
        log(f"Written -> {wg_path}")
    else:
        log("Dry-run: skipping workflow_graph.md write")

    return {
        "target": target_pkg,
        "version": version,
        "cache_dir": str(cache_dir),
        "integration_graph_written": not dry_run,
        "workflow_graph_written": not dry_run,
        "files_found": len(report.get("files", [])),
        "unique_apis": len(api_index),
    }


# ── CLI ────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    p = argparse.ArgumentParser(
        prog="asset_synthesizer",
        description=(
            "DSC Stage 4 \u2014 read smoke_trace_report.json api_surface and "
            "generate integration_graph.md + workflow_graph.md knowledge assets."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # From a package_inspector manifest (derives cache dir automatically)
  python3 dsc/asset_synthesizer.py --manifest /tmp/ase_manifest.json

  # Explicit mode
  python3 dsc/asset_synthesizer.py \\
      --cache-dir ~/.knowledge-cache/ase/3.28.0 \\
      --target ase --version 3.28.0

  # Dry run (check what would be generated)
  python3 dsc/asset_synthesizer.py --manifest /tmp/ase_manifest.json --dry-run
        """,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--manifest",
        metavar="FILE",
        help="JSON manifest from package_inspector.py (first package is processed)",
    )
    src.add_argument(
        "--cache-dir",
        metavar="DIR",
        help="Path to the knowledge cache entry (e.g. ~/.knowledge-cache/ase/3.28.0)",
    )
    p.add_argument(
        "--target",
        metavar="PKG",
        help="Target package name (required with --cache-dir)",
    )
    p.add_argument(
        "--version",
        metavar="VER",
        help="Package version (required with --cache-dir)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate reports in memory but do not write to cache",
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON report",
    )
    return p


def main(argv=None):
    """CLI entry point."""
    args = build_parser().parse_args(argv)

    if args.manifest:
        mf = json.loads(Path(args.manifest).expanduser().read_text())
        pkg = mf["packages"][0]
        target = pkg["name"].lower()
        version = pkg["version"]
        cache_dir = Path(pkg["cache_path"]).expanduser()
    else:
        if not (args.target and args.version):
            print(
                "ERROR: --target and --version are required with --cache-dir",
                file=sys.stderr,
            )
            sys.exit(1)
        target = args.target.lower()
        version = args.version
        cache_dir = Path(args.cache_dir).expanduser().resolve()

    if not cache_dir.exists():
        print(f"ERROR: Cache directory does not exist: {cache_dir}", file=sys.stderr)
        sys.exit(1)

    result = synthesize(
        cache_dir=cache_dir,
        target_pkg=target,
        version=version,
        dry_run=args.dry_run,
    )

    indent = None if args.compact else 2
    print(json.dumps(result, indent=indent, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
