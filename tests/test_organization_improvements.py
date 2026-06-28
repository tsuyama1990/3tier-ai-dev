"""TDD tests for Organizational-Theory-Based Improvements.

This file contains the test cases that drive the implementation of:

1. ``IntegratorAgent`` — safe diff application with global checks and rollback.
2. ``AdversarialReviewer`` — independent edge-case robustness audit (non-blocking).

Run with::

    pytest tests/test_organization_improvements.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from ekp_forge.sandbox.integrator import IntegratorAgent
from ekp_forge.sandbox.adversarial_reviewer import (
    AdversarialReviewer,
    EDGE_CASE_MAP,
)


# ===================================================================
# IntegratorAgent Tests
# ===================================================================


class TestIntegratorRevertOnRegression:
    """Test Case 1: Integrator must revert files on global check failure.

    Objective: Ensure that a regression bug applied to the host repo is
    caught by the Integrator, and the host files are restored to their
    original state.
    """

    @pytest.fixture
    def project_dir(self, tmp_path: Path) -> Path:
        """Set up a temporary project with two compatible Python files."""
        # math_utils.py — defines add(a: int, b: int) -> int
        math_file = tmp_path / "math_utils.py"
        math_file.write_text("def add(a: int, b: int) -> int: return a + b\n")

        # stats.py — calls add() with int arguments
        stats_file = tmp_path / "stats.py"
        stats_file.write_text("from math_utils import add\n\nresult = add(1, 2)\n")

        # pyproject.toml with mypy config (needed for mypy check)
        toml_file = tmp_path / "pyproject.toml"
        toml_file.write_text("[tool.mypy]\nstrict = true\nignore_missing_imports = true\n")

        return tmp_path

    def test_revert_on_type_mismatch_regression(self, project_dir: Path) -> None:
        """Inject a diff that changes add() signature from int to str.

        Expected:
        - integrate() returns False with mypy error in log.
        - math_utils.py is reverted to original int signature.
        """
        integrator = IntegratorAgent(project_root=project_dir)

        # Bad diff: changes type signature of add from int to str
        # This will cause mypy to fail because stats.py still passes int
        bad_diff = (
            "--- a/math_utils.py\n"
            "+++ b/math_utils.py\n"
            "@@ -1 +1 @@\n"
            "-def add(a: int, b: int) -> int: return a + b\n"
            "+def add(a: str, b: str) -> str: return a + b\n"
        )

        success, log = integrator.integrate(bad_diff, ["math_utils.py"])

        # Assert integration failed
        assert not success, "Expected integration to fail on type mismatch"
        assert "error" in log.lower() or "mypy" in log.lower(), f"Expected mypy error in log, got: {log[:500]}"

        # Assert file was reverted to original
        restored = (project_dir / "math_utils.py").read_text()
        assert "a: int" in restored, f"Expected 'a: int' in restored file, got: {restored}"
        assert "b: int" in restored

    def test_success_path(self, project_dir: Path) -> None:
        """A valid diff passes integration and files are updated."""
        integrator = IntegratorAgent(project_root=project_dir)

        # Valid diff: change implementation body without altering signature
        good_diff = (
            "--- a/math_utils.py\n"
            "+++ b/math_utils.py\n"
            "@@ -1 +1 @@\n"
            "-def add(a: int, b: int) -> int: return a + b\n"
            "+def add(a: int, b: int) -> int: return a * b\n"
        )

        success, log = integrator.integrate(good_diff, ["math_utils.py"])

        assert success, f"Expected integration to succeed, got log: {log[:500]}"
        content = (project_dir / "math_utils.py").read_text()
        assert "a * b" in content

    def test_empty_diff_returns_success(self, project_dir: Path) -> None:
        """An empty diff should return success immediately."""
        integrator = IntegratorAgent(project_root=project_dir)
        success, log = integrator.integrate("", [])
        assert success

    def test_backup_restore_on_git_apply_failure(self, project_dir: Path) -> None:
        """When git apply fails, files should remain unchanged."""
        integrator = IntegratorAgent(project_root=project_dir)
        original = (project_dir / "math_utils.py").read_text()

        # Malformed diff that git apply cannot parse
        bad_diff = "this is not a valid diff"

        success, log = integrator.integrate(bad_diff, ["math_utils.py"])
        assert not success
        assert "git apply failed" in log.lower()

        # File should be unchanged
        assert (project_dir / "math_utils.py").read_text() == original


class TestIntegratorBackupMechanism:
    """Unit tests for IntegratorAgent backup/restore internals."""

    def test_backup_and_restore(self, tmp_path: Path) -> None:
        """Verify _backup_files and _restore_backups work correctly."""
        integrator = IntegratorAgent(project_root=tmp_path)

        # Create a file
        test_file = tmp_path / "test.py"
        original = "x = 1\n"
        test_file.write_text(original)

        # Backup
        integrator._backup_files(["test.py"])
        assert len(integrator._backups) == 1
        assert integrator._backups[test_file] == original

        # Modify the file
        test_file.write_text("x = 999\n")

        # Restore
        integrator._restore_backups()
        assert test_file.read_text() == original
        assert len(integrator._backups) == 0  # Cleared after restore


# ===================================================================
# AdversarialReviewer Tests
# ===================================================================


class TestAdversarialWarningsNonBlocking:
    """Test Case 2: AdversarialReviewer warnings do not block pipeline.

    Objective: Verify that adversarial test failures are returned as
    warnings but do not block pipeline execution success.
    """

    def test_division_by_zero_detected(self) -> None:
        """Division function should trigger ZeroDivisionError warning."""
        code = """
