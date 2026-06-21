#!/usr/bin/env python3
"""
DSC Stage 4: Asset Synthesizer

Reads smoke_trace_report.json (api_surface pre-extracted by Stage 3 Smoke Tracer)
and generates two Markdown knowledge assets:

  - integration_graph.md:  API dependency table (template mode, default)
                           OR semantic constraints document (--llm mode)
  - workflow_graph.md:     Mermaid flow diagram with code examples

Modes:
  Default (--no-llm):  Fast, offline. Generates an API surface table from
                       extracted FQN names. No network access required.

  LLM mode (--llm):   Sends verified code snippets to OpenRouter and asks an
                       LLM to compile constructor signatures, deprecation
                       warnings, and data-flow constraints into a semantic
                       Markdown document. Requires OPENROUTER_API_KEY.

Usage:
    python3 dsc/asset_synthesizer.py --manifest /tmp/ase_manifest.json
    python3 dsc/asset_synthesizer.py \\
        --cache-dir ~/.knowledge-cache/ase/3.28.0 \\
        --target ase --version 3.28.0
    python3 dsc/asset_synthesizer.py --manifest /tmp/ase_manifest.json --dry-run
    python3 dsc/asset_synthesizer.py --manifest /tmp/ase_manifest.json --llm
"""

import sys
import os
import json
import argparse
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field
from dsc.utils import load_manifest


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
    files: list[dict] = []
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

def _escape_mermaid_label(label: str) -> str:
    """
    Escape special characters in Mermaid labels to prevent rendering errors.
    We replace double quotes with single quotes and brackets/braces with parentheses.
    """
    s = label.replace('"', "'")
    s = s.replace("[", "(").replace("]", ")")
    s = s.replace("{", "(").replace("}", ")")
    return s


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
                lines.append(f'    {node_id}["{_escape_mermaid_label(fqn)}"]')

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


# ── LLM semantic compiler ─────────────────────────────────────────────────────

# Characters-per-token approximation for rough budget calculation
_CHARS_PER_TOKEN = 4

# System prompt for the semantic compiler
_SYNTHESIS_SYSTEM_PROMPT = """You are a technical knowledge compiler specialising in Python library APIs.
Your task is to analyse verified, working Python code snippets and extract SEMANTIC constraints.
Output ONLY valid Markdown. Do not add commentary or explanations outside the document."""

_SYNTHESIS_USER_TEMPLATE = """\
Analyse the following verified code snippets from **{package} {version}** and compile
an `integration_graph.md` that documents semantic constraints.

## Verified Code Snippets
(Source: smoke-traced examples and tests with Trust Score >= 0.9)

{snippets}

## Observed API Surface (FQN list)
```
{api_surface}
```

## Required Output Format
Generate a Markdown document following this EXACT structure:

```markdown
# {PACKAGE} {VERSION} — Integration Graph

Generated by DSC Asset Synthesizer (LLM mode).

## Core API Constraints

### ClassName (`module.ClassName`)
- **Constructor Signature**: `ClassName(exact_args)` — note deprecated params
- **Key Constraint**: [constraint extracted directly from the code]
- **Subclassing Example**:
  ```python
  [minimal working example copied verbatim from snippets]
  ```

## Data Flow Constraints

| From | To | Constraint |
|---|---|---|
| `api_a()` | `api_b()` | return type / required argument |

## Deprecations & Breaking Changes
- [list any deprecated APIs observed in the snippets]
```

## CRITICAL RULES
1. Only document what is EXPLICITLY demonstrated in the provided snippets.
2. Never invent or hallucinate. If evidence is absent, write "(not observed in snippets)".
3. Copy constructor signatures and examples VERBATIM from the working code.
4. Highlight deprecated APIs (e.g. `RandomActivation`) if they appear in comments or errors.
"""


def _get_api_key() -> str:
    """
    Retrieve OPENROUTER_API_KEY from the environment or ~/.zshrc.
    Raises RuntimeError if the key cannot be found.
    """
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    # Attempt to source ~/.zshrc (same strategy as orchestrator.py)
    zshrc = Path.home() / ".zshrc"
    if zshrc.exists():
        try:
            val = subprocess.check_output(
                ["zsh", "-c", f"source {zshrc} && echo $OPENROUTER_API_KEY"],
                text=True,
                timeout=10,
            ).strip()
            if val:
                os.environ["OPENROUTER_API_KEY"] = val
                return val
        except Exception:
            pass
    raise RuntimeError(
        "OPENROUTER_API_KEY not found in environment or ~/.zshrc. "
        "Set it before running with --llm."
    )


