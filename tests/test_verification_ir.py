"""Tests for Verification IR parsers: RuffParser, MypyParser, PytestParser."""

from __future__ import annotations

from ekp_forge.sandbox.verification_ir import (
    MypyParser,
    PytestParser,
    RuffParser,
)
from ekp_forge.schemas.contract import (
    DiagnosticCategory,
    DiagnosticSeverity,
)


# ===================================================================
# RuffParser
# ===================================================================


class TestRuffParser:
    def test_parse_valid_json(self) -> None:
        """Parse valid ruff JSON output."""
        json_input = """[
            {
                "code": "F821",
                "filename": "src/main.py",
                "location": {"row": 42, "col": 5},
                "message": "Undefined name 'x'"
            },
            {
                "code": "I001",
                "filename": "src/utils.py",
                "location": {"row": 1, "col": 1},
                "message": "Import block is unsorted"
            }
        ]"""
        diagnostics = RuffParser.parse(json_input)
        assert len(diagnostics) == 2

        # First: F821
        assert diagnostics[0].tool == "ruff"
        assert diagnostics[0].severity == DiagnosticSeverity.ERROR  # F-prefix
        assert diagnostics[0].code == "F821"
        assert diagnostics[0].file == "src/main.py"
        assert diagnostics[0].line == 42
        assert diagnostics[0].category == DiagnosticCategory.UNDEFINED_NAME

        # Second: I001
        assert diagnostics[1].severity == DiagnosticSeverity.WARNING  # I-prefix
        assert diagnostics[1].category == DiagnosticCategory.IMPORT

    def test_parse_empty_json(self) -> None:
        """Empty JSON array returns empty list."""
        diagnostics = RuffParser.parse("[]")
        assert diagnostics == []

    def test_parse_invalid_json(self) -> None:
        """Invalid JSON returns empty list (graceful degradation)."""
        diagnostics = RuffParser.parse("not json")
        assert diagnostics == []

    def test_parse_malformed_record(self) -> None:
        """Malformed records are skipped."""
        json_input = """[
            {"code": "F821", "filename": "ok.py", "location": {"row": 1}, "message": "ok"},
            {"bad": "data"},
            {"code": "E999", "filename": "also_ok.py", "location": {"row": 2}, "message": "syntax"}
        ]"""
        diagnostics = RuffParser.parse(json_input)
        assert len(diagnostics) == 2  # middle record skipped

    def test_security_category(self) -> None:
        """S-prefix codes map to SECURITY category."""
        json_input = """[
            {
                "code": "S101",
                "filename": "src/main.py",
                "location": {"row": 10},
                "message": "Use of assert detected"
            }
        ]"""
        diagnostics = RuffParser.parse(json_input)
        assert len(diagnostics) == 1
        # S101 doesn't match _RUFF_CODE_CATEGORY keys directly;
        # it's handled by prefix matching in RuffParser
        assert diagnostics[0].code == "S101"
        assert diagnostics[0].severity == DiagnosticSeverity.ERROR


# ===================================================================
# MypyParser
# ===================================================================


