"""Tests for knowledge harvester and RAG search — Phase 3, Priority 3."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ekp_forge.knowledge.harvester import (
    KnowledgeHarvester,
    PackageInfo,
    search_knowledge_base,
)


# ---------------------------------------------------------------------------
# PackageInfo
# ---------------------------------------------------------------------------


class TestPackageInfo:
    def test_default_values(self) -> None:
        info = PackageInfo()
        assert info.name == ""
        assert info.version == ""
        assert info.summary == ""
        assert info.top_level_modules == []
        assert info.classes == []
        assert info.functions == []
        assert info.usage_examples == []

    def test_with_data(self) -> None:
        info = PackageInfo(
            name="flask",
            version="3.0.0",
            summary="A simple WSGI framework",
            top_level_modules=["flask"],
            usage_examples=["from flask import Flask"],
        )
        assert info.name == "flask"
        assert info.version == "3.0.0"


# ---------------------------------------------------------------------------
# KnowledgeHarvester — README Parsing
# ---------------------------------------------------------------------------


class TestKnowledgeHarvesterReadme:
    """Test README parsing without network calls."""

    def test_extract_usage_examples_python_blocks(self) -> None:
        """Extract ```python ... ``` blocks from README."""
        readme = """# My Package

Some text.

```python
from my_pkg import foo
foo.do_something()
```

More text.

