"""Knowledge Harvester — extracts and compresses PyPI package documentation.

Phase 3, Priority 3.

On ``ekp-forge package add <package>``, this module:
1. Calls PyPI JSON API for package metadata.
2. Downloads and parses README.
3. Extracts top-level modules, classes, and usage examples.
4. Compresses into a markdown file saved to ``.ai-knowledge/libs/<package>.md``.

Design:
- All operations are **deterministic** — no LLM calls, no embeddings.
- README parsing is regex-based (extract code blocks, headings, lists).
- Output markdown is intentionally minimal to prevent context bloom for 7B workers.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
AI_KNOWLEDGE_DIR = Path(".ai-knowledge") / "libs"

# Maximum number of usage examples to extract
_MAX_EXAMPLES = 3

# Maximum lines per usage example
_MAX_EXAMPLE_LINES = 30

# Maximum characters in compressed doc
_MAX_DOC_CHARS = 5000

# Maximum characters in README before we start truncating
_MAX_README_CHARS = 20000


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class PackageInfo:
    """Structured info about a PyPI package."""

    name: str = ""
    version: str = ""
    summary: str = ""
    top_level_modules: list[str] = field(default_factory=list)
    classes: list[dict[str, Any]] = field(default_factory=list)
    functions: list[dict[str, Any]] = field(default_factory=list)
    usage_examples: list[str] = field(default_factory=list)
    raw_readme: str | None = None


# ---------------------------------------------------------------------------
# Knowledge Harvester
# ---------------------------------------------------------------------------


class KnowledgeHarvester:
    """Harvests and compresses PyPI package documentation.

    Usage::

        harvester = KnowledgeHarvester(project_root=Path.cwd())
        info = harvester.harvest("flask")
        harvester.save(info)  # → .ai-knowledge/libs/flask.md
    """

    def __init__(self, project_root: Path | None = None) -> None:
        """Initialise the harvester.

        Args:
            project_root: The project root directory. If ``None``, uses ``Path.cwd()``.
        """
        self._root = project_root or Path.cwd()
        self._output_dir = self._root / AI_KNOWLEDGE_DIR

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def harvest(self, package_name: str, version: str | None = None) -> PackageInfo | None:
        """Fetch and compress package documentation from PyPI.

        Steps:
        1. GET ``pypi.org/pypi/{package}/json``
        2. Extract info, summary, and README from response.
        3. Parse README (handle both reStructuredText and Markdown).
        4. Extract top-level modules via ``top_level.txt`` or import heuristics.
        5. Compress into ``PackageInfo``.

        Args:
            package_name: The PyPI package name (e.g. ``"flask"``).
            version:      Optional specific version. ``None`` = latest.

        Returns:
            ``PackageInfo`` with compressed documentation, or ``None`` on failure.
        """
        data = self._fetch_pypi_json(package_name, version)
        if data is None:
            return None

        info = data.get("info", {})
        if not info:
            return None

        # Extract basic info
        name = info.get("name", package_name)
        version_str = info.get("version", "unknown")
        summary = info.get("summary", "") or ""

        # Extract README
        readme = info.get("description", "") or ""
        readme_content_type = info.get("description_content_type", "text/markdown")

        # Truncate README if too long
        if len(readme) > _MAX_README_CHARS:
            readme = readme[:_MAX_README_CHARS] + "\n... [truncated]"

        # Extract usage examples from README
        usage_examples = self._extract_usage_examples(readme)

        # Try to extract top-level modules
        top_level = self._extract_top_level_modules(data)

        # Compress README
        compressed_readme = self._compress_readme(readme)

        return PackageInfo(
            name=name,
            version=version_str,
            summary=summary,
            top_level_modules=top_level,
            usage_examples=usage_examples,
            raw_readme=compressed_readme,
        )

    def save(self, info: PackageInfo) -> Path:
        """Save compressed documentation as a markdown file.

        The file is saved to ``.ai-knowledge/libs/{package_name}.md``.

        Args:
            info: The ``PackageInfo`` to save.

        Returns:
            The path to the saved file.
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        path = self._output_dir / f"{info.name.lower()}.md"

        content = self._format_markdown(info)
        path.write_text(content, encoding="utf-8")

        return path

    # ------------------------------------------------------------------
    # PyPI API
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_pypi_json(package: str, version: str | None = None) -> dict[str, Any] | None:
        """Call PyPI JSON API for package metadata.

        Args:
            package: The PyPI package name.
            version: Optional version string. ``None`` = latest.

        Returns:
            Parsed JSON response dict, or ``None`` on failure.
        """
        if version:
            url = f"https://pypi.org/pypi/{package}/{version}/json"
        else:
            url = f"https://pypi.org/pypi/{package}/json"

        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    # ------------------------------------------------------------------
    # README Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_usage_examples(readme: str) -> list[str]:
        """Extract Python code blocks from README.

        Uses regex to find `` ```python ... ``` `` blocks.
        Limits to first 3 examples, each max 30 lines.

        Args:
            readme: The raw README text.

        Returns:
            List of extracted code examples.
        """
        examples: list[str] = []
        # Match ```python ... ``` blocks
        pattern = re.compile(
            r"```python\s*\n(.*?)\n```",
            re.DOTALL,
        )
        for match in pattern.finditer(readme):
            code = match.group(1).strip()
            # Limit lines per example
            lines = code.split("\n")
            if len(lines) > _MAX_EXAMPLE_LINES:
                code = "\n".join(lines[:_MAX_EXAMPLE_LINES]) + "\n# ... [truncated]"
            examples.append(code)
            if len(examples) >= _MAX_EXAMPLES:
                break

        # Fallback: if no ```python blocks, look for >>> (doctest) blocks
        if not examples:
            doctest_pattern = re.compile(r"^>>>\s*.*$", re.MULTILINE)
            doctest_matches = doctest_pattern.findall(readme)
            if doctest_matches:
                examples.append("\n".join(doctest_matches[:_MAX_EXAMPLE_LINES]))

        return examples

    @staticmethod
    def _compress_readme(readme: str, max_chars: int = _MAX_DOC_CHARS) -> str:
        """Compress README to essential parts.

        - Remove HTML tags.
        - Keep only headings, lists, and code blocks.
        - Truncate to max_chars.

        Args:
            readme:   The raw README text.
            max_chars: Maximum characters in the output.

        Returns:
            Compressed README text.
        """
        if not readme:
            return ""

        # Remove HTML tags
        text = re.sub(r"<[^>]+>", "", readme)

        # Keep only meaningful lines
        important_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            # Keep headings, list items, and code-like lines
            if (
                stripped.startswith("#")  # Markdown headings
                or stripped.startswith("-")  # List items
                or stripped.startswith("*")
                or stripped.startswith("1.")
                or stripped.startswith("```")  # Code blocks
                or stripped.startswith(">>>")  # Doctests
                or (stripped and stripped[0].isupper() and len(stripped) > 20)  # Sentences
            ):
                important_lines.append(stripped)

        compressed = "\n".join(important_lines)
        if len(compressed) > max_chars:
            compressed = compressed[:max_chars] + "\n... [truncated]"

        return compressed

    @staticmethod
    def _extract_top_level_modules(data: dict[str, Any]) -> list[str]:
        """Extract top-level module names from PyPI response.

        Checks the ``top_level`` list if available, otherwise infers
        from the package name and URL.

        Args:
            data: The full PyPI JSON response.

        Returns:
            List of top-level module names.
        """
        # Direct top_level list from API
        top_level = data.get("top_level", []) or []
        if top_level:
            return sorted(top_level)

        # Fallback: infer from package name
        info = data.get("info", {}) or {}
        name = info.get("name", "")
        if name:
            # Common pattern: package name is also the top-level module
            return [name.replace("-", "_").replace(".", "/")]

        return []

    # ------------------------------------------------------------------
    # Markdown Formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_markdown(info: PackageInfo) -> str:
        """Format PackageInfo as compressed markdown.

        The format is intentionally minimal to prevent context bloom:
        - One-liner summary
        - Top-level modules list
        - Key classes/functions (when available from README analysis)
        - Usage examples (up to 3)

        Args:
            info: The ``PackageInfo`` to format.

        Returns:
            Formatted markdown string.
        """
        lines: list[str] = [
            f"# {info.name} v{info.version}",
            "",
            info.summary,
            "",
        ]

        # Top-level modules
        if info.top_level_modules:
            lines.append("## Top-Level Modules")
            for mod in info.top_level_modules:
                lines.append(f"- `{mod}`")
            lines.append("")

        # Usage examples
        if info.usage_examples:
            lines.append("## Usage Examples")
            for example in info.usage_examples:
                lines.append("```python")
                lines.append(example)
                lines.append("```")
                lines.append("")

        # Compressed README
        if info.raw_readme:
            lines.append("## README Summary")
            lines.append(info.raw_readme)
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Knowledge Base Search (used by ManagerAgent)
# ---------------------------------------------------------------------------


