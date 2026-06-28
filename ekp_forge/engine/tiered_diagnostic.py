"""Sequential tiered diagnostic runner — Ruff → Mypy → Pytest with early exit.

Phase 4 replaces the flat ``run_verification_pipeline()`` with a stateful
sequential runner that exits early at the first failing stage. This prevents
cascading false positives: if Ruff finds syntax errors, Mypy would produce
10× noise — we never show that noise to the Worker.

Usage::

    from ekp_forge.engine.tiered_diagnostic import TieredDiagnosticRunner

    runner = TieredDiagnosticRunner()
    result = runner.run(["src/main.py"])

    if result.stage == DiagnosticStage.RUFF and not result.passed:
        # Handle Ruff errors only
        fix_tasks = planner.plan_v2(result)
    elif result.stage == DiagnosticStage.MYPY and not result.passed:
        # Handle Mypy errors only
        ...
    elif result.stage == DiagnosticStage.PYTEST and not result.passed:
        # Handle test failures
        ...
    else:
        # All passed
        ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ekp_forge.sandbox.verification_ir import (
    AutoFixRunner,
    MypyParser,
    PytestParser,
    RuffParser,
    run_single_tool,
)
from ekp_forge.schemas.contract import Diagnostic, DiagnosticCategory


class DiagnosticStage(StrEnum):
    """Current stage in the tiered diagnostic pipeline.

    Each stage corresponds to a verification tool. The pipeline progresses
    sequentially: Ruff → Mypy → Pytest → Passed.
    """

    RUFF = "ruff"
    MYPY = "mypy"
    PYTEST = "pytest"
    PASSED = "passed"


@dataclass
class TieredDiagnosticResult:
    """Result of executing one or more stages in the tiered pipeline.

    Attributes:
        stage:        Which stage was executed / is failing.
        diagnostics:  Diagnostics found at this stage (empty if passed).
        passed:       True if no errors at this stage.
        next_stage:   The next stage to execute, or PASSED if all clear.
    """

    stage: DiagnosticStage
    diagnostics: list[Diagnostic] = field(default_factory=list)
    passed: bool = True
    next_stage: DiagnosticStage = DiagnosticStage.PASSED


class TieredDiagnosticRunner:
    """Sequential diagnostic runner with stage-gated execution.

    The runner executes verification tools one at a time, stopping at the
    first stage that produces errors. This prevents cascading false positives
    from downstream tools.

    It also supports ``run_stage()`` for re-verifying a single stage after
    a fix has been applied, without re-running the entire pipeline.
    """

    def __init__(self, workspace: Path | None = None) -> None:
        """Initialise the runner.

        Args:
            workspace: Optional workspace path. If None, uses CWD.
        """
        self._workspace = workspace

    # ------------------------------------------------------------------
    # Full pipeline: sequential stages with early exit
    # ------------------------------------------------------------------

    def run(
        self,
        changed_files: list[str] | None = None,
    ) -> TieredDiagnosticResult:
        """Execute the tiered diagnostic pipeline sequentially.

        Stages:
        1. Auto-fix (ruff check --fix, ruff format) — always runs first.
        2. Ruff — syntax/format errors. If found, return immediately.
        3. Mypy — type errors. If found, return immediately.
        4. Pytest — logic errors. If found, return (no further stages).

        Args:
            changed_files: Optional list of file paths to scope checks to.

        Returns:
            A ``TieredDiagnosticResult`` representing the first failing stage,
            or a PASSED result if all stages succeed.
        """
        resolve_cwd = self._workspace or Path.cwd()

        # Step 0: Auto-fix (mechanical fixes only — never delegated to LLM)
        fixer = AutoFixRunner(workspace=resolve_cwd)
        fixer.run_all(changed_files)

        # Step 1: Ruff — Syntax/format errors
        ruff_result = run_single_tool("ruff", changed_files, cwd=resolve_cwd)
        ruff_diags = RuffParser.parse(ruff_result.raw_output)
        non_format_diags = [
            d
            for d in ruff_diags
            if d.category
            not in {DiagnosticCategory.FORMATTING, DiagnosticCategory.UNUSED_IMPORT}
        ]

        if non_format_diags:
            return TieredDiagnosticResult(
                stage=DiagnosticStage.RUFF,
                diagnostics=non_format_diags,
                passed=False,
                next_stage=DiagnosticStage.MYPY,
            )

        # Step 2: Mypy — Type errors (only if Ruff passed)
        mypy_result = run_single_tool("mypy", changed_files, cwd=resolve_cwd)
        mypy_diags = MypyParser.parse(mypy_result.raw_output)

        if mypy_diags:
            return TieredDiagnosticResult(
                stage=DiagnosticStage.MYPY,
                diagnostics=mypy_diags,
                passed=False,
                next_stage=DiagnosticStage.PYTEST,
            )

        # Step 3: Pytest — Logic errors (only if Ruff and Mypy passed)
        pytest_result = run_single_tool("pytest", changed_files, cwd=resolve_cwd)
        pytest_diags = PytestParser.parse(pytest_result.raw_output)

        if pytest_diags:
            return TieredDiagnosticResult(
                stage=DiagnosticStage.PYTEST,
                diagnostics=pytest_diags,
                passed=False,
                next_stage=DiagnosticStage.PASSED,
            )

        # All passed
        return TieredDiagnosticResult(
            stage=DiagnosticStage.PASSED,
            diagnostics=[],
            passed=True,
            next_stage=DiagnosticStage.PASSED,
        )

    # ------------------------------------------------------------------
    # Single-stage runner (for re-verification after fix)
    # ------------------------------------------------------------------

    def run_stage(
        self,
        stage: DiagnosticStage,
        changed_files: list[str] | None = None,
    ) -> TieredDiagnosticResult:
        """Run a single diagnostic stage independently.

        This is used during Phase 6 re-verification after a fix has been
        applied, to re-check only the relevant stage without running the
        entire pipeline.

        Args:
            stage:         The stage to run.
            changed_files: Optional list of file paths to scope checks to.

        Returns:
            Result for that single stage.
        """
        resolve_cwd = self._workspace or Path.cwd()

        if stage == DiagnosticStage.RUFF:
            result = run_single_tool("ruff", changed_files, cwd=resolve_cwd)
            diags = RuffParser.parse(result.raw_output)
            non_format = [
                d
                for d in diags
                if d.category
                not in {DiagnosticCategory.FORMATTING, DiagnosticCategory.UNUSED_IMPORT}
            ]
            return TieredDiagnosticResult(
                stage=stage,
                diagnostics=non_format,
                passed=len(non_format) == 0,
                next_stage=DiagnosticStage.MYPY,
            )

        if stage == DiagnosticStage.MYPY:
            result = run_single_tool("mypy", changed_files, cwd=resolve_cwd)
            diags = MypyParser.parse(result.raw_output)
            return TieredDiagnosticResult(
                stage=stage,
                diagnostics=diags,
                passed=len(diags) == 0,
                next_stage=DiagnosticStage.PYTEST,
            )

        if stage == DiagnosticStage.PYTEST:
            result = run_single_tool("pytest", changed_files, cwd=resolve_cwd)
            diags = PytestParser.parse(result.raw_output)
            return TieredDiagnosticResult(
                stage=stage,
                diagnostics=diags,
                passed=len(diags) == 0,
                next_stage=DiagnosticStage.PASSED,
            )

        return TieredDiagnosticResult(
            stage=DiagnosticStage.PASSED, passed=True
        )