```python
result = bar(42)
print(result)
```
"""
        examples = KnowledgeHarvester._extract_usage_examples(readme)
        assert len(examples) == 2
        assert "from my_pkg import foo" in examples[0]
        assert "result = bar(42)" in examples[1]

    def test_extract_usage_examples_limit(self) -> None:
        """Should not exceed _MAX_EXAMPLES (3)."""
        readme = "\n\n```python\nprint(1)\n```\n" * 10
        examples = KnowledgeHarvester._extract_usage_examples(readme)
        assert len(examples) <= 3

    def test_extract_usage_examples_line_limit(self) -> None:
        """Long examples should be truncated."""
        many_lines = "\n".join(f"print({i})" for i in range(50))
        readme = f"```python\n{many_lines}\n```"
        examples = KnowledgeHarvester._extract_usage_examples(readme)
        if examples:
            lines = examples[0].split("\n")
            assert len(lines) <= 32  # 30 + truncated note + blank

    def test_extract_usage_examples_no_blocks(self) -> None:
        """No code blocks returns empty list."""
        examples = KnowledgeHarvester._extract_usage_examples("Just plain text.")
        assert examples == []

    def test_compress_readme_removes_html(self) -> None:
        """HTML tags should be removed."""
        readme = "<h1>Title and a longer description here</h1><p>Some <b>bold</b> text here for testing</p>"
        compressed = KnowledgeHarvester._compress_readme(readme)
        assert "<h1>" not in compressed
        assert "<b>" not in compressed
        # The text "Title and a longer" starts with uppercase and is > 20 chars
        assert "Title" in compressed

    def test_compress_readme_keeps_headings(self) -> None:
        """Markdown headings should be preserved."""
        readme = "# Title\n\nSome description\n\n## Subsection\n\nDetails here."
        compressed = KnowledgeHarvester._compress_readme(readme)
        assert "# Title" in compressed
        assert "## Subsection" in compressed

    def test_compress_readme_empty(self) -> None:
        """Empty README returns empty string."""
        assert KnowledgeHarvester._compress_readme("") == ""

    def test_compress_readme_truncates(self) -> None:
        """Very long README should be truncated."""
        long_text = "# Heading\n\n" + "word " * 10000
        compressed = KnowledgeHarvester._compress_readme(long_text, max_chars=100)
        assert len(compressed) <= 150  # 100 chars + truncation note


# ---------------------------------------------------------------------------
# KnowledgeHarvester — PyPI API (mocked)
# ---------------------------------------------------------------------------


class TestKnowledgeHarvesterPyPI:
    def test_fetch_pypi_json_invalid_package(self) -> None:
        """Non-existent package should return None."""
        result = KnowledgeHarvester._fetch_pypi_json("this-package-definitely-does-not-exist-12345")
        assert result is None

    def test_fetch_pypi_json_valid_package(self) -> None:
        """Known package should return metadata."""
        result = KnowledgeHarvester._fetch_pypi_json("requests")
        if result is not None:  # Network may be unavailable
            assert "info" in result
            info = result["info"]
            assert info.get("name", "").lower() == "requests"

    def test_extract_top_level_modules(self) -> None:
        """Top-level modules should be extracted from API response."""
        data = {
            "top_level": ["requests", "requests_ftp"],
            "info": {"name": "requests"},
        }
        modules = KnowledgeHarvester._extract_top_level_modules(data)
        assert "requests" in modules
        assert "requests_ftp" in modules

    def test_extract_top_level_modules_fallback(self) -> None:
        """Fallback should use package name."""
        data = {"info": {"name": "flask"}}
        modules = KnowledgeHarvester._extract_top_level_modules(data)
        assert "flask" in modules

    def test_extract_top_level_modules_empty(self) -> None:
        """Empty response returns empty list."""
        assert KnowledgeHarvester._extract_top_level_modules({}) == []


# ---------------------------------------------------------------------------
# KnowledgeHarvester — Markdown Formatting
# ---------------------------------------------------------------------------


class TestKnowledgeHarvesterFormatting:
    def test_format_markdown_basic(self) -> None:
        """Basic PackageInfo produces valid markdown."""
        info = PackageInfo(
            name="flask",
            version="3.0.0",
            summary="A simple WSGI framework",
            top_level_modules=["flask"],
            usage_examples=["from flask import Flask\napp = Flask(__name__)"],
        )
        content = KnowledgeHarvester._format_markdown(info)

        assert "# flask v3.0.0" in content
        assert "flask" in content
        assert "from flask import Flask" in content

    def test_format_markdown_no_examples(self) -> None:
        """No usage examples should still produce valid markdown."""
        info = PackageInfo(name="pkg", version="1.0", summary="Test")
        content = KnowledgeHarvester._format_markdown(info)
        assert "# pkg v1.0" in content
        assert "Test" in content

    def test_save_creates_file(self, tmp_path: Path) -> None:
        """Save should write a file to the expected location."""
        harvester = KnowledgeHarvester(project_root=tmp_path)
        info = PackageInfo(
            name="testpkg",
            version="1.0",
            summary="Test package",
        )
        saved_path = harvester.save(info)

        assert saved_path.exists()
        assert saved_path.name == "testpkg.md"
        assert "libs" in str(saved_path)
        content = saved_path.read_text()
        assert "# testpkg v1.0" in content


# ---------------------------------------------------------------------------
# Knowledge Base Search
# ---------------------------------------------------------------------------


class TestKnowledgeBaseSearch:
    def test_search_empty_directory(self, tmp_path: Path) -> None:
        """Empty knowledge directory returns empty results."""
        results = search_knowledge_base("flask", knowledge_dir=tmp_path)
        assert results == []

    def test_search_no_match(self, tmp_path: Path) -> None:
        """No matching documents returns empty results."""
        # Create files directly in knowledge_dir (not in subdirectory)
        (tmp_path / "flask.md").write_text("# flask v3.0.0\n\nSome flask docs")
        (tmp_path / "numpy.md").write_text("# numpy v1.24\n\nSome numpy docs")

        results = search_knowledge_base("zzzzz_nonexistent", knowledge_dir=tmp_path)
        assert results == []

    def test_search_finds_relevant(self, tmp_path: Path) -> None:
        """Relevant query should find matching packages."""
        (tmp_path / "flask.md").write_text("# flask v3.0.0\n\nFlask is a WSGI web framework")
        (tmp_path / "numpy.md").write_text("# numpy v1.24\n\nNumPy is for numerical computing")

        results = search_knowledge_base("web framework", knowledge_dir=tmp_path)
        assert len(results) >= 1
        # Flask should be more relevant than NumPy for "web framework"
        flask_results = [r for r in results if "flask" in r["package"].lower()]
        assert len(flask_results) >= 1

    def test_search_top_k_limit(self, tmp_path: Path) -> None:
        """top_k parameter limits results."""
        for i in range(10):
            (tmp_path / f"pkg{i}.md").write_text(f"# pkg{i} v1.0\n\nCommon keyword docs")

        results = search_knowledge_base("common keyword", knowledge_dir=tmp_path, top_k=3)
        assert len(results) <= 3

    def test_search_result_structure(self, tmp_path: Path) -> None:
        """Results should have the correct structure."""
        (tmp_path / "flask.md").write_text("# flask v3.0.0\n\nSome documentation")

        results = search_knowledge_base("flask", knowledge_dir=tmp_path)
        assert len(results) >= 1
        result = results[0]
        assert "package" in result
        assert "relevance" in result
        assert "excerpt" in result
        assert "filepath" in result


# ---------------------------------------------------------------------------
# Contract Extension
# ---------------------------------------------------------------------------


class TestWorkerContractKnowledgeContext:
    def test_knowledge_context_default_empty(self) -> None:
        """WorkerContract.knowledge_context should default to empty string."""
        from ekp_forge.schemas.contract import WorkerContract

        contract = WorkerContract(
            contract_id="C-20260627000000-abcdef",
            objective="Test objective",
            target_files=["src/test.py"],
        )
        assert contract.knowledge_context == ""

    def test_knowledge_context_with_value(self) -> None:
        """WorkerContract should accept knowledge_context."""
        from ekp_forge.schemas.contract import WorkerContract

        contract = WorkerContract(
            contract_id="C-20260627000000-abcdef",
            objective="Test objective",
            target_files=["src/test.py"],
            knowledge_context="## flask v3.0.0\nFlask is a WSGI framework\nUsage: `from flask import Flask`",
        )
        assert contract.knowledge_context != ""
        assert "flask" in contract.knowledge_context.lower()