def divide(a, b):
    return a / b
"""
        reviewer = AdversarialReviewer()
        ok, report = reviewer.review(None, code)

        # Should detect edge-case crashes (ZeroDivisionError for b=0,
        # TypeError for None, etc.)
        assert not ok, "Expected adversarial review to find edge-case crashes"
        assert "crashed" in report.lower(), f"Expected crash report, got: {report[:500]}"

    def test_robust_code_passes(self) -> None:
        """Robust code that handles all edge cases should pass."""
        code = """
def divide(a: float, b: float) -> float:
    if a is None or b is None:
        return float('nan')
    if b == 0.0:
        return float('inf')
    return a / b
"""
        reviewer = AdversarialReviewer()
        ok, report = reviewer.review(None, code)

        assert ok, f"Expected robust code to pass, got report: {report[:500]}"

    def test_execute_returns_warnings_in_result(self) -> None:
        """AdversarialReviewer.execute() returns warnings in result dict."""
        code = """
def unsafe_get(data, key):
    return data[key]
"""
        reviewer = AdversarialReviewer()
        result = reviewer.execute({"code": code})

        # Status must always be "success" (non-blocking)
        assert result["status"] == "success"

        # If code is unsafe, there should be warnings
        # (None key in dict should raise TypeError or KeyError depending on impl)
        # But even if no warnings, status is still success
        assert "adversarial_warnings" in result
        assert "robust" in result

    def test_no_functions_generates_no_warnings(self) -> None:
        """Code with no functions should pass without warnings."""
        code = "x = 42\n"
        reviewer = AdversarialReviewer()
        ok, report = reviewer.review(None, code)
        assert ok
        assert report == ""


class TestAdversarialEdgeCaseGeneration:
    """Unit tests for edge-case generation logic."""

    def test_simple_int_parameter(self) -> None:
        """Functions with int parameter get int edge cases."""
        code = "def process(count: int) -> None: pass\n"
        reviewer = AdversarialReviewer()
        cases = reviewer._generate_edge_cases(code)
        assert len(cases) > 0

        # First case should involve an int edge case
        func_name, args = cases[0]
        assert func_name == "process"
        # At least one of the edge values should be in the args
        int_edge_values = [v for v, _ in EDGE_CASE_MAP["int"]]
        assert any(arg in int_edge_values for arg in args), f"Expected int edge case in args: {args}"

    def test_optional_parameter_unwrapped(self) -> None:
        """Optional[str] should be treated as str for edge-case generation."""
        code = """
from typing import Optional

def greet(name: Optional[str]) -> str:
    return f"Hello, {name}"
"""
        reviewer = AdversarialReviewer()
        cases = reviewer._generate_edge_cases(code)
        # Should generate cases with str edge values (including None)
        assert len(cases) > 0

    def test_skip_self_parameter(self) -> None:
        """self/cls parameters should be skipped in edge-case generation."""
        code = """
class Calculator:
    def add(self, a: int, b: int) -> int:
        return a + b
"""
        reviewer = AdversarialReviewer()
        cases = reviewer._generate_edge_cases(code)
        # Cases should exist (for a, b parameters)
        assert len(cases) > 0
        # None of the arg lists should include 'self'
        for _, args in cases:
            assert "self" not in args

    def test_resolve_type_name_annotations(self) -> None:
        """_resolve_type_name handles various annotation forms."""
        reviewer = AdversarialReviewer()

        # Simple name
        import ast

        node = ast.Name(id="int")
        assert reviewer._resolve_type_name(node) == "int"

        # Subscript (list[int])
        sub = ast.Subscript(
            value=ast.Name(id="list"),
            slice=ast.Name(id="int"),
        )
        assert reviewer._resolve_type_name(sub) == "list"

        # Optional[int]
        opt = ast.Subscript(
            value=ast.Name(id="Optional"),
            slice=ast.Name(id="int"),
        )
        assert reviewer._resolve_type_name(opt) == "int"

        # None annotation
        assert reviewer._resolve_type_name(None) is None


# ===================================================================
# Pipeline Integration Tests (smoke-level)
# ===================================================================


class TestPipelineNonRegression:
    """Verify that existing pipeline behavior is preserved.

    These tests ensure the new agents integrate without breaking
    the existing run_managed_task flow (tested at the API level).
    """

    def test_integrator_agent_id_and_capabilities(self) -> None:
        """IntegratorAgent declares correct identity."""
        agent = IntegratorAgent()
        assert agent.agent_id == "integrator"
        caps = [c.value for c in agent.capabilities]
        assert "integration" in caps
        assert "verification" in caps

    def test_adversarial_reviewer_agent_id_and_capabilities(self) -> None:
        """AdversarialReviewer declares correct identity."""
        agent = AdversarialReviewer()
        assert agent.agent_id == "adversarial_reviewer"
        caps = [c.value for c in agent.capabilities]
        assert "adversarial_review" in caps
        assert "verification" in caps

    def test_integrator_execute_no_task(self) -> None:
        """IntegratorAgent.execute() handles empty context gracefully."""
        agent = IntegratorAgent()
        result = agent.execute({})
        assert "status" in result
        # No diff to apply → success with skip message
        assert result["status"] == "success"

    def test_adversarial_execute_no_code(self) -> None:
        """AdversarialReviewer.execute() handles empty context gracefully."""
        agent = AdversarialReviewer()
        result = agent.execute({})
        assert result["status"] == "success"
        assert result["adversarial_warnings"] == []
        assert result["robust"] is True
