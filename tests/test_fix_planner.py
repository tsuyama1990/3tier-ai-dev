"""Tests for Fix Planner — prioritisation, FixTask generation, instruction building."""

from __future__ import annotations

import pytest

from ekp_forge.engine.fix_planner import FixPlanner
from ekp_forge.schemas.contract import (
    Diagnostic,
    DiagnosticCategory,
    DiagnosticSeverity,
    WorkerContract,
)


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
def contract() -> WorkerContract:
    return WorkerContract(
        contract_id="C-20260627000000-abcdef",
        objective="Fix user auth middleware",
        target_files=["src/middleware/auth.py"],
        editable_symbols=["validate_token", "get_user"],
        forbidden_symbols=["db_session"],
    )


@pytest.fixture
def planner(contract: WorkerContract) -> FixPlanner:
    return FixPlanner(contract)


@pytest.fixture
def syntax_diag() -> Diagnostic:
    return Diagnostic(
        tool="ruff",
        severity=DiagnosticSeverity.ERROR,
        file="src/middleware/auth.py",
        line=15,
        code="E999",
        message="SyntaxError: invalid syntax",
        category=DiagnosticCategory.SYNTAX,
    )


@pytest.fixture
def import_diag() -> Diagnostic:
    return Diagnostic(
        tool="ruff",
        severity=DiagnosticSeverity.ERROR,
        file="src/middleware/auth.py",
        line=1,
        code="F401",
        message="`os` imported but unused",
        category=DiagnosticCategory.UNUSED_IMPORT,
    )


@pytest.fixture
def type_diag() -> Diagnostic:
    return Diagnostic(
        tool="mypy",
        severity=DiagnosticSeverity.ERROR,
        file="src/middleware/auth.py",
        line=42,
        code="mypy-arg-type",
        message="Argument 1 has incompatible type",
        category=DiagnosticCategory.TYPE_MISMATCH,
    )


@pytest.fixture
def test_diag() -> Diagnostic:
    return Diagnostic(
        tool="pytest",
        severity=DiagnosticSeverity.ERROR,
        file="tests/test_auth.py",
        code="AssertionError",
        message="AssertionError: expected 401 got 200",
        category=DiagnosticCategory.TEST_FAILURE,
    )




# ===================================================================
# FixPlanner.has_work
# ===================================================================


class TestHasWork:
    def test_has_work_with_non_auto_fixable(self, planner: FixPlanner, syntax_diag: Diagnostic) -> None:
        """Non-auto-fixable diagnostics indicate work."""
        assert planner.has_work([syntax_diag]) is True

    def test_no_work_empty(self, planner: FixPlanner) -> None:
        """Empty list = no work."""
        assert planner.has_work([]) is False

    def test_no_work_auto_fixable_only(self, planner: FixPlanner) -> None:
        """Only auto-fixable items = no work (they were already fixed by ruff --fix)."""
        diag = Diagnostic(
            tool="ruff",
            severity=DiagnosticSeverity.WARNING,
            file="test.py",
            message="Unused import",
            category=DiagnosticCategory.UNUSED_IMPORT,
        )
        # filter_auto_fixable removes UNUSED_IMPORT and FORMATTING
        assert planner.has_work([diag]) is False


# ===================================================================
# FixPlanner.plan — priority ordering
# ===================================================================