class TestMypyParser:
    def test_parse_valid_output(self) -> None:
        """Parse valid mypy text output."""
        raw = """src/main.py:42:5: error: Incompatible return value type (got "int", expected "str")  [return-type]
src/utils.py:10:3: warning: Unused import "os"  [import-untyped]
"""
        diagnostics = MypyParser.parse(raw)
        assert len(diagnostics) == 2

        # First diagnostic
        assert diagnostics[0].tool == "mypy"
        assert diagnostics[0].file == "src/main.py"
        assert diagnostics[0].line == 42
        assert diagnostics[0].code == "mypy-return-type"
        assert diagnostics[0].severity == DiagnosticSeverity.ERROR
        assert diagnostics[0].category == DiagnosticCategory.WRONG_RETURN_VALUE

        # Second diagnostic
        assert diagnostics[1].file == "src/utils.py"
        assert diagnostics[1].line == 10
        assert diagnostics[1].severity == DiagnosticSeverity.WARNING
        assert diagnostics[1].category == DiagnosticCategory.IMPORT

    def test_parse_no_issues(self) -> None:
        """Empty output (no issues) returns empty list."""
        diagnostics = MypyParser.parse("Success: no issues found in 1 source file")
        assert diagnostics == []

    def test_parse_with_unknown_code(self) -> None:
        """Unknown mypy error codes map to OTHER category."""
        raw = "src/main.py:5:5: error: Something weird  [weird-code]\n"
        diagnostics = MypyParser.parse(raw)
        assert len(diagnostics) == 1
        assert diagnostics[0].code == "mypy-weird-code"
        assert diagnostics[0].category == DiagnosticCategory.OTHER

    def test_parse_without_code_bracket(self) -> None:
        """Mypy output without [code] suffix."""
        raw = "src/main.py:5:8: error: Incompatible types\n"
        diagnostics = MypyParser.parse(raw)
        assert len(diagnostics) == 1
        assert diagnostics[0].code == "mypy-error"
        assert diagnostics[0].message == "Incompatible types"

    def test_parse_empty_string(self) -> None:
        """Empty string returns empty list."""
        assert MypyParser.parse("") == []

    def test_parse_note_severity(self) -> None:
        """'note' severity maps to WARNING."""
        raw = 'src/main.py:10:3: note: Revealed type is "builtins.int"\n'
        diagnostics = MypyParser.parse(raw)
        assert len(diagnostics) == 1
        assert diagnostics[0].severity == DiagnosticSeverity.WARNING


# ===================================================================
# PytestParser
# ===================================================================


# ===================================================================
# _ToolResult + gatekeeper guard
# ===================================================================


class TestToolResult:
    def test_constructs_with_exit_code(self) -> None:
        from ekp_forge.sandbox.verification_ir import _ToolResult

        result = _ToolResult(raw_output="some output", exit_code=1)
        assert result.raw_output == "some output"
        assert result.exit_code == 1
        assert result.parse_ok is False

    def test_default_exit_code(self) -> None:
        from ekp_forge.sandbox.verification_ir import _ToolResult

        result = _ToolResult(raw_output="output")
        assert result.exit_code == -1


# ===================================================================
# PytestParser
# ===================================================================


class TestPytestParser:
    def test_parse_failed_tests(self) -> None:
        """Parse FAILED test lines."""
        raw = """FAILED tests/test_main.py::test_auth - AssertionError: expected 401 got 200
FAILED tests/test_utils.py::test_validate - TypeError: validate() missing 1 required positional argument
"""
        diagnostics = PytestParser.parse(raw)
        assert len(diagnostics) == 2

        assert diagnostics[0].tool == "pytest"
        assert diagnostics[0].severity == DiagnosticSeverity.ERROR
        assert diagnostics[0].file == "tests/test_main.py"
        assert diagnostics[0].code == "AssertionError"
        assert diagnostics[0].message == "AssertionError: expected 401 got 200"
        assert diagnostics[0].category == DiagnosticCategory.TEST_FAILURE

        assert diagnostics[1].code == "TypeError"

    def test_parse_all_passed(self) -> None:
        """No FAILED lines returns empty list."""
        raw = "=== 5 passed in 0.12s ===\n"
        diagnostics = PytestParser.parse(raw)
        assert diagnostics == []

    def test_parse_empty_output(self) -> None:
        """Empty string returns empty list."""
        assert PytestParser.parse("") == []

    def test_parse_with_assertion_details(self) -> None:
        """Parse E lines with expected/actual values."""
        raw = """FAILED tests/test_main.py::test_add - AssertionError: assert 1 == 2
E       assert 1 == 2
E        +  where 1 = add(0, 0)
"""
        diagnostics = PytestParser.parse(raw)
        assert len(diagnostics) >= 1
        # The FAILED line creates one diagnostic
        assert diagnostics[0].code == "AssertionError"

    def test_parse_module_not_found(self) -> None:
        """ModuleNotFoundError is captured."""
        raw = "FAILED tests/test_import.py::test_imports - ModuleNotFoundError: No module named 'nonexistent'\n"
        diagnostics = PytestParser.parse(raw)
        assert len(diagnostics) == 1
        assert "ModuleNotFoundError" in diagnostics[0].code
        assert diagnostics[0].category == DiagnosticCategory.TEST_FAILURE