def _call_openrouter(
    prompt: str,
    model: str = "deepseek/deepseek-v4-flash",
    max_tokens: int = 4096,
    temperature: float = 0.1,
    timeout: int = 120,
) -> str:
    """
    Call the OpenRouter chat completion API using urllib.request (stdlib only).

    Args:
        prompt:      User-turn message.
        model:       OpenRouter model identifier (without 'openrouter/' prefix).
        max_tokens:  Maximum output tokens.
        temperature: Sampling temperature (low = deterministic).
        timeout:     HTTP timeout in seconds.

    Returns:
        The assistant's reply string.
    """
    api_key = _get_api_key()
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _SYNTHESIS_SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/3tier-ai-devs/dsc",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"OpenRouter API error {exc.code}: {body}") from exc

    return data["choices"][0]["message"]["content"]


def _call_ollama(
    prompt: str,
    model: str = "qwen2.5-coder:7b",
    max_tokens: int = 4096,
    temperature: float = 0.1,
    timeout: int = 600,
    ollama_base_url: str = "http://localhost:11434",
) -> str:
    """
    Call the Ollama chat completion API using urllib.request (stdlib only).

    Compatible with Ollama's /api/chat endpoint (OpenAI-compatible format).

    Args:
        prompt:          User-turn message.
        model:           Ollama model name (e.g. "qwen2.5-coder:7b").
        max_tokens:      Maximum output tokens (maps to num_predict in Ollama).
        temperature:     Sampling temperature.
        timeout:         HTTP timeout in seconds.
        ollama_base_url: Ollama server base URL.

    Returns:
        The assistant's reply string.

    Raises:
        RuntimeError: If the Ollama server is unreachable or returns an error.
    """
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _SYNTHESIS_SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
        },
    }).encode("utf-8")

    url = f"{ollama_base_url.rstrip('/')}/api/chat"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Ollama server unreachable at {url}: {exc}. "
            "Ensure 'ollama serve' is running."
        ) from exc

    # Ollama /api/chat response: {"message": {"role": "assistant", "content": "..."}}
    return data["message"]["content"]


def collect_snippets(cache_dir: Path, max_tokens: int = 24000) -> str:
    """
    Collect verified Python code snippets from cache_dir.

    Reads verified_tests/ first (Trust Score 1.0), then verified_examples/
    (Trust Score 0.9). Concatenates them until the character budget is
    exhausted (budget = max_tokens * _CHARS_PER_TOKEN).

    Returns a single Markdown string with fenced code blocks.
    """
    char_budget = max_tokens * _CHARS_PER_TOKEN
    snippets: list[str] = []

    for subdir in ("verified_tests", "verified_examples"):
        src_dir = cache_dir / subdir
        if not src_dir.exists():
            continue
        for py_file in sorted(src_dir.rglob("*.py")):
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            rel = py_file.relative_to(cache_dir)
            header = f"\n### {rel}\n```python\n"
            footer = "\n```\n"
            entry = header + content + footer
            if char_budget >= len(entry):
                snippets.append(entry)
                char_budget -= len(entry)
            elif char_budget > len(header) + len(footer) + 100:
                # Partial inclusion to fill remaining budget
                truncated = content[: char_budget - len(header) - len(footer) - 20]
                snippets.append(header + truncated + "\n# [truncated]\n" + footer)
                char_budget = 0
                break
        if char_budget <= 0:
            break

    return "".join(snippets)


