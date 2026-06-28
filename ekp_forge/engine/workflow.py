"""WorkflowEngine — central workflow orchestrator.

Phase 1 provides a minimal linear workflow that routes through the
protocol layer.

Phase 2 introduces the Fix Planner loop: after implementation, the
Verification role returns structured ``Diagnostic`` items (Verification IR),
and the Fix Planner prioritises them and dispatches focused ``FixTask``
instances to the Worker one priority level at a time.

Phase 4 introduces ``run_with_fix_loop_v2()`` which replaces the
Phase 2 flat verification loop with the **contract-driven repair
pipeline**: tiered diagnostic → FixTaskV2 → hint generation →
function-level isolation (LibCST slicing) → patch validation →
Manager escalation.

DESIGN RULES:
1. **No exception wrapping**: All agent exceptions propagate as-is.
2. **Shared context**: ``_context`` dict enables state accumulation across
   sequential role executions.
3. **Single-direction dependency**: Engine depends on protocol + agents;
   never the reverse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ekp_forge.agents.registry import AgentRegistry
from ekp_forge.engine.dispatcher import Dispatcher
from ekp_forge.engine.fix_planner import FixPlanner
from ekp_forge.engine.tiered_diagnostic import (
    DiagnosticStage,
    TieredDiagnosticRunner,
)
from ekp_forge.protocol.assignment import OrganizationProfile
from ekp_forge.protocol.roles import Role
from ekp_forge.sandbox.hint_generator import HintGenerator
from ekp_forge.sandbox.patch_validator import PatchValidator
from ekp_forge.sandbox.slicer import FunctionSlicer
from ekp_forge.schemas.contract import (
    Diagnostic,
    FixTask,
    FixTaskV2,
    WorkerContract,
)


class WorkflowEngine:
    """Central workflow orchestrator that routes roles to agents.

    Phase 2 adds ``run_with_fix_loop()`` which wraps the implementation
    and verification roles in a Fix Planner-controlled loop.

    Usage::

        engine = WorkflowEngine(profile, registry)
        triage_result = engine.run(Role.REQUIREMENT_REVIEW, {"task": task})
        plan = engine.run(Role.PLANNING, {"task": task, "triage_result": triage_result})
        impl_result = engine.run(Role.IMPLEMENTATION, {"task": task, "plan": plan})
    """

    def __init__(self, profile: OrganizationProfile, registry: AgentRegistry) -> None:
        """Initialize the workflow engine.

        Args:
            profile:  The active OrganizationProfile.
            registry: The AgentRegistry with all registered agents.
        """
        self._profile = profile
        self._dispatcher = Dispatcher(profile, registry)
        # Shared context accumulates state across sequential role executions.
        self._context: dict[str, Any] = {}

    def run(self, role: Role, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute the given role by dispatching to assigned agent(s).

        The engine merges the shared ``_context`` with the provided
        ``context`` (role-specific context takes precedence), then
        dispatches to the agent(s) assigned to this role via the
        active ``OrganizationProfile``.

        Args:
            role:    The role to execute.
            context: Role-specific context. Will be merged with the
                     engine's shared context.

        Returns:
            A single result dict. If multiple agents are assigned to the
            same role, returns only the last agent's result. (Use
            ``run_all()`` to collect all results.)

        Raises:
            ValueError: If no agent is registered for the assigned role.
            Propagates all underlying exceptions transparently.
        """
        merged = dict(self._context)
        if context:
            merged.update(context)
        merged["_role"] = role
        # Phase 4.5: Inject execution mode from profile into agent context
        merged["execution_mode"] = self._profile.mode

        results = self._dispatcher.dispatch(role, merged)

        final = results[-1] if results else {}
        self._context.update(final)
        return final

    def run_all(self, role: Role, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute role and return ALL agent results.

        Unlike ``run()`` which returns only the last result, this method
        returns results from all agents assigned to the role.

        Args:
            role:    The role to execute.
            context: Role-specific context.

        Returns:
            List of result dicts, one per assigned agent.
        """
        merged = dict(self._context)
        if context:
            merged.update(context)
        merged["_role"] = role

        results = self._dispatcher.dispatch(role, merged)
        for result in results:
            self._context.update(result)
        return results

    # ------------------------------------------------------------------
    # Phase 2: Fix Planner integration
    # ------------------------------------------------------------------

    def run_with_fix_loop(
        self,
        task: Any,
        contract: WorkerContract,
        plan: str,
        max_iterations: int = 5,
    ) -> dict[str, Any]:
        """Execute implementation + verification with Fix Planner control.

        This method implements the Phase 2 fix loop:

        1. Run the IMPLEMENTATION role with the initial ``WorkerContract``.
        2. Run the VERIFICATION role to collect ``list[Diagnostic]``.
        3. Feed diagnostics to the ``FixPlanner`` for prioritisation.
        4. If remaining issues exist, dispatch a ``FixTask`` to the Worker.
        5. Repeat until all priority levels are resolved or max_iterations.

        Args:
            task:            The ``TaskSchema`` instance.
            contract:        The ``WorkerContract`` constraining Worker scope.
            plan:            The implementation plan text.
            max_iterations:  Maximum fix loop iterations (default: 5).

        Returns:
            Result dict with keys:
            - ``"status"``: ``"success"`` | ``"failed"``
            - ``"fix_tasks_completed"``: list of ``FixTask.task_id``
            - ``"remaining_diagnostics"``: list[Diagnostic] (if any)
            - ``"impl_result"``: dict (last implementation result)
            - ``"verification_result"``: dict (last verification result)
        """
        planner = FixPlanner(contract)
        completed_task_ids: list[str] = []
        impl_result: dict[str, Any] = {}
        verification_result: dict[str, Any] = {}

        # Phase 1: Initial implementation
        # Phase 3: Pass knowledge_context from contract to Worker
        rag_context = contract.knowledge_context if hasattr(contract, "knowledge_context") else ""
        impl_context = {
            "task": task,
            "plan": plan,
            "worker_contract": contract,
            "_rag_context": rag_context,
        }
        impl_result = self.run(Role.IMPLEMENTATION, impl_context)
        if impl_result.get("status") in ("failed", "escalated"):
            return {
                "status": impl_result["status"],
                "fix_tasks_completed": completed_task_ids,
                "remaining_diagnostics": [],
                "impl_result": impl_result,
                "verification_result": verification_result,
            }

        # Phase 2: Fix loop
        for iteration in range(1, max_iterations + 1):
            # Run verification
            ver_context = {
                "task": task,
                "impl_result": impl_result,
                "worker_contract": contract,
            }
            verification_result = self.run(Role.VERIFICATION, ver_context)

            # Extract diagnostics from verification result
            diagnostics: list[Diagnostic] = verification_result.get("diagnostics", [])

            # Ask Fix Planner if there's work
            if not planner.has_work(diagnostics):
                return {
                    "status": "success",
                    "fix_tasks_completed": completed_task_ids,
                    "remaining_diagnostics": [],
                    "impl_result": impl_result,
                    "verification_result": verification_result,
                }

            # Plan fix tasks
            fix_tasks = planner.plan(diagnostics)
            if not fix_tasks:
                # No non-auto-fixable items remain
                return {
                    "status": "success",
                    "fix_tasks_completed": completed_task_ids,
                    "remaining_diagnostics": diagnostics,
                    "impl_result": impl_result,
                    "verification_result": verification_result,
                }

            # Dispatch each fix task to the Worker
            for fix_task in fix_tasks:
                completed_task_ids.append(fix_task.task_id)
                # Phase 3: Pass knowledge_context on each fix iteration
                rag_context = contract.knowledge_context if hasattr(contract, "knowledge_context") else ""
                fix_context = {
                    "task": task,
                    "fix_task": fix_task,
                    "worker_contract": contract,
                    "plan": fix_task.instruction,
                    "_rag_context": rag_context,
                }
                impl_result = self.run(Role.IMPLEMENTATION, fix_context)
                if impl_result.get("status") in ("failed", "escalated"):
                    return {
                        "status": impl_result["status"],
                        "fix_tasks_completed": completed_task_ids,
                        "remaining_diagnostics": diagnostics,
                        "impl_result": impl_result,
                        "verification_result": verification_result,
                    }

        # Exhausted iterations
        return {
            "status": "failed",
            "fix_tasks_completed": completed_task_ids,
            "remaining_diagnostics": [],
            "impl_result": impl_result,
            "verification_result": verification_result,
        }

    # ------------------------------------------------------------------
    # Phase 4: Contract-driven repair with function-level isolation
    # ------------------------------------------------------------------

    def run_with_fix_loop_v2(
        self,
        task: Any,
        contract: WorkerContract,
        plan: str,
        max_iterations: int = 5,
        max_escalations: int = 2,
    ) -> dict[str, Any]:
        """Phase 4: Contract-driven repair with function-level isolation.

        This method implements the full Phase 4 pipeline:

        1. Initial implementation (standard IMPLEMENTATION role).
        2. Tiered Diagnostic (Ruff → Mypy → Pytest with early exit).
        3. FixTaskV2 building with AST-based symbol resolution.
        4. Hint Generation per error type (deterministic, no LLM).
        5. Function-level isolation via LibCST slicer (Worker sees only
           the sliced function, never the full file).
        6. Patch validation against scope (reject if scope violated).
        7. Re-verification.
        8. Manager escalation for logical errors (Pytest failures).

        Args:
            task:              The ``TaskSchema`` instance.
            contract:          The ``WorkerContract`` constraining scope.
            plan:              The implementation plan text.
            max_iterations:    Maximum fix loop iterations (default: 5).
            max_escalations:   Maximum Manager escalations (default: 2).

        Returns:
            Result dict with keys:
            - ``"status"``: ``"success"`` | ``"failed"`` | ``"contract_redesign"``
              | ``"escalate_human"``
            - ``"fix_tasks_completed"``: list of task IDs
            - ``"impl_result"``: dict (last implementation result)
            - ``"stage"``: final ``DiagnosticStage``
        """
        planner = FixPlanner(contract)
        diagnostic_runner = TieredDiagnosticRunner()
        hint_generator = HintGenerator()
        slicer = FunctionSlicer()
        patch_validator = PatchValidator()

        completed_task_ids: list[str] = []
        escalation_count = 0
        impl_result: dict[str, Any] = {}
        last_fix_task: FixTaskV2 | None = None

        # Step 1: Initial implementation
        rag_context = contract.knowledge_context if hasattr(contract, "knowledge_context") else ""
        impl_context = {
            "task": task,
            "plan": plan,
            "worker_contract": contract,
            "_rag_context": rag_context,
        }
        impl_result = self.run(Role.IMPLEMENTATION, impl_context)
        if impl_result.get("status") in ("failed", "escalated"):
            return self._build_v2_result("failed", completed_task_ids, impl_result=impl_result)

        # Step 2-8: Fix loop with Phase 4 pipeline
        for iteration in range(1, max_iterations + 1):
            # Step 2: Tiered Diagnostic (Ruff → Mypy → Pytest with early exit)
            tiered_result = diagnostic_runner.run(
                changed_files=task.affected_modules,
            )

            if tiered_result.passed:
                return self._build_v2_result("success", completed_task_ids, impl_result=impl_result)

            # Step 3: Build FixTaskV2 from tiered result
            fix_tasks = planner.plan_v2(tiered_result)
            if not fix_tasks:
                # Only auto-fixable items remain
                return self._build_v2_result("success", completed_task_ids, impl_result=impl_result)

            for fix_task in fix_tasks:
                last_fix_task = fix_task
                completed_task_ids.append(fix_task.task_id)

                # Step 4: Generate hints per error type
                hints = hint_generator.generate_hints(fix_task.diagnostics)
                fix_task.references = hints

                # Step 5: Function-level isolation via LibCST
                original_source = self._read_file_content(fix_task.target_file)
                if original_source is None:
                    return self._build_v2_result(
                        "failed",
                        completed_task_ids,
                        error=f"Cannot read {fix_task.target_file}",
                        impl_result=impl_result,
                    )

                sliced_function = slicer.extract_function(
                    original_source,
                    fix_task.target_symbol,
                )

                # Dispatch to Worker with sliced context
                worker_result = self._run_v2_worker_fix(
                    task,
                    fix_task,
                    sliced_function,
                    contract,
                    rag_context,
                )

                if worker_result.get("status") == "failed":
                    continue

                # Read Worker's fixed code (if slice was used)
                fixed_function = self._read_worker_fix_output(fix_task.target_file)

                # Step 6: Patch Validation
                if fixed_function is not None and sliced_function is not None:
                    validation = patch_validator.validate(
                        original_source=original_source,
                        fixed_source=fixed_function,
                        target_symbol=fix_task.target_symbol,
                    )

                    if not validation.accepted:
                        # REJECT: return to Worker with rejection feedback
                        self._reject_and_retry(fix_task, validation.details)
                        continue

                    # Inject fixed function back into original file
                    inject_ok = slicer.inject_fix_to_file(
                        fix_task.target_file,
                        fix_task.target_symbol,
                        fixed_function,
                    )
                    if not inject_ok:
                        return self._build_v2_result(
                            "failed",
                            completed_task_ids,
                            error=f"Failed to inject fix for {fix_task.target_symbol}",
                            impl_result=impl_result,
                        )

            # Re-verify after all fix tasks
            recheck = diagnostic_runner.run(
                changed_files=task.affected_modules,
            )

            if recheck.passed:
                return self._build_v2_result("success", completed_task_ids, impl_result=impl_result)

            # Pytest logical errors remain → escalate to Manager
            if recheck.stage == DiagnosticStage.PYTEST and iteration >= max_iterations - 1:
                if escalation_count < max_escalations:
                    escalation_count += 1
                    manager_result = self._escalate_logical_error(
                        task,
                        last_fix_task,
                        original_source,
                        fixed_function,
                        recheck.diagnostics,
                        iteration,
                    )

                    if manager_result.get("status") == "manager_patch_applied":
                        continue  # Re-verify
                    if manager_result.get("status") == "contract_redesign":
                        return self._build_v2_result(
                            "contract_redesign",
                            completed_task_ids,
                            manager_result=manager_result,
                            impl_result=impl_result,
                        )
                    return self._build_v2_result(
                        "escalate_human",
                        completed_task_ids,
                        manager_result=manager_result,
                        impl_result=impl_result,
                    )

        return self._build_v2_result("failed", completed_task_ids, impl_result=impl_result)

    # ------------------------------------------------------------------
    # Phase 4 internal helpers
    # ------------------------------------------------------------------

    def _read_file_content(self, file_path: str) -> str | None:
        """Read a file from disk, returning None if it doesn't exist."""
        path = Path(file_path)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def _run_v2_worker_fix(
        self,
        task: Any,
        fix_task: FixTaskV2,
        sliced_function: str | None,
        contract: WorkerContract,
        rag_context: str,
    ) -> dict[str, Any]:
        """Run Worker fix with optional function-slice context.

        If a sliced function is provided, the Worker receives only that
        code (no access to the full file).
        """
        # Build fix instruction with slice context
        instruction_parts: list[str] = [
            f"[Fix Task V2: {fix_task.task_id}]",
            f"Target: {fix_task.target_file}:{fix_task.target_symbol}",
            f"Scope: {fix_task.editable_scope}",
            "",
        ]

        if fix_task.references:
            instruction_parts.append("[Hints from Hint Generator]")
            for ref in fix_task.references:
                instruction_parts.append(f"--- {ref.reference_type} ---")
                instruction_parts.append(ref.content)
            instruction_parts.append("")

        instruction_parts.extend(
            [
                "Fix the errors below. Do NOT modify anything outside the target symbol.",
                "",
                "Errors to fix:",
            ]
        )
        for d in fix_task.diagnostics:
            loc = f"{d.file}:{d.line}" if d.line else d.file
            instruction_parts.append(f"  - {loc}: {d.message[:200]}")

        instruction = "\n".join(instruction_parts)

        fix_context = {
            "task": task,
            "fix_task_v2": fix_task,
            "sliced_function": sliced_function,
            "worker_contract": contract,
            "plan": instruction,
            "_rag_context": rag_context,
        }
        return self.run(Role.IMPLEMENTATION, fix_context)

    def _read_worker_fix_output(self, file_path: str) -> str | None:
        """Read the Worker's fixed output from a temp file.

        After the Worker completes a fix on a sliced function,
        the result is written to a temp file. This reads it back.
        """
        temp_path = Path(f"._fix_temp_{Path(file_path).name}")
        if not temp_path.exists():
            return None
        content = temp_path.read_text(encoding="utf-8")
        return content

    def _reject_and_retry(self, fix_task: FixTaskV2, rejection_details: str) -> None:
        """Log a patch rejection (in production, re-queue to Worker).

        For now, the rejection is written to a log file. Integration
        with the Worker retry queue is a future enhancement.
        """
        from pathlib import Path

        log_path = Path("_patch_rejections.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[REJECT] {fix_task.task_id}: {fix_task.target_symbol}\n{rejection_details}\n\n")

    def _escalate_logical_error(
        self,
        task: Any,
        last_fix_task: FixTaskV2 | None,
        original_source: str,
        worker_fixed_source: str | None,
        diagnostics: list[Diagnostic],
        iteration_count: int,
    ) -> dict[str, Any]:
        """Escalate a logical error to the Manager for analysis."""
        from ekp_forge.manager import ManagerAgent

        manager = ManagerAgent()
        return manager.handle_logical_error_escalation(
            task=task,
            fix_task=last_fix_task,
            original_source=original_source,
            worker_fixed_source=worker_fixed_source,
            diagnostics=diagnostics,
            iteration_count=iteration_count,
        )

    @staticmethod
    def _build_v2_result(
        status: str,
        completed_task_ids: list[str],
        impl_result: dict[str, Any] | None = None,
        manager_result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """Build a standardised Phase 4 result dict."""
        result: dict[str, Any] = {
            "status": status,
            "fix_tasks_completed": completed_task_ids,
        }
        if impl_result:
            result["impl_result"] = impl_result
        if manager_result:
            result["manager_result"] = manager_result
        if error:
            result["error"] = error
        return result

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------

    def reset_context(self) -> None:
        """Reset the shared context (useful for starting a new workflow)."""
        self._context.clear()

    @property
    def profile(self) -> OrganizationProfile:
        """Return the active organization profile."""
        return self._profile

    @property
    def dispatcher(self) -> Dispatcher:
        """Return the dispatcher instance."""
        return self._dispatcher