class TestPlanPriority:
    def test_plan_returns_highest_priority_first(
        self, planner: FixPlanner, type_diag: Diagnostic, syntax_diag: Diagnostic
    ) -> None:
        """Syntax (priority 1) should be returned before Type (priority 3)."""
        tasks = planner.plan([type_diag, syntax_diag])
        assert len(tasks) == 1
        assert tasks[0].priority == 1  # syntax comes first
        assert tasks[0].diagnostics[0].category == DiagnosticCategory.SYNTAX

    def test_plan_returns_all_same_priority(
        self, planner: FixPlanner, syntax_diag: Diagnostic
    ) -> None:
        """All diagnostics at the same priority are bundled."""
        d2 = Diagnostic(
            tool="ruff",
            severity=DiagnosticSeverity.ERROR,
            file="src/main.py",
            line=20,
            code="E999",
            message="Syntax error",
            category=DiagnosticCategory.SYNTAX,
        )
        tasks = planner.plan([syntax_diag, d2])
        assert len(tasks) == 1
        assert len(tasks[0].diagnostics) == 2

    def test_plan_empty_diagnostics(self, planner: FixPlanner) -> None:
        """Empty diagnostics = no tasks."""
        assert planner.plan([]) == []

    def test_plan_auto_fixable_only(self, planner: FixPlanner) -> None:
        """Only auto-fixable diagnostics = no tasks."""
        diag = Diagnostic(
            tool="ruff",
            severity=DiagnosticSeverity.WARNING,
            file="test.py",
            message="Formatting issue",
            category=DiagnosticCategory.FORMATTING,
        )
        assert planner.plan([diag]) == []

    def test_plan_with_mixed_priorities(
        self, planner: FixPlanner, syntax_diag: Diagnostic, type_diag: Diagnostic, test_diag: Diagnostic
    ) -> None:
        """Mixed priorities: only the highest priority group is returned."""
        tasks = planner.plan([test_diag, type_diag, syntax_diag])
        assert len(tasks) == 1
        assert tasks[0].priority == 1  # syntax
        # type and test failures are not included in this task
        assert len(tasks[0].diagnostics) == 1


# ===================================================================
# FixPlanner.plan — instruction building
# ===================================================================


class TestInstructionBuilding:
    def test_instruction_contains_contract_id(self, planner: FixPlanner, syntax_diag: Diagnostic) -> None:
        """Instruction references the contract ID."""
        tasks = planner.plan([syntax_diag])
        assert len(tasks) == 1
        assert tasks[0].contract.contract_id in tasks[0].instruction

    def test_instruction_contains_file_and_error(self, planner: FixPlanner, syntax_diag: Diagnostic) -> None:
        """Instruction includes the file path and error message."""
        tasks = planner.plan([syntax_diag])
        assert "src/middleware/auth.py" in tasks[0].instruction
        assert "SyntaxError" in tasks[0].instruction

    def test_instruction_under_2000_chars(self, planner: FixPlanner) -> None:
        """Instruction must not exceed 2000 characters (FixTask constraint)."""
        # Generate many diagnostics to test truncation
        many_diags = [
            Diagnostic(
                tool="ruff",
                severity=DiagnosticSeverity.ERROR,
                file=f"src/mod{i}.py",
                message=f"Error number {i} with some additional context to fill up space",
                category=DiagnosticCategory.SYNTAX,
            )
            for i in range(50)
        ]
        tasks = planner.plan(many_diags)
        if tasks:
            assert len(tasks[0].instruction) <= 2000


# ===================================================================
# FixPlanner has correct contract reference
# ===================================================================


class TestContractReference:
    def test_fix_task_references_contract(self, planner: FixPlanner, syntax_diag: Diagnostic) -> None:
        """FixTask.contract must match the planner's contract."""
        tasks = planner.plan([syntax_diag])
        assert len(tasks) == 1
        assert tasks[0].contract.contract_id == planner.contract.contract_id

    def test_contract_property(self, planner: FixPlanner, contract: WorkerContract) -> None:
        """planner.contract returns the original contract."""
        assert planner.contract is contract


# ===================================================================
# FixHistory — stateless context bridge (Fix 1)
# ===================================================================


class TestFixHistory:
    def test_history_accumulates(self, planner: FixPlanner, syntax_diag: Diagnostic) -> None:
        """Each plan() call appends to history."""
        assert planner.history == []
        planner.plan([syntax_diag])
        assert len(planner.history) == 1
        assert planner.history[0].priority == 1

    def test_history_contains_files_and_codes(self, planner: FixPlanner, syntax_diag: Diagnostic) -> None:
        """History entry records files and error codes."""
        planner.plan([syntax_diag])
        entry = planner.history[0]
        assert "src/middleware/auth.py" in entry.files
        assert "E999" in entry.error_codes

    def test_multiple_history_entries(self, planner: FixPlanner) -> None:
        """Multiple plan() calls accumulate multiple history entries."""
        diag1 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="a.py", line=1, code="E999", message="syntax",
            category=DiagnosticCategory.SYNTAX,
        )
        diag2 = Diagnostic(
            tool="mypy", severity=DiagnosticSeverity.ERROR,
            file="b.py", line=10, code="mypy-arg-type", message="type",
            category=DiagnosticCategory.TYPE_MISMATCH,
        )
        planner.plan([diag1])
        planner.plan([diag2])
        assert len(planner.history) == 2

    def test_history_included_in_instruction(self, planner: FixPlanner) -> None:
        """After first fix, instruction includes '[PREVIOUSLY FIXED]'."""
        diag1 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="a.py", line=1, code="E999", message="syntax error",
            category=DiagnosticCategory.SYNTAX,
        )
        diag2 = Diagnostic(
            tool="mypy", severity=DiagnosticSeverity.ERROR,
            file="b.py", line=10, code="mypy-arg-type", message="type error",
            category=DiagnosticCategory.TYPE_MISMATCH,
        )
        planner.plan([diag1])
        tasks = planner.plan([diag2])
        assert len(tasks) == 1
        assert "[PREVIOUSLY FIXED" in tasks[0].instruction
        assert "DO NOT REVERT" in tasks[0].instruction

    def test_reset_history(self, planner: FixPlanner, syntax_diag: Diagnostic) -> None:
        """reset_history clears accumulated entries."""
        planner.plan([syntax_diag])
        assert len(planner.history) == 1
        planner.reset_history()
        assert planner.history == []