def synthesize_semantic_graph(
    cache_dir: Path,
    target_pkg: str,
    version: str,
    api_index: "dict[str, ApiEntry]",
    report: dict,
    model: str = "deepseek/deepseek-v4-flash",
    llm_provider: str = "openrouter",
    ollama_base_url: str = "http://localhost:11434",
) -> str:
    """
    Use an LLM to compile a semantic integration_graph.md.

    Collects verified snippets from the cache, formats the synthesis prompt,
    calls OpenRouter or Ollama, and returns the generated Markdown.
    """

    def log(msg: str) -> None:
        print(f"[Synthesizer/LLM] {msg}", file=sys.stderr)

    snippets = collect_snippets(cache_dir)
    if not snippets:
        log("No snippets found in cache. Falling back to template mode.")
        return generate_integration_graph(api_index, report, cache_dir, target_pkg)

    api_surface_str = "\n".join(
        sorted(report.get("api_surface", list(api_index.keys())))
    )

    prompt = _SYNTHESIS_USER_TEMPLATE.format(
        package=target_pkg,
        version=version,
        PACKAGE=target_pkg.upper(),
        VERSION=version,
        snippets=snippets,
        api_surface=api_surface_str,
    )

    if llm_provider == "ollama":
        log(f"Calling Ollama ({model} @ {ollama_base_url}) with {len(snippets)} chars of snippets …")
        try:
            result = _call_ollama(
                prompt, model=model, ollama_base_url=ollama_base_url
            )
            log("Ollama synthesis complete.")
            return result
        except Exception as exc:
            log(f"Ollama call failed ({exc}). Falling back to template mode.")
            return generate_integration_graph(api_index, report, cache_dir, target_pkg)
    else:
        log(f"Calling OpenRouter ({model}) with {len(snippets)} chars of snippets …")
        try:
            result = _call_openrouter(prompt, model=model)
            log("LLM synthesis complete.")
            return result
        except Exception as exc:
            log(f"LLM call failed ({exc}). Falling back to template mode.")
            return generate_integration_graph(api_index, report, cache_dir, target_pkg)


# ── Orchestrator ───────────────────────────────────────────────────────────────


def synthesize(
    cache_dir: Path,
    target_pkg: str,
    version: str,
    dry_run: bool = False,
    use_llm: bool = False,
    llm_model: str = "deepseek/deepseek-v4-flash",
    llm_provider: str = "openrouter",
    ollama_base_url: str = "http://localhost:11434",
) -> dict:
    """
    Full Asset Synthesizer pipeline.

    1. load_report()
    2. build_api_index()
    3a. (use_llm=False) generate_integration_graph() — API surface table
    3b. (use_llm=True)  synthesize_semantic_graph()  — LLM semantic doc
    4. generate_workflow_graph()  -> workflow_graph.md
    5. Return result report
    """

    def log(msg: str):
        print(f"[Synthesizer] {msg}", file=sys.stderr)

    mode = "LLM" if use_llm else "template"
    log(f"Synthesizing assets for {target_pkg} {version} -> {cache_dir} (mode: {mode})")

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
            "mode": mode,
        }

    # Step 2: Build API index
    api_index = build_api_index(report)
    log(f"Built API index: {len(api_index)} unique APIs")

    # Step 3: Generate integration graph (template or LLM)
    if use_llm:
        ig_content = synthesize_semantic_graph(
            cache_dir=cache_dir,
            target_pkg=target_pkg,
            version=version,
            api_index=api_index,
            report=report,
            model=llm_model,
            llm_provider=llm_provider,
            ollama_base_url=ollama_base_url,
        )
    else:
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
        "mode": mode,
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
    llm_group = p.add_mutually_exclusive_group()
    llm_group.add_argument(
        "--llm",
        action="store_true",
        default=False,
        help=(
            "Use LLM (OpenRouter) to generate a semantic integration_graph.md "
            "with constructor constraints, deprecation notes, and data-flow rules. "
            "Requires OPENROUTER_API_KEY. (default: off)"
        ),
    )
    llm_group.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help="Use template-based generation only (default, offline-safe).",
    )
    p.add_argument(
        "--llm-model",
        metavar="MODEL",
        default="deepseek/deepseek-v4-flash",
        help="OpenRouter model to use for LLM synthesis (default: deepseek/deepseek-v4-flash)",
    )
    p.add_argument(
        "--llm-provider",
        metavar="PROVIDER",
        default="openrouter",
        choices=["openrouter", "ollama"],
        help=(
            "LLM provider for --llm mode. "
            "'openrouter' (default): uses OPENROUTER_API_KEY. "
            "'ollama': calls local Ollama server at --ollama-url."
        ),
    )
    p.add_argument(
        "--ollama-url",
        metavar="URL",
        default="http://localhost:11434",
        help="Ollama server base URL (default: http://localhost:11434). "
             "Used when --llm-provider ollama.",
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
        mf = load_manifest(args.manifest)
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
        use_llm=args.llm,
        llm_model=args.llm_model,
        llm_provider=args.llm_provider,
        ollama_base_url=args.ollama_url,
    )

    indent = None if args.compact else 2
    print(json.dumps(result, indent=indent, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
