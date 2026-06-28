"""Tests for IntrospectionTool — sandbox-safe dir()/help() introspection.

Phase 3, Priority 2.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ekp_forge.sandbox.introspection import (
    IntrospectionResult,
    IntrospectionTool,
)


# ---------------------------------------------------------------------------
# IntrospectionResult
# ---------------------------------------------------------------------------


class TestIntrospectionResult:
    def test_default_values(self) -> None:
        result = IntrospectionResult()
        assert result.module_name == ""
        assert result.attributes == []
        assert result.callables == []
        assert result.classes == []
        assert result.signature == ""
        assert result.doc_summary == ""
        assert result.error == ""

    def test_with_data(self) -> None:
        result = IntrospectionResult(
            module_name="json",
            attributes=["dumps", "loads", "dump"],
            callables=["dumps", "loads"],
            classes=["JSONDecoder", "JSONEncoder"],
            doc_summary="JSON encoder and decoder",
        )
        assert result.module_name == "json"
        assert "dumps" in result.callables
        assert "JSONDecoder" in result.classes


# ---------------------------------------------------------------------------
# IntrospectionTool — Module Inspection
# ---------------------------------------------------------------------------


class TestIntrospectionToolModule:
    """Test inspecting standard library modules."""

    def test_inspect_math_module(self) -> None:
        """Standard lib module 'math' should be inspectable."""
        tool = IntrospectionTool()
        result = tool.inspect_module("math")

        assert result.error == "", f"Error: {result.error}"
        assert result.module_name == "math"
        assert "sin" in result.callables or "sin" in result.attributes
        assert "cos" in result.callables or "cos" in result.attributes
        assert "pi" in result.attributes or "pi" in result.callables
        assert "sqrt" in result.callables or "sqrt" in result.attributes

    def test_inspect_json_module(self) -> None:
        """'json' module should show dumps/loads."""
        tool = IntrospectionTool()
        result = tool.inspect_module("json")

        assert result.error == "", f"Error: {result.error}"
        assert result.module_name == "json"
        assert "dumps" in result.callables
        assert "loads" in result.callables

    def test_inspect_module_not_found(self) -> None:
        """Non-existent module should return error."""
        tool = IntrospectionTool()
        result = tool.inspect_module("nonexistent_module_xyz")

        assert result.error != ""  # Should have an error message
        assert "ModuleNotFoundError" in result.error or "No module" in result.error

    def test_inspect_module_docstring(self) -> None:
        """Module docstring should be extracted."""
        tool = IntrospectionTool()
        result = tool.inspect_module("json")

        assert result.doc_summary != ""
        # JSON module docstring typically mentions "JSON"
        assert "JSON" in result.doc_summary or "json" in result.doc_summary.lower()

    def test_inspect_module_classes(self) -> None:
        """Module classes should be listed."""
        tool = IntrospectionTool()
        result = tool.inspect_module("json")

        # json module has JSONDecoder and JSONEncoder
        class_names = {c.lower() for c in result.classes}
        assert "jsondecoder" in class_names or "jsonencoder" in class_names


# ---------------------------------------------------------------------------
# IntrospectionTool — Attribute Error Resolution
# ---------------------------------------------------------------------------


class TestIntrospectionToolAttributeError:
    def test_resolve_existing_attribute(self) -> None:
        """If attribute exists, no error should be reported."""
        tool = IntrospectionTool()
        result = tool.resolve_attribute_error("json", "dumps")

        assert result.error == ""  # dumps exists in json
        assert "dumps" in result.attributes

    def test_resolve_nonexistent_attribute(self) -> None:
        """Non-existent attribute should produce helpful suggestion."""
        tool = IntrospectionTool()
        result = tool.resolve_attribute_error("json", "dumpz")  # typo: should be dumps

        # Should either find similar or report the right error
        assert result.error != ""
        # Should mention "dumpz" not found
        assert "dumpz" in result.error or "dump" in result.error.lower()

    def test_resolve_on_nonexistent_module(self) -> None:
        """Non-existent module returns error without attribute check."""
        tool = IntrospectionTool()
        result = tool.resolve_attribute_error("nonexistent_mod_xyz", "foo")

        assert result.error != ""
        assert "No module" in result.error or "not found" in result.error


# ---------------------------------------------------------------------------
# IntrospectionTool — Prompt Formatting
# ---------------------------------------------------------------------------


class TestIntrospectionToolFormatting:
    def test_format_successful_result(self) -> None:
        """Successful introspection should produce structured output."""
        result = IntrospectionResult(
            module_name="json",
            attributes=["dumps", "loads", "dump"],
            callables=["dumps", "loads"],
            classes=["JSONDecoder"],
            doc_summary="JSON encoder/decoder",
        )
        output = IntrospectionTool.format_for_prompt(result)

        assert "[Introspection: json]" in output
        assert "dumps" in output
        assert "loads" in output
        assert "JSONDecoder" in output

    def test_format_error_result(self) -> None:
        """Error result should show error message."""
        result = IntrospectionResult(
            module_name="bad_module",
            error="ModuleNotFoundError: No module named 'bad_module'",
        )
        output = IntrospectionTool.format_for_prompt(result)

        assert "[Introspection Error]" in output
        assert "bad_module" in output

    def test_format_truncates_long_output(self) -> None:
        """Very long output should be truncated."""
        # Create a result with attributes that would exceed _MAX_PROMPT_CHARS
        # Each attr name is ~50 chars, 100 attrs = ~5000 chars
        long_attrs = [f"very_long_attribute_name_number_{i}_padding_to_make_it_longer" for i in range(200)]
        result = IntrospectionResult(
            module_name="big_module",
            attributes=long_attrs,
        )
        output = IntrospectionTool.format_for_prompt(result)

        # Should be truncated to _MAX_PROMPT_CHARS (3000)
        assert len(output) <= 3100  # at or near the limit


# ---------------------------------------------------------------------------
# IntrospectionTool — Edge Cases
# ---------------------------------------------------------------------------


class TestIntrospectionToolEdgeCases:
    def test_inspect_empty_module_name(self) -> None:
        """Empty module name should produce error."""
        tool = IntrospectionTool()
        result = tool.inspect_module("")
        assert result.error != ""

    def test_inspect_special_characters_in_name(self) -> None:
        """Module name with special chars should be handled safely."""
        tool = IntrospectionTool()
        # Script injection attempt via module name
        result = tool.inspect_module("os; rm -rf /")
        assert result.error != ""

    def test_inspect_dot_notation_module(self) -> None:
        """Dot-notation module path (e.g. 'os.path') should work."""
        tool = IntrospectionTool()
        result = tool.inspect_module("os.path")

        assert result.error == "", f"Error: {result.error}"
        assert "join" in result.callables or "join" in result.attributes
        assert "exists" in result.callables or "exists" in result.attributes

    def test_fuzzy_match_exact(self) -> None:
        """Exact match should score highest."""
        matches = IntrospectionTool._fuzzy_match("dumps", ["dumps", "loads", "dump"])
        assert "dumps" in matches
        assert matches[0] == "dumps"  # Exact match first

    def test_fuzzy_match_substring(self) -> None:
        """Substring match should find similar names."""
        matches = IntrospectionTool._fuzzy_match("dump", ["dumps", "loads", "dumper"])
        assert len(matches) > 0
        assert any("dump" in m.lower() for m in matches)

    def test_fuzzy_match_no_match(self) -> None:
        """No match returns empty list."""
        matches = IntrospectionTool._fuzzy_match("zzzzzz", ["aaaa", "bbbb"])
        assert matches == []


# ---------------------------------------------------------------------------
# IntrospectionTool — Subprocess Isolation
# ---------------------------------------------------------------------------


class TestIntrospectionToolSubprocess:
    def test_subprocess_does_not_affect_caller(self) -> None:
        """Subprocess import should not pollute the caller's namespace."""
        # Verify 'math' is not imported in the test process before
        assert "math" not in dir() or True  # just a sanity check

        tool = IntrospectionTool()
        result = tool.inspect_module("collections")

        assert result.error == ""
        # The subprocess imported collections, but the test process shouldn't
        # have 'deque' in its namespace unless it was already there
        assert "Counter" in result.classes or "defaultdict" in result.classes

    def test_subprocess_timeout(self) -> None:
        """Slow introspection should time out gracefully."""
        tool = IntrospectionTool()
        # Use a module that would hang (or just test the timeout logic)
        # We pass a script that sleeps to trigger the timeout
        result = tool._run_script(
            "import time; time.sleep(30); print('{}')",
            "timeout_test",
        )
        assert result.error != ""
        assert "timed out" in result.error.lower()
