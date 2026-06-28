"""Worker Agent — Tier 3: executes tasks via Aider + verification loop with escalation.

Phase 2 upgrade:
- Accepts ``WorkerContract`` to constrain Worker scope.
- Accepts ``FixTask`` for targeted fix instructions from the Fix Planner.
- Uses the Verification IR pipeline (``run_verification_pipeline``) instead
  of raw ``run_ruff`` / ``run_mypy`` / string-based error logs.
- ``Diagnostic`` models are returned for structured escalation.

All existing public methods are preserved for backward compatibility.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

# Import verification & setup from orchestrator
from ekp_forge.orchestrator import setup_ruff_mypy
from ekp_forge.agents.base import BaseAgent, ExecutionTier
from ekp_forge.protocol.capability import Capability
from ekp_forge.protocol.roles import Role
from ekp_forge.sandbox.introspection import IntrospectionTool
from ekp_forge.sandbox.verification_ir import (
    run_verification_pipeline,
)
from ekp_forge.schemas.contract import (
    Diagnostic,
    DiagnosticCategory,
    DiagnosticSeverity,
    FixTask,
    WorkerContract,
)
from ekp_forge.schemas.task_schema import (
    ErrorChunkEntry,
    ErrorChunkSummary,
    EscalationReason,
    HelpRequestSchema,
    ReflectionEntry,
    ReflectionLog,
    _error_fingerprint,
    _estimate_confidence,
    _is_project_specific,
)

# Paths for reflection logs
_GLOBAL_REFLECTIONS_DIR = Path.home() / ".ai-knowledge" / "reflections"
_PROJECT_REFLECTIONS_DIR = Path(".ai-knowledge") / "reflections"


# ---------------------------------------------------------------------------
# Worker Agent
# ---------------------------------------------------------------------------


class WorkerAgent(BaseAgent):
    """Executes implementation plans through Aider + verification loop.

    Implements ``BaseAgent`` for protocol compatibility. The ``execute()``
    method dispatches to existing methods based on the ``_role`` key in
    the context dict.

    Phase 2 additions:
    - ``worker_contract`` in context: constrains scope via ``WorkerContract``.
    - ``fix_task`` in context: a ``FixTask`` from the Fix Planner for targeted
      single-priority fixes.

    Phase 3 additions:
    - Declares ``capabilities`` including CODING, VERIFICATION, and INTROSPECTION.
    - ``execution_tier`` is ``"local"`` by default (7B model via Ollama).
    """

    agent_id: str = "worker"
    capabilities: list[Capability] = [
        Capability.CODING,
        Capability.VERIFICATION,
        Capability.INTROSPECTION,  # Phase 3: dynamic dir()/help() introspection
    ]
    execution_tier: ExecutionTier = "local"

    def __init__(
        self,
        model: str = "ollama/qwen2.5-coder:7b",
        max_retries: int = 3,
        escalation_confidence_threshold: float = 0.6,
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        self.escalation_confidence_threshold = escalation_confidence_threshold
        self._reflection_log: ReflectionLog | None = None
        # Phase 3: cumulative introspection budget (max 30s total)
        self._introspection_budget_remaining: float = 30.0

    # -------------------------------------------------------------------
    # BaseAgent Protocol (Role-based Protocol Architecture)
    # -------------------------------------------------------------------

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        """Dispatch to the appropriate method based on the role context.

        This is the ``BaseAgent`` interface implementation. It reads the
        ``_role`` key from context to determine which internal method to
        call. Exceptions propagate transparently — no try/except here.

        Supported roles:
        - ``IMPLEMENTATION``: calls ``execute_verification_loop()``
        - ``VERIFICATION``:   calls ``execute_verification()`` (IR generation)

        Phase 2 context keys:
        - ``worker_contract``: A ``WorkerContract`` constraining scope.
        - ``fix_task``: A ``FixTask`` for targeted single-priority fixes.
        """
        role: Role | None = context.get("_role")
        task: Any = context.get("task")
        plan: str = context.get("plan", "")
        # Phase 4.5: Read execution mode from context (injected by WorkflowEngine)
        execution_mode: str = context.get("execution_mode", "production")

        if role == Role.IMPLEMENTATION:
            if task is None:
                raise ValueError("WorkerAgent.execute(): 'task' required in context")
            plan_text = plan or context.get("implementation_plan", "")
            rag_context = context.get("_rag_context", "")
            worker_contract: WorkerContract | None = context.get("worker_contract")
            fix_task: FixTask | None = context.get("fix_task")

            if execution_mode == "research":
                # Phase 4.5: Research mode — no sandbox, no git ops, direct local execution
                result = self._execute_research_mode(
                    task,
                    plan_text,
                    rag_context,
                    worker_contract=worker_contract,
                    fix_task=fix_task,
                )
            else:
                # Production mode — use git worktree isolated execution
                result = self._run_with_worktree(
                    task,
                    plan_text,
                    rag_context,
                    worker_contract=worker_contract,
                    fix_task=fix_task,
                )
            return dict(result)

        if role == Role.VERIFICATION:
            # Verification role: run the IR pipeline and return structured diagnostics
            if task is None:
                raise ValueError("WorkerAgent.execute(): 'task' required in context for VERIFICATION")
            result = self.execute_verification(task)
            return dict(result)

        # Role not handled by WorkerAgent
        return {"status": "skipped", "reason": f"Role {role} not handled by WorkerAgent"}

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def execute_verification(
        self,
        task: Any,
    ) -> dict[str, Any]:
        """Execute the Verification role: run the IR pipeline and return structured diagnostics.

        This method is invoked when ``_role == Role.VERIFICATION``. It runs
        the full verification pipeline (auto-fix → ruff → mypy → pytest)
        and returns structured ``Diagnostic`` instances instead of raw strings.

        Args:
            task: The ``TaskSchema`` instance.

        Returns:
            Dict with keys:
            - ``"status"``: ``"success"`` | ``"failed"``
            - ``"diagnostics"``: list[Diagnostic]
            - ``"diagnostic_count"``: int
        """
        from ekp_forge.sandbox.scoped_lint import _changed_files

        changed_file_paths = _changed_files()
        changed_files = [str(f) for f in changed_file_paths] if changed_file_paths else None

        # Run the verification pipeline: auto-fix → check → parse
        diagnostics = run_verification_pipeline(
            changed_files=changed_files,
            run_pytest=True,
        )

        if not diagnostics:
            return {
                "status": "success",
                "diagnostics": [],
                "diagnostic_count": 0,
            }

        return {
            "status": "failed",
            "diagnostics": [d.model_dump() for d in diagnostics],
            "diagnostic_count": len(diagnostics),
        }

    # -------------------------------------------------------------------
    # Phase 4.5: Shared helpers for all execution modes
    # -------------------------------------------------------------------

    def _build_aider_cmd(
        self,
        task: Any,
        fix_task: FixTask | None = None,
    ) -> list[str]:
        """Build a standard Aider command with common arguments.

        All execution modes share this command template:
        - ``--yes`` (non-interactive)
        - ``--no-git`` (prevent Aider from managing git — Worker retains control)
        - ``--edit-format diff``
        - ``--read`` for any ``.ai-knowledge/*.md`` files
        - ``--message-file`` for the plan/error instructions
        - Target files from ``fix_task.contract.target_files`` (if fix_task)
          or ``task.affected_modules``

        Args:
            task: The TaskSchema instance.
            fix_task: Optional FixTask for targeted fix scope.

        Returns:
            Aider command as a list of strings.
        """
        temp_msg = ".aider.msg.temp"
        cmd = [
            "aider",
            "--yes",
            "--no-git",  # CRITICAL: prevent Aider git operations in all modes
            "--edit-format",
            "diff",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        if os.path.exists(".ai-knowledge"):
            for f in sorted(os.listdir(".ai-knowledge")):
                fpath = os.path.join(".ai-knowledge", f)
                if os.path.isfile(fpath) and f.endswith(".md"):
                    cmd.extend(["--read", fpath])
        cmd.extend(["--message-file", temp_msg])

        if fix_task is not None:
            cmd.extend(fix_task.contract.target_files)
        else:
            cmd.extend(task.affected_modules)
        return cmd

    def _build_scope_block(self, worker_contract: WorkerContract) -> str:
        """Build the scope-constraint block for a WorkerContract."""
        return (
            f"[CONTRACT: {worker_contract.contract_id}]\n"
            f"Objective: {worker_contract.objective}\n"
            f"Target files: {', '.join(worker_contract.target_files)}\n"
            f"Editable symbols: {', '.join(worker_contract.editable_symbols) or '(any)'}\n"
            f"Forbidden symbols: {', '.join(worker_contract.forbidden_symbols) or '(none)'}\n"
            f"Design freedom: {worker_contract.local_design_freedom}\n\n"
            f"You are STRICTLY FORBIDDEN from modifying files outside the target_files list. "
            f"You MUST NOT change or remove any function/class/method listed in forbidden_symbols.\n\n"
        )

    # -------------------------------------------------------------------
    # Phase 4.5: Shared Aider + Verification Retry Loop
    # -------------------------------------------------------------------

    def _run_aider_verification_loop(
        self,
        task: Any,
        plan: str,
        aider_cmd: list[str],
        error_chunk: ErrorChunkSummary,
        *,
        worker_contract: WorkerContract | None = None,
        fix_task: FixTask | None = None,
        workspace: Path | None = None,
    ) -> dict[str, Any]:
        """Shared Aider + Verification retry loop for all execution modes.

        This method contains the common retry logic that is identical across
        research, production (worktree), and legacy (sandbox) modes:

        1. Write plan → run Aider → validate imports → verify → repeat.
        2. Escalation policy with introspection fallback.
        3. Success / failure / escalation return dicts.

        Args:
            task:       The TaskSchema instance.
            plan:       Initial plan text (may be updated across retries).
            aider_cmd:  Pre-built Aider command (see ``_build_aider_cmd``).
            error_chunk: Pre-allocated ErrorChunkSummary for this run.
            worker_contract: Optional WorkerContract for scope constraints.
            fix_task:   Optional FixTask for targeted fixes.
            workspace:  Optional workspace path for verification pipeline.
                        ``None`` = research mode (local files).

        Returns:
            Standard result dict (same schema as ``execute_verification_loop``).
        """
        from ekp_forge.sandbox.verification_ir import run_verification_pipeline

        prev_error_hash: str | None = None
        current_instructions = plan

        # Prepend WorkerContract scope constraints
        if worker_contract is not None and fix_task is None:
            current_instructions = self._build_scope_block(worker_contract) + current_instructions

        try:
            for attempt in range(1, self.max_retries + 1):
                self._write_temp_message(current_instructions)

                # Step 1: Aider execution
                aider_ok, aider_msg = self._run_aider(aider_cmd, attempt)
                if not aider_ok:
                    error_chunk.add_entry(
                        ErrorChunkEntry(
                            attempt=attempt,
                            error_type="AiderExecutionError",
                            module=task.affected_modules[0] if task.affected_modules else "*",
                            action_taken=aider_msg,
                        )
                    )
                    break

                # Step 2: AST gatekeeper
                import_ok, import_err = self._validate_imports()
                if not import_ok:
                    error_chunk.add_entry(
                        ErrorChunkEntry(
                            attempt=attempt, error_type="ImportViolation", module="*", action_taken=import_err
                        )
                    )
                    continue

                from ekp_forge.sandbox.scoped_lint import _changed_files

                changed_files = [str(f) for f in _changed_files()] if _changed_files() else None

                # Step 3: Verification IR pipeline
                diagnostics = run_verification_pipeline(
                    changed_files=changed_files,
                    run_pytest=True,
                    workspace=workspace,
                )

                # Record diagnostics
                for d in diagnostics:
                    error_chunk.add_entry(
                        ErrorChunkEntry(
                            attempt=attempt,
                            error_type=f"{d.tool.upper()}{d.code}",
                            module=d.file,
                            action_taken=f"{d.category.value}: {d.message[:200]}",
                        )
                    )

                # Check for remaining issues
                remaining = [
                    d
                    for d in diagnostics
                    if d.category not in {DiagnosticCategory.FORMATTING, DiagnosticCategory.UNUSED_IMPORT}
                ]
                if not remaining:
                    git_diff = self._get_git_diff()
                    self._update_reflection_log(task, error_chunk, success=True)
                    return {
                        "status": "success",
                        "retries": attempt,
                        "error_chunk_summary": error_chunk,
                        "help_request": None,
                        "git_diff": git_diff,
                        "diagnostics": [d.model_dump() for d in diagnostics],
                    }

                # Build compressed error feedback
                error_lines = [f"  [{d.tool}] {d.file}:{d.line}: {d.message[:150]}" for d in diagnostics[:15]]
                if len(diagnostics) > 15:
                    error_lines.append(f"  ... and {len(diagnostics) - 15} more issues")
                combined_error_log = "\n".join(error_lines)[:1500]

                # Introspection
                introspection_context: str | None = None
                if (
                    "AttributeError" in combined_error_log
                    or "ModuleNotFoundError" in combined_error_log
                    or "has no attribute" in combined_error_log.lower()
                ):
                    introspection_context = self._try_introspection(combined_error_log)

                # Escalation policy
                if not introspection_context:
                    esc_result = self._check_escalation_policy(
                        attempt, error_chunk, combined_error_log, prev_error_hash, task
                    )
                    if esc_result is not None:
                        git_diff = self._get_git_diff()
                        self._git_rollback()
                        return {
                            "status": "escalated",
                            "retries": attempt,
                            "error_chunk_summary": error_chunk,
                            "help_request": esc_result,
                            "git_diff": git_diff,
                            "diagnostics": [d.model_dump() for d in diagnostics],
                        }

                # Build next iteration instructions
                contract_block = (
                    f"[CONTRACT: {worker_contract.contract_id}]\n"
                    f"Target files: {', '.join(worker_contract.target_files)}\n"
                    f"You are STRICTLY FORBIDDEN from modifying anything outside the scope above.\n\n"
                    if worker_contract is not None
                    else ""
                )
                introspection_block = (
                    f"\n[Introspection Result]\n{introspection_context}\n\n" if introspection_context else ""
                )
                current_instructions = (
                    f"{contract_block}{introspection_block}"
                    f"The verification pipeline found remaining issues. Fix only the errors listed below.\n\n"
                    f"--- Remaining Issues ({len(remaining)} unresolved) ---\n{combined_error_log}"
                )
                prev_error_hash = _error_fingerprint(combined_error_log)
        finally:
            self._cleanup_temp_message()

        # Loop exhausted
        self._git_rollback()
        self._update_reflection_log(task, error_chunk, success=False)
        return {
            "status": "failed",
            "retries": self.max_retries,
            "error_chunk_summary": error_chunk,
            "help_request": None,
            "git_diff": "",
            "diagnostics": None,
        }

    # -------------------------------------------------------------------
    # Legacy: execute_verification_loop (DEPRECATED)
    # -------------------------------------------------------------------

    def execute_verification_loop(
        self,
        task: Any,  # TaskSchema (avoid import-time circular issues with typing)
        plan: str,
        _rag_context: str = "",
        *,
        worker_contract: WorkerContract | None = None,
        fix_task: FixTask | None = None,
    ) -> dict[str, Any]:
        """
        **DEPRECATED**: Use ``WorkerAgent.execute()`` with ``execution_mode``
        context instead.

        This legacy method uses ``SandboxWorkspace`` + ``clone_into()`` which
        performs a full ``git clone --depth 1`` on every invocation (~300s
        overhead).

        **Replacement**: Call ``execute()`` with::

            worker.execute({
                "_role": Role.IMPLEMENTATION,
                "task": task,
                "plan": plan,
                "execution_mode": "production",
            })

        The new production path uses ``GitWorktree`` (millisecond-fast
        isolated workspace) instead of ``SandboxWorkspace`` + ``clone_into``.

        This method is preserved for backward compatibility and delegates to
        the deprecated ``SandboxWorkspace`` implementation.
        """
        import warnings

        warnings.warn(
            "execute_verification_loop() is deprecated. Use execute() with execution_mode context instead.",
            DeprecationWarning,
            stacklevel=2,
        )

        from ekp_forge.sandbox.cloner import clone_into
        from ekp_forge.sandbox.workspace import SandboxWorkspace
        from ekp_forge.schemas.task_schema import TaskSchema

        assert isinstance(task, TaskSchema), "task must be a TaskSchema instance"

        error_chunk = ErrorChunkSummary(task_id=task.task_id)

        with SandboxWorkspace() as ws_path:
            clone_ok, clone_err = clone_into(ws_path)
            if not clone_ok:
                return {
                    "status": "failed",
                    "retries": 0,
                    "error_chunk_summary": error_chunk,
                    "help_request": None,
                    "git_diff": "",
                }

            original_cwd = os.getcwd()
            os.chdir(ws_path / "repo")
            try:
                try:
                    setup_ruff_mypy()
                except Exception as e:
                    print(f"Failed to run setup_ruff_mypy: {e}", file=sys.stderr)

                prev_error_hash: str | None = None

                temp_msg = ".aider.msg.temp"
                aider_cmd = ["aider", "--yes", "--no-git", "--edit-format", "diff"]
                if self.model:
                    aider_cmd.extend(["--model", self.model])
                if os.path.exists(".ai-knowledge"):
                    for f in sorted(os.listdir(".ai-knowledge")):
                        fpath = os.path.join(".ai-knowledge", f)
                        if os.path.isfile(fpath) and f.endswith(".md"):
                            aider_cmd.extend(["--read", fpath])
                aider_cmd.extend(["--message-file", temp_msg])
                if fix_task is not None:
                    aider_cmd.extend(fix_task.contract.target_files)
                else:
                    aider_cmd.extend(task.affected_modules)

                current_instructions = plan
                if worker_contract is not None and fix_task is None:
                    scope_block = (
                        f"[CONTRACT: {worker_contract.contract_id}]\n"
                        f"Objective: {worker_contract.objective}\n"
                        f"Target files: {', '.join(worker_contract.target_files)}\n"
                        f"Editable symbols: {', '.join(worker_contract.editable_symbols) or '(any)'}\n"
                        f"Forbidden symbols: {', '.join(worker_contract.forbidden_symbols) or '(none)'}\n"
                        f"Design freedom: {worker_contract.local_design_freedom}\n\n"
                        f"You are STRICTLY FORBIDDEN from modifying files outside the target_files list. "
                        f"You MUST NOT change or remove any function/class/method listed in forbidden_symbols.\n\n"
                    )
                    current_instructions = scope_block + current_instructions

                try:
                    for attempt in range(1, self.max_retries + 1):
                        self._write_temp_message(current_instructions)

                        aider_ok, aider_msg = self._run_aider(aider_cmd, attempt)
                        if not aider_ok:
                            error_chunk.add_entry(
                                ErrorChunkEntry(
                                    attempt=attempt,
                                    error_type="AiderExecutionError",
                                    module=task.affected_modules[0] if task.affected_modules else "*",
                                    action_taken=aider_msg,
                                )
                            )
                            break

                        import_ok, import_err = self._validate_imports()
                        if not import_ok:
                            error_chunk.add_entry(
                                ErrorChunkEntry(
                                    attempt=attempt, error_type="ImportViolation", module="*", action_taken=import_err
                                )
                            )
                            continue

                        from ekp_forge.sandbox.scoped_lint import _changed_files

                        changed_files = [str(f) for f in _changed_files()] if _changed_files() else None

                        diagnostics = run_verification_pipeline(
                            changed_files=changed_files,
                            run_pytest=True,
                            workspace=ws_path / "repo",
                        )

                        for d in diagnostics:
                            error_chunk.add_entry(
                                ErrorChunkEntry(
                                    attempt=attempt,
                                    error_type=f"{d.tool.upper()}{d.code}",
                                    module=d.file,
                                    action_taken=f"{d.category.value}: {d.message[:200]}",
                                )
                            )

                        remaining = [
                            d
                            for d in diagnostics
                            if d.category not in {DiagnosticCategory.FORMATTING, DiagnosticCategory.UNUSED_IMPORT}
                        ]
                        if not remaining:
                            git_diff = self._get_git_diff()
                            self._update_reflection_log(task, error_chunk, success=True)
                            return {
                                "status": "success",
                                "retries": attempt,
                                "error_chunk_summary": error_chunk,
                                "help_request": None,
                                "git_diff": git_diff,
                                "diagnostics": [d.model_dump() for d in diagnostics],
                            }

                        error_lines = [f"  [{d.tool}] {d.file}:{d.line}: {d.message[:150]}" for d in diagnostics[:15]]
                        if len(diagnostics) > 15:
                            error_lines.append(f"  ... and {len(diagnostics) - 15} more issues")
                        combined_error_log = "\n".join(error_lines)[:1500]

                        introspection_context: str | None = None
                        if (
                            "AttributeError" in combined_error_log
                            or "ModuleNotFoundError" in combined_error_log
                            or "has no attribute" in combined_error_log.lower()
                        ):
                            introspection_context = self._try_introspection(combined_error_log)

                        if not introspection_context:
                            esc_result = self._check_escalation_policy(
                                attempt, error_chunk, combined_error_log, prev_error_hash, task
                            )
                            if esc_result is not None:
                                git_diff = self._get_git_diff()
                                self._git_rollback()
                                return {
                                    "status": "escalated",
                                    "retries": attempt,
                                    "error_chunk_summary": error_chunk,
                                    "help_request": esc_result,
                                    "git_diff": git_diff,
                                    "diagnostics": [d.model_dump() for d in diagnostics],
                                }

                        contract_block = ""
                        if worker_contract is not None:
                            contract_block = (
                                f"[CONTRACT: {worker_contract.contract_id}]\n"
                                f"Target files: {', '.join(worker_contract.target_files)}\n"
                                f"You are STRICTLY FORBIDDEN from modifying anything outside the scope above.\n\n"
                            )
                        introspection_block = (
                            f"\n[Introspection Result]\n{introspection_context}\n\n" if introspection_context else ""
                        )
                        current_instructions = (
                            f"{contract_block}{introspection_block}"
                            f"The verification pipeline found remaining issues. Fix only the errors listed below.\n\n"
                            f"--- Remaining Issues ({len(remaining)} unresolved) ---\n{combined_error_log}"
                        )
                        prev_error_hash = _error_fingerprint(combined_error_log)
                finally:
                    self._cleanup_temp_message()

                self._git_rollback()
                self._update_reflection_log(task, error_chunk, success=False)
                return {
                    "status": "failed",
                    "retries": self.max_retries,
                    "error_chunk_summary": error_chunk,
                    "help_request": None,
                    "git_diff": "",
                    "diagnostics": None,
                }
            finally:
                os.chdir(original_cwd)

    # -------------------------------------------------------------------
    # Phase 4.5: Research Mode — Direct Local Execution (no sandbox, no git ops)
    # -------------------------------------------------------------------

    def _execute_research_mode(
        self,
        task: Any,
        plan: str,
        _rag_context: str = "",
        *,
        worker_contract: WorkerContract | None = None,
        fix_task: FixTask | None = None,
    ) -> dict[str, Any]:
        """Research mode — Aider + verification directly on local workspace.

        - No SandboxWorkspace (no temp directory, no file copy).
        - No clone_into() (no git clone — works on local files directly).
        - Aider runs with ``--file`` restriction to prevent uncontrolled edits.
        - Delegates the retry loop to ``_run_aider_verification_loop()``.
        """
        from ekp_forge.schemas.task_schema import TaskSchema

        assert isinstance(task, TaskSchema), "task must be a TaskSchema instance"
        error_chunk = ErrorChunkSummary(task_id=task.task_id)

        try:
            setup_ruff_mypy()
        except Exception as e:
            print(f"Failed to run setup_ruff_mypy: {e}", file=sys.stderr)

        aider_cmd = self._build_aider_cmd(task, fix_task)
        return self._run_aider_verification_loop(
            task,
            plan,
            aider_cmd,
            error_chunk,
            worker_contract=worker_contract,
            fix_task=fix_task,
            workspace=None,  # research mode = local files, no chdir
        )

    # -------------------------------------------------------------------
    # Phase 4.5: Production Mode — Git Worktree Isolated Execution
    # -------------------------------------------------------------------

    def _run_with_worktree(
        self,
        task: Any,
        plan: str,
        _rag_context: str = "",
        *,
        worker_contract: WorkerContract | None = None,
        fix_task: FixTask | None = None,
    ) -> dict[str, Any]:
        """Production mode — Aider + verification inside a ``git worktree``.

        - Creates a ``GitWorktree`` in **milliseconds** (no file copy).
        - Aider always runs with ``--no-git`` (Worker retains git control).
        - Worktree is removed on both success and failure (``try/finally``).
        - Delegates the retry loop to ``_run_aider_verification_loop()``.
        """
        from ekp_forge.sandbox.git_worktree import GitWorktree
        from ekp_forge.schemas.task_schema import TaskSchema

        assert isinstance(task, TaskSchema), "task must be a TaskSchema instance"
        error_chunk = ErrorChunkSummary(task_id=task.task_id)

        with GitWorktree() as worktree_path:
            original_cwd = os.getcwd()
            os.chdir(worktree_path)
            try:
                try:
                    setup_ruff_mypy()
                except Exception as e:
                    print(f"Failed to run setup_ruff_mypy: {e}", file=sys.stderr)

                aider_cmd = self._build_aider_cmd(task, fix_task)
                return self._run_aider_verification_loop(
                    task,
                    plan,
                    aider_cmd,
                    error_chunk,
                    worker_contract=worker_contract,
                    fix_task=fix_task,
                    workspace=worktree_path,
                )
            finally:
                os.chdir(original_cwd)

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _write_temp_message(self, content: str) -> str:
        """Write plan to a temporary message file."""
        path = ".aider.msg.temp"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _cleanup_temp_message(self) -> None:
        """Remove temporary message file."""
        path = ".aider.msg.temp"
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    def _run_aider(self, cmd: list[str], _attempt: int, timeout: int = 600) -> tuple[bool, str]:
        """Execute Aider and return (success, message_or_output)."""
        env = os.environ.copy()
        env["AIDER_MAP_TOKENS"] = "0"
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                env=env,
            )
            if res.returncode != 0:
                return False, f"Aider failed with code {res.returncode}: {res.stderr[:500]}"
            return True, res.stdout
        except subprocess.TimeoutExpired:
            return False, f"Aider timed out after {timeout}s"

    def _validate_imports(self) -> tuple[bool, str]:
        """AST gatekeeper: check imports against api_schema.yaml."""
        import yaml

        schema_path = Path("api_schema.yaml")
        if not schema_path.exists():
            return True, "No schema file found"

        with open(schema_path) as f:
            schema = yaml.safe_load(f)

        allowed = set(schema.get("allowed_imports", []))
        dangerous_builtins = {"eval", "exec", "compile", "open"}

        for py_file in Path().rglob("*.py"):
            if ".venv" in py_file.parts:
                continue
            if "ekp_forge" in py_file.parts:
                continue
            if "test_output" in py_file.parts:
                continue
            if "dsc" in py_file.parts:
                continue
            if "tests" in py_file.parts and "generated" not in py_file.parts:
                continue

            content = py_file.read_text()
            for raw_line in content.splitlines():
                line = raw_line.strip()
                if line.startswith("import ") or line.startswith("from "):
                    pkg = line.split()[1].split(".")[0]
                    if pkg not in allowed and not pkg.startswith("_"):
                        return False, f"Unauthorized import of '{pkg}' in {py_file}"
                for danger in dangerous_builtins:
                    if f"{danger}(" in line:
                        return False, f"Dangerous builtin '{danger}()' in {py_file}"

        return True, "All imports valid"

    def _run_pytest(self) -> tuple[bool, str]:
        """Run pytest and return (success, output)."""
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-v",
                    "--tb=short",
                    "--ignore=tests/step1_baseline",
                    "--ignore=tests/step2_fake_api",
                    "--ignore=tests/step3_stress",
                    "--ignore=tests/step4_ollama_synthesizer",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout + result.stderr
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "pytest timed out after 120s"
        except Exception as e:
            return False, str(e)

    def _compress_error_log(self, error_text: str) -> str:
        """
        Truncates and extracts only the essential lines of traceback and error output
        to prevent context window decay (Lost in the Middle).
        """
        if not error_text:
            return ""

        # Limit overall text length first to prevent processing massive text
        if len(error_text) <= 1500:
            return error_text

        lines = error_text.splitlines()
        important_lines: list[str] = []

        # Keywords that indicate actual failure details
        target_patterns = [
            r"^FAILED\b",
            r"^ERROR\b",
            r"^Traceback \(most recent call first\):",
            r"^Traceback \(most recent call last\):",
            r"Error\b",
            r"^>",  # pytest source failure marker
            r"^E\s+",  # pytest error detail marker
            r":\d+: (error|warning|note):",  # mypy/ruff format
        ]

        # Gather context around failure markers
        for idx, line in enumerate(lines):
            is_important = False
            for pat in target_patterns:
                if re.search(pat, line):
                    is_important = True
                    break

            if is_important:
                # Add context around the line
                start = max(0, idx - 1)
                end = min(len(lines), idx + 3)
                for c_idx in range(start, end):
                    c_line = lines[c_idx].strip()
                    if c_line and c_line not in important_lines:
                        important_lines.append(c_line)

        # Fallback to last 30 lines if no patterns matched
        if not important_lines:
            important_lines = [line.strip() for line in lines[-30:] if line.strip()]

        compressed = "\n".join(important_lines)
        if len(compressed) > 1500:
            compressed = compressed[:1500] + "\n... [Truncated due to length constraints] ..."

        return compressed

    def _get_git_diff(self) -> str:
        """Get current git diff."""
        try:
            res = subprocess.run(
                ["git", "diff"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
            return res.stdout
        except Exception:
            return ""

    def _git_rollback(self) -> None:
        """Rollback changes via git."""
        try:
            subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
            subprocess.run(
                ["git", "clean", "-fdx", "--exclude=.venv"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
        except Exception:
            pass

    # -------------------------------------------------------------------
    # Phase 3: Introspection — try before escalating
    # -------------------------------------------------------------------

    def _try_introspection(self, error_text: str) -> str | None:
        """Attempt to resolve AttributeError/ModuleNotFoundError via introspection.

        Parses the error text to extract module/attribute names, then uses
        ``IntrospectionTool`` to inspect the actual object in a sandboxed
        subprocess.

        Returns:
            Formatted introspection result string for prompt injection,
            or ``None`` if introspection couldn't resolve or budget is exhausted.
        """
        # Check cumulative budget first
        if self._introspection_budget_remaining <= 0:
            return None

        # Extract module and attribute names from error text
        module_name = self._extract_module_from_error(error_text)
        if not module_name:
            return None

        # Deduct from budget (each call costs up to 10s timeout)
        self._introspection_budget_remaining -= 10.0

        tool = IntrospectionTool(workspace=Path.cwd())

        attr_name = self._extract_attribute_from_error(error_text)
        if attr_name:
            result = tool.resolve_attribute_error(module_name, attr_name)
        else:
            result = tool.inspect_module(module_name)

        if result.error and not result.attributes:
            # Introspection also failed → return None (will escalate instead)
            return None

        return IntrospectionTool.format_for_prompt(result)

    @staticmethod
    def _extract_module_from_error(error_text: str) -> str | None:
        """Extract module name from an error message.

        Handles patterns like:
        - ``AttributeError: module 'X' has no attribute 'Y'``
        - ``ModuleNotFoundError: No module named 'X'``
        - ``AttributeError: 'X' object has no attribute 'Y'``

        Args:
            error_text: The error message text.

        Returns:
            The extracted module name, or ``None`` if not found.
        """
        # Pattern 1: module 'X' has no attribute 'Y'
        m = re.search(r"module\s+'([^']+)'", error_text)
        if m:
            return m.group(1)

        # Pattern 2: No module named 'X'
        m = re.search(r"No module named '([^']+)'", error_text)
        if m:
            return m.group(1)

        # Pattern 3: 'X' object has no attribute 'Y'
        m = re.search(r"'([^']+)' object has no attribute", error_text)
        if m:
            return m.group(1)

        return None

    @staticmethod
    def _extract_attribute_from_error(error_text: str) -> str | None:
        """Extract attribute name from an error message.

        Handles pattern: ``has no attribute 'Y'``

        Args:
            error_text: The error message text.

        Returns:
            The extracted attribute name, or ``None`` if not found.
        """
        m = re.search(r"has no attribute '([^']+)'", error_text)
        if m:
            return m.group(1)
        return None

    # -------------------------------------------------------------------
    # Escalation Policy
    # -------------------------------------------------------------------

    def _check_escalation_policy(
        self,
        attempt: int,
        error_chunk: ErrorChunkSummary,
        pytest_output: str,
        prev_error_hash: str | None,
        task: Any,
    ) -> HelpRequestSchema | None:
        """Check all escalation conditions. Returns HelpRequestSchema if triggered."""

        # 1. Cyclic Error Detection
        current_hash = _error_fingerprint(pytest_output)
        if prev_error_hash is not None and current_hash == prev_error_hash:
            return HelpRequestSchema(
                task_id=task.task_id,
                reason=EscalationReason.CYCLIC_ERROR,
                confidence=_estimate_confidence(attempt, error_chunk),
                attempts=[e.action_taken for e in error_chunk.entries],
                needed_information=["Same error pattern repeated; task requires human intervention"],
            )

        # 2. Context Missing Detection
        if "AttributeError" in pytest_output or "ModuleNotFoundError" in pytest_output:
            # Simplified check — in production, verify against rag_context
            return HelpRequestSchema(
                task_id=task.task_id,
                reason=EscalationReason.CONTEXT_MISSING,
                confidence=_estimate_confidence(attempt, error_chunk),
                attempts=[e.action_taken for e in error_chunk.entries],
                needed_information=["Missing class/module referenced in error"],
            )

        # 3. Confidence Drop Detection
        confidence = _estimate_confidence(attempt, error_chunk)
        if confidence < self.escalation_confidence_threshold:
            return HelpRequestSchema(
                task_id=task.task_id,
                reason=EscalationReason.CONFIDENCE_DROP,
                confidence=confidence,
                attempts=[e.action_taken for e in error_chunk.entries],
                needed_information=[
                    f"Confidence dropped to {confidence} (threshold: {self.escalation_confidence_threshold})"
                ],
            )

        return None

    @staticmethod
    def _classify_error(output: str) -> str:
        """Extract error type from check output (supports Pytest, Ruff, Mypy)."""
        if "Ruff Lint Failures" in output or "Ruff check failed" in output:
            return "RuffLintError"
        if "Mypy Type Failures" in output or "Mypy check failed" in output:
            return "MypyTypeError"
        if "ImportViolation" in output or "Unauthorized import" in output:
            return "ImportViolation"
        import re

        for line in output.splitlines():
            # Match error type patterns: AssertionError, TypeError, ValueError, etc.
            m = re.search(r"(\w+(?:Error|Exception|Warning))", line)
            if m:
                return m.group(1)
        # Fallback markers
        for marker in ["FAILED", "assert", "SyntaxError", "TypeError", "ValueError"]:
            if marker in output:
                return marker
        return "UnknownError"

    @staticmethod
    def _error_module(pytest_output: str, affected_modules: list[str]) -> str:
        """Extract error module from pytest output."""
        for mod in affected_modules:
            if mod in pytest_output:
                return mod
        for line in pytest_output.splitlines():
            if line.startswith("FAILED"):
                parts = line.split("::")
                if len(parts) > 0:
                    return parts[0].replace("FAILED ", "")
        return "unknown"

    # -------------------------------------------------------------------
    # Reflection Log
    # -------------------------------------------------------------------

    def _update_reflection_log(self, task: Any, error_chunk: ErrorChunkSummary, success: bool) -> None:
        """Update both global and project-local reflection logs (v4.0 Phase 1)."""
        try:
            from datetime import datetime

            if not error_chunk.entries:
                return  # No failures to reflect on

            # Determine root cause and actionable tactic
            error_types = list({e.error_type for e in error_chunk.entries})
            root_cause = error_types[0] if error_types else "Unknown"
            tactic = f"Watch for {root_cause} — verify {error_chunk.entries[0].module} before running tests"

            entry = ReflectionEntry(
                task_id=task.task_id,
                timestamp=datetime.now(UTC).isoformat(),
                trigger=f"{'Success' if success else 'Failure'} after {error_chunk.total_retries} retries",
                root_cause=root_cause,
                actionable_tactic=tactic,
                error_types_encountered=error_types,
            )

            # Global reflection log (model-level)
            self._append_global_reflection(entry)

            # Project-local reflection log
            self._append_project_reflection(entry)

        except Exception:
            pass  # Reflection logging is best-effort

    def _append_global_reflection(self, entry: ReflectionEntry) -> None:
        """Append to global (model-level) reflection log."""
        try:
            model_key = self.model.replace("/", "_").replace(":", "_")
            path = _GLOBAL_REFLECTIONS_DIR / f"{model_key}_tactics.json"
            path.parent.mkdir(parents=True, exist_ok=True)

            log = self._load_reflection_log(path, model_name=self.model)
            log.entries.append(entry)
            # Keep only last 50 entries
            log.entries = log.entries[-50:]
            path.write_text(json.dumps(log.model_dump(), indent=2, ensure_ascii=False))
        except Exception:
            pass

    def _append_project_reflection(self, entry: ReflectionEntry) -> None:
        """Append to project-local reflection log if project-specific."""
        try:
            # Determine if project-specific
            project_pkgs = self._get_project_packages()
            if not _is_project_specific(entry.actionable_tactic, project_pkgs):
                return

            path = _PROJECT_REFLECTIONS_DIR / "project_context.json"
            path.parent.mkdir(parents=True, exist_ok=True)

            log = self._load_reflection_log(path, model_name="project_context")
            log.entries.append(entry)
            log.entries = log.entries[-30:]
            path.write_text(json.dumps(log.model_dump(), indent=2, ensure_ascii=False))
        except Exception:
            pass

    @staticmethod
    def _load_reflection_log(path: Path, model_name: str) -> ReflectionLog:
        """Load existing reflection log or create new."""
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return ReflectionLog(**data)
            except Exception:
                pass
        return ReflectionLog(model_name=model_name)

    @staticmethod
    def _get_project_packages() -> list[str]:
        """Get list of project package names from pyproject.toml or directory listing."""
        pkg_names = []
        if Path("pyproject.toml").exists():
            try:
                import tomllib

                data = tomllib.loads(Path("pyproject.toml").read_text())
                # Try to extract package name
                if "project" in data and "name" in data["project"]:
                    pkg_names.append(data["project"]["name"])
            except Exception:
                pass
        return pkg_names

    @staticmethod
    def get_recent_tactics(model: str = "ollama/qwen2.5-coder:7b", max_global: int = 5, max_project: int = 3) -> str:
        """Collect recent tactics for prompt injection (v4.0 Phase 1)."""
        lines: list[str] = ["[PAST LESSONS - APPLY THESE FIRST]"]

        # Global tactics
        model_key = model.replace("/", "_").replace(":", "_")
        global_path = _GLOBAL_REFLECTIONS_DIR / f"{model_key}_tactics.json"
        if global_path.exists():
            try:
                data = json.loads(global_path.read_text())
                log = ReflectionLog(**data)
                lines.append("[GLOBAL]")
                for entry in log.entries[-max_global:]:
                    lines.append(f"- {entry.actionable_tactic}")
            except Exception:
                pass

        # Project tactics
        project_path = _PROJECT_REFLECTIONS_DIR / "project_context.json"
        if project_path.exists():
            try:
                data = json.loads(project_path.read_text())
                log = ReflectionLog(**data)
                lines.append(f"[PROJECT: {Path.cwd().name}]")
                for entry in log.entries[-max_project:]:
                    lines.append(f"- {entry.actionable_tactic}")
            except Exception:
                pass

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Quick self-test
    pass