# ===================================================================
# Co-location merging (Fix 3)
# ===================================================================


class TestCoLocationMerging:
    def test_same_file_nearby_lines_merged(self, planner: FixPlanner) -> None:
        """Same file, nearby lines, different priorities → merged."""
        syntax_diag = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="src/auth.py", line=1, code="E999", message="Syntax error",
            category=DiagnosticCategory.SYNTAX,
        )
        import_diag = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="src/auth.py", line=3, code="F401", message="Unused import",
            category=DiagnosticCategory.UNUSED_IMPORT,
        )
        type_diag = Diagnostic(
            tool="mypy", severity=DiagnosticSeverity.ERROR,
            file="src/auth.py", line=5, code="mypy-arg-type", message="Type error",
            category=DiagnosticCategory.TYPE_MISMATCH,
        )
        # UNUSED_IMPORT is auto-fixable, so filtered out
        tasks = planner.plan([syntax_diag, type_diag])
        # Co-located: syntax (P1, line 1) + type (P3, line 5) → same task
        assert len(tasks) == 1
        assert len(tasks[0].diagnostics) == 2
        assert tasks[0].priority == 1  # highest priority among merged

    def test_different_file_no_merge(self, planner: FixPlanner) -> None:
        """Different files → not merged, only highest priority returned."""
        d1 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="a.py", line=1, code="E999", message="syntax",
            category=DiagnosticCategory.SYNTAX,
        )
        d2 = Diagnostic(
            tool="mypy", severity=DiagnosticSeverity.ERROR,
            file="b.py", line=2, code="mypy-arg-type", message="type",
            category=DiagnosticCategory.TYPE_MISMATCH,
        )
        tasks = planner.plan([d1, d2])
        assert len(tasks) == 1
        assert len(tasks[0].diagnostics) == 1  # only syntax (P1)
        assert tasks[0].diagnostics[0].file == "a.py"

    def test_far_apart_lines_no_merge(self, planner: FixPlanner) -> None:
        """Same file but lines far apart → not merged."""
        d1 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="a.py", line=1, code="E999", message="syntax",
            category=DiagnosticCategory.SYNTAX,
        )
        d2 = Diagnostic(
            tool="mypy", severity=DiagnosticSeverity.ERROR,
            file="a.py", line=100, code="mypy-arg-type", message="type",
            category=DiagnosticCategory.TYPE_MISMATCH,
        )
        tasks = planner.plan([d1, d2])
        assert len(tasks) == 1
        assert len(tasks[0].diagnostics) == 1  # only syntax (P1)

    def test_same_priority_not_merged(self, planner: FixPlanner) -> None:
        """Same priority items are not merged (they stay in same group anyway)."""
        d1 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="a.py", line=1, code="E999", message="syntax1",
            category=DiagnosticCategory.SYNTAX,
        )
        d2 = Diagnostic(
            tool="ruff", severity=DiagnosticSeverity.ERROR,
            file="a.py", line=3, code="E999", message="syntax2",
            category=DiagnosticCategory.SYNTAX,
        )
        tasks = planner.plan([d1, d2])
        assert len(tasks) == 1
        assert len(tasks[0].diagnostics) == 2  # both in same priority group
