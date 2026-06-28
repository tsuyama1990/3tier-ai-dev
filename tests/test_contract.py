"""Tests for Phase 2 Pydantic data models: WorkerContract, Diagnostic, FixTask."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from ekp_forge.schemas.contract import (
    CATEGORY_PRIORITY,
    Diagnostic,
    DiagnosticCategory,
    DiagnosticSeverity,
    FixTask,
    WorkerContract,
    diagnostic_priority,
    filter_auto_fixable,
    group_diagnostics_by_priority,
)


# ===================================================================
# WorkerContract
# ===================================================================


class TestWorkerContract:
    def test_valid_minimal(self) -> None:
        """Minimal valid WorkerContract."""
        contract = WorkerContract(
            contract_id="C-20260627000000-abcdef",
            objective="Implement user auth middleware",
            target_files=["src/middleware/auth.py"],
        )
        assert contract.contract_id == "C-20260627000000-abcdef"
        assert contract.objective == "Implement user auth middleware"
        assert contract.target_files == ["src/middleware/auth.py"]
        assert contract.editable_symbols == []
        assert contract.forbidden_symbols == []
        assert contract.acceptance_tests == []
        assert contract.implementation_steps == []
        assert contract.local_design_freedom == "none"

    def test_valid_full(self) -> None:
        """WorkerContract with all fields populated."""
        contract = WorkerContract(
            contract_id="C-20260627000000-123456",
            objective="Add input validation to login form",
            target_files=["src/forms/login.py"],
            editable_symbols=["validate_email", "validate_password"],
            forbidden_symbols=["authenticate_user", "db_session"],
            acceptance_tests=["tests/test_login.py::test_email_validation"],
            implementation_steps=[
                "Add validate_email function",
                "Add validate_password function",
            ],
            local_design_freedom="within_file",
        )
        assert contract.local_design_freedom == "within_file"
        assert len(contract.implementation_steps) == 2

    def test_invalid_contract_id_pattern(self) -> None:
        """Contract ID must match C-YYYYMMDDHHMMSS-hex6 pattern."""
        with pytest.raises(ValidationError):
            WorkerContract(
                contract_id="invalid-id",
                objective="Test",
                target_files=["test.py"],
            )

    def test_empty_objective(self) -> None:
        """Objective must not be empty."""
        with pytest.raises(ValidationError):
            WorkerContract(
                contract_id="C-20260627000000-abcdef",
                objective="",
                target_files=["test.py"],
            )

    def test_empty_target_files(self) -> None:
        """At least one target file is required."""
        with pytest.raises(ValidationError):
            WorkerContract(
                contract_id="C-20260627000000-abcdef",
                objective="Test",
                target_files=[],
            )

    def test_invalid_design_freedom(self) -> None:
        """local_design_freedom must be 'none' or 'within_file'."""
        with pytest.raises(ValidationError):
            WorkerContract(
                contract_id="C-20260627000000-abcdef",
                objective="Test",
                target_files=["test.py"],
                local_design_freedom="unlimited",  # type: ignore[typeddict-item]
            )

    def test_frozen_immutability(self) -> None:
        """WorkerContract is frozen (immutable)."""
        contract = WorkerContract(
            contract_id="C-20260627000000-abcdef",
            objective="Test",
            target_files=["test.py"],
        )
        with pytest.raises(ValidationError):
            contract.objective = "Changed"  # type: ignore[misc]


# ===================================================================
# Diagnostic (Verification IR)
# ===================================================================


class TestDiagnostic:
    def test_valid_ruff_diagnostic(self) -> None:
        """Minimal valid ruff Diagnostic."""
        d = Diagnostic(
            tool="ruff",
            severity=DiagnosticSeverity.ERROR,
            file="src/main.py",
            line=42,
            code="F821",
            message="Undefined name 'x'",
            category=DiagnosticCategory.UNDEFINED_NAME,
        )
        assert d.tool == "ruff"
        assert d.severity == DiagnosticSeverity.ERROR
        assert d.file == "src/main.py"
        assert d.line == 42
        assert d.code == "F821"
        assert d.message == "Undefined name 'x'"
        assert d.category == DiagnosticCategory.UNDEFINED_NAME
        assert d.expected is None
        assert d.actual is None

    def test_valid_mypy_diagnostic(self) -> None:
        """Minimal valid mypy Diagnostic."""
        d = Diagnostic(
            tool="mypy",
            severity=DiagnosticSeverity.ERROR,
            file="src/main.py",
            line=10,
            code="mypy-arg-type",
            message="Argument 1 has incompatible type",
            category=DiagnosticCategory.TYPE_MISMATCH,
        )
        assert d.tool == "mypy"

    def test_valid_pytest_diagnostic(self) -> None:
        """Pytest Diagnostic with expected/actual values."""
        d = Diagnostic(
            tool="pytest",
            severity=DiagnosticSeverity.ERROR,
            file="tests/test_main.py",
            code="AssertionError",
            message="AssertionError: assert 1 == 2",
            category=DiagnosticCategory.WRONG_RETURN_VALUE,
            expected="2",
            actual="1",
        )
        assert d.expected == "2"
        assert d.actual == "1"

    def test_invalid_tool_value(self) -> None:
        """Tool must be one of ruff/mypy/pytest/gatekeeper."""
        with pytest.raises(ValidationError):
            Diagnostic(
                tool="flake8",  # type: ignore[arg-type]
                severity=DiagnosticSeverity.ERROR,
                file="test.py",
                message="test",
            )

    def test_empty_message(self) -> None:
        """Message must not be empty."""
        with pytest.raises(ValidationError):
            Diagnostic(
                tool="ruff",
                severity=DiagnosticSeverity.ERROR,
                file="test.py",
                message="",
            )

    def test_frozen_immutability(self) -> None:
        """Diagnostic is frozen (immutable)."""
        d = Diagnostic(
            tool="ruff",
            severity=DiagnosticSeverity.ERROR,
            file="test.py",
            message="test",
        )
        with pytest.raises(ValidationError):
            d.message = "Changed"  # type: ignore[misc]


# ===================================================================
# FixTask
# ===================================================================


class TestFixTask:
    @pytest.fixture
    def contract(self) -> WorkerContract:
        return WorkerContract(
            contract_id="C-20260627000000-abcdef",
            objective="Fix auth middleware",
            target_files=["src/middleware/auth.py"],
        )

    @pytest.fixture
    def diagnostics(self) -> list[Diagnostic]:
        return [
            Diagnostic(
                tool="ruff",
                severity=DiagnosticSeverity.ERROR,
                file="src/middleware/auth.py",
                line=15,
                code="F821",
                message="Undefined name 'token'",
                category=DiagnosticCategory.UNDEFINED_NAME,
            ),
        ]

    def test_valid_fix_task(self, contract: WorkerContract, diagnostics: list[Diagnostic]) -> None:
        """Minimal valid FixTask."""
        task = FixTask(
            task_id="FT-20260627000000-123456",
            contract=contract,
            diagnostics=diagnostics,
            priority=2,
            instruction="Fix undefined name 'token' in auth.py",
        )
        assert task.task_id.startswith("FT-")
        assert task.priority == 2
        assert len(task.diagnostics) == 1

    def test_empty_diagnostics_raises(self, contract: WorkerContract) -> None:
        """At least one diagnostic is required."""
        with pytest.raises(ValidationError):
            FixTask(
                task_id="FT-20260627000000-123456",
                contract=contract,
                diagnostics=[],
                priority=1,
                instruction="Fix issues",
            )

    def test_invalid_priority_range(self, contract: WorkerContract, diagnostics: list[Diagnostic]) -> None:
        """Priority must be 1-4."""
        with pytest.raises(ValidationError):
            FixTask(
                task_id="FT-20260627000000-123456",
                contract=contract,
                diagnostics=diagnostics,
                priority=5,
                instruction="Fix issues",
            )

    def test_invalid_task_id(self, contract: WorkerContract, diagnostics: list[Diagnostic]) -> None:
        """Task ID must match FT- pattern."""
        with pytest.raises(ValidationError):
            FixTask(
                task_id="invalid",
                contract=contract,
                diagnostics=diagnostics,
                priority=1,
                instruction="Fix issues",
            )


# ===================================================================
# Helper functions
# ===================================================================


class TestHelpers:
    def test_diagnostic_priority(self) -> None:
        """diagnostic_priority returns correct priority for each category."""
        cat_map = {
            DiagnosticCategory.SYNTAX: 1,
            DiagnosticCategory.IMPORT: 2,
            DiagnosticCategory.TYPE_MISMATCH: 3,
            DiagnosticCategory.TEST_FAILURE: 4,
            DiagnosticCategory.OTHER: 4,
        }
        for cat, expected_pri in cat_map.items():
            d = Diagnostic(
                tool="ruff", severity=DiagnosticSeverity.ERROR,
                file="test.py", message="test", category=cat,
            )
            assert diagnostic_priority(d) == expected_pri, f"Failed for {cat}"

    def test_group_diagnostics_by_priority(self) -> None:
        """Diagnostics are correctly grouped by priority level."""
        d1 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="a.py", message="syntax", category=DiagnosticCategory.SYNTAX,
        )
        d2 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="b.py", message="import", category=DiagnosticCategory.IMPORT,
        )
        d3 = Diagnostic(
            tool="mypy", severity=DiagnosticSeverity.ERROR,
            file="c.py", message="type", category=DiagnosticCategory.TYPE_MISMATCH,
        )
        grouped = group_diagnostics_by_priority([d1, d2, d3])
        assert len(grouped[1]) == 1  # syntax
        assert len(grouped[2]) == 1  # import
        assert len(grouped[3]) == 1  # type
        assert len(grouped[4]) == 0  # none

    def test_filter_auto_fixable(self) -> None:
        """Auto-fixable diagnostics (formatting, unused import) are filtered out."""
        d1 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.WARNING,
            file="a.py", message="format", category=DiagnosticCategory.FORMATTING,
        )
        d2 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.WARNING,
            file="b.py", message="unused", category=DiagnosticCategory.UNUSED_IMPORT,
        )
        d3 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="c.py", message="syntax", category=DiagnosticCategory.SYNTAX,
        )
        filtered = filter_auto_fixable([d1, d2, d3])
        assert len(filtered) == 1
        assert filtered[0].category == DiagnosticCategory.SYNTAX