def search_knowledge_base(
    query: str,
    knowledge_dir: Path | None = None,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Search ``.ai-knowledge/libs/`` for relevant package documentation.

    Deterministic keyword/BM25 search — no embeddings, no LLM calls.
    Because harvested docs are cleanly organized by package/module name,
    error messages like ``AttributeError: module 'fastapi' has no attribute 'X'``
    provide clear deterministic search targets.

    Args:
        query:         The search query (e.g., task goal + constraints).
        knowledge_dir: Path to the knowledge directory. Defaults to
                       ``.ai-knowledge/libs/`` relative to CWD.
        top_k:         Maximum number of results to return.

    Returns:
        List of dicts with keys: ``package``, ``relevance``, ``excerpt``, ``filepath``.
    """
    import math

    kd = knowledge_dir or (Path.cwd() / AI_KNOWLEDGE_DIR)
    if not kd.exists():
        return []

    results: list[dict[str, Any]] = []
    query_tokens = query.lower().split()
    all_md_files = sorted(kd.glob("*.md"))
    total_docs = len(all_md_files)

    if not query_tokens or total_docs == 0:
        return []

    for md_file in all_md_files:
        content = md_file.read_text(encoding="utf-8")
        first_line = content.split("\n")[0] if content else ""
        package_name = first_line.replace("# ", "").split(" v")[0]

        # BM25-inspired scoring: term frequency * inverse document frequency
        content_lower = content.lower()
        word_count = max(len(content_lower.split()), 1)
        score = 0.0
        for token in query_tokens:
            tf = content_lower.count(token) / word_count
            # Simplified BM25 IDF: log(1 + (N - df + 0.5) / (df + 0.5))
            # For single document (df=1): log(1 + (1-1+0.5)/(1+0.5)) = log(1 + 0.333) > 0
            # For df < N: always positive
            df = total_docs  # document frequency approximated as total docs
            idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            score += tf * idf

        if score > 0:
            # Extract first meaningful section as excerpt
            sections = content.split("## ")
            excerpt = sections[1][:300] if len(sections) > 1 else content[:300]

            results.append(
                {
                    "package": package_name,
                    "relevance": round(score, 4),
                    "excerpt": excerpt,
                    "filepath": str(md_file),
                }
            )

    results.sort(key=lambda r: r["relevance"], reverse=True)
    return results[:top_k]
