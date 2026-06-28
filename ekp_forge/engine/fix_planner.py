"""Fix Planner — Phase 2/4 control component for prioritised, serial fix dispatch.

Phase 2
-------
The Fix Planner receives ``list[Diagnostic]`` from the Verification role,
applies priority ordering (Syntax > Import > Type > Test), and generates
focused ``FixTask`` instances that address **only** the highest-priority
issues at each step. The Worker receives one ``FixTask`` at a time.

This prevents cognitive overload in lightweight (7B-class) models by:
1. Eliminating "fix all errors at once" prompts.
2. Providing explicit, bounded instructions per fix cycle.
3. Filtering out auto-fixable items that were already resolved by
   ``ruff check --fix`` before IR generation.

Phase 4 additions:
- **SymbolResolver**: AST-based line-number → symbol-name resolution.
- **``plan_v2()``**: Generates ``FixTaskV2`` instances from tiered diagnostic
  results, enabling function-level isolation via LibCST slicing.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ekp_forge.engine.tiered_diagnostic import (
    DiagnosticStage,
    TieredDiagnosticResult,
)
from ekp_forge.schemas.contract import (
    CATEGORY_PRIORITY,
    Diagnostic,
    DiagnosticCategory,
    FixTask,
    FixTaskV2,
    WorkerContract,
    diagnostic_priority,
    filter_auto_fixable,
    group_diagnostics_by_priority,
)

# -- Constants ----------------------------------------------------------------

# Maximum line distance for co-location merging (same file, within N lines)
_CO_LOCATION_LINE_THRESHOLD = 5

# Max diagnostics included in the instruction error summary
_MAX_ERRORS_IN_INSTRUCTION = 10


# ---------------------------------------------------------------------------
# FixHistory — stateless context bridge across iterations
# ---------------------------------------------------------------------------


@dataclass
class FixHistoryEntry:
    """Record of what was fixed in one iteration.

    This accumulates across ``FixPlanner.plan()`` calls so that each new
    ``FixTask`` instruction includes a "previously fixed" reminder that
    prevents the Worker from reverting earlier fixes.
    """

    task_id: str
    priority: int
    files: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Fix Planner
# ---------------------------------------------------------------------------


class FixPlanner:
    """Plans and sequences fix tasks from a list of ``Diagnostic`` items.

    Phase 2.1 adds:
    - ``_history``: accumulates completed fix context to prevent regressions.
    - Co-location merging: same-file, near-line diagnostics from different
      priority levels are merged into a single FixTask.

    Phase 4 adds:
    - ``plan_v2()``: generates ``FixTaskV2`` instances from tiered diagnostic
      results, enabling function-level isolation.
    """

    def __init__(self, contract: WorkerContract) -> None:
        """Initialise the Fix Planner with an active ``WorkerContract``.

        Args:
            contract: The ``WorkerContract`` that defines the implementation
                     scope. Generated ``FixTask`` instances will reference
                     this contract.
        """
        self._contract = contract
        self._completed_task_ids: set[str] = set()

        # Phase 2.1: FixHistory accumulates context across iterations
        self._history: list[FixHistoryEntry] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(self, diagnostics: list[Diagnostic]) -> list[FixTask]:
        """Plan fix tasks from a list of ``Diagnostic`` items.

        Process:
        1. Filter out auto-fixable diagnostics (already handled by auto-fix).
        2. Group by priority (1=highest, 4=lowest).
        3. For the highest non-empty priority group, **merge co-located**
           diagnostics from lower-priority groups (same file, within
           ``_CO_LOCATION_LINE_THRESHOLD`` lines).
        4. Generate one ``FixTask`` with FixHistory context prepended.

        Returns an empty list if no diagnostics remain after auto-fixable
        filtering.

        Args:
            diagnostics: All ``Diagnostic`` items from the verification pipeline.

        Returns:
            List of ``FixTask`` instances to dispatch (typically one element).
            Empty list if nothing to fix.
        """
        # Step 1: Remove auto-fixable items
        remaining = filter_auto_fixable(diagnostics)
        if not remaining:
            return []

        # Step 2: Group by priority
        grouped = group_diagnostics_by_priority(remaining)

        # Step 3: Find the highest-priority non-empty group
        target_priority: int | None = None
        for pri in range(1, 5):  # priorities 1-4
            if grouped[pri]:
                target_priority = pri
                break

        if target_priority is None:
            return []

        # Step 4: Merge co-located diagnostics from lower-priority groups
        # into the target group to reduce iteration count.
        target_diagnostics = list(grouped[target_priority])
        for lower_pri in range(target_priority + 1, 5):
            for lower_d in grouped[lower_pri]:
                if self._is_co_located_with_any(lower_d, target_diagnostics):
                    target_diagnostics.append(lower_d)

        instruction = self._build_instruction(target_priority, target_diagnostics)

        fix_task = FixTask(
            task_id=self._generate_task_id(),
            contract=self._contract,
            diagnostics=target_diagnostics,
            priority=target_priority,
            instruction=instruction,
        )

        # Record in FixHistory
        self._record_history(fix_task, target_diagnostics)

        self._completed_task_ids.add(fix_task.task_id)
        return [fix_task]

    def has_work(self, diagnostics: list[Diagnostic]) -> bool:
        """Quick check if there are any non-auto-fixable diagnostics."""
        return len(filter_auto_fixable(diagnostics)) > 0

    @property
    def contract(self) -> WorkerContract:
        """Return the active contract."""
        return self._contract

    @property
    def history(self) -> list[FixHistoryEntry]:
        """Return the accumulated fix history."""
        return list(self._history)

    def reset_history(self) -> None:
        """Clear the fix history (for starting a fresh fix cycle)."""
        self._history.clear()

    # ------------------------------------------------------------------
    # Internal — Co-location detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_co_located_with_any(
        candidate: Diagnostic, targets: list[Diagnostic]
    ) -> bool:
        """Check if *candidate* is co-located with any diagnostic in *targets*.

        Two diagnostics are co-located if they share the same file and their
        line numbers are within ``_CO_LOCATION_LINE_THRESHOLD``. If either
        has line == 0 (unknown line), co-location is NOT assumed to avoid
        excessive merging with unparseable diagnostics.

        Args:
            candidate: The diagnostic to test for co-location.
            targets:   Already-selected diagnostics to compare against.

        Returns:
            True if co-located with any target.
        """
        if candidate.line == 0:
            return False
        for t in targets:
            if t.line == 0:
                continue
            if t.file == candidate.file and abs(t.line - candidate.line) <= _CO_LOCATION_LINE_THRESHOLD:
                return True
        return False

    # ------------------------------------------------------------------
    # Internal — Instruction building with FixHistory
    # ------------------------------------------------------------------

    def _build_instruction(
        self, priority: int, diagnostics: list[Diagnostic]
    ) -> str:
        """Build a concise, bounded instruction for the Worker.

        The instruction includes:
        - Priority label and contract reference.
        - **FixHistory**: what was fixed in previous iterations (prevents
          ping-pong regression).
        - Specific files and error codes.
        - Compressed error messages (capped at ``_MAX_ERRORS_IN_INSTRUCTION``).

        Args:
            priority: The numeric priority level (1-4).
            diagnostics: The diagnostics to address in this task.

        Returns:
            A concise instruction string (≤ 1500 chars).
        """
        priority_label = {1: "Syntax / Security", 2: "Import / Names", 3: "Type", 4: "Tests"}.get(
            priority, "Fixes"
        )

        # Collect unique files
        files = sorted(set(d.file for d in diagnostics if d.file))
        codes = sorted(set(d.code for d in diagnostics if d.code))

        # Build a compact error summary
        error_lines: list[str] = []
        for d in diagnostics[:_MAX_ERRORS_IN_INSTRUCTION]:
            loc = f"{d.file}:{d.line}" if d.line else d.file
            error_lines.append(f"  - {loc}: {d.message[:120]}")

        if len(diagnostics) > _MAX_ERRORS_IN_INSTRUCTION:
            error_lines.append(
                f"  ... and {len(diagnostics) - _MAX_ERRORS_IN_INSTRUCTION} more issues"
            )

        # -- FixHistory: prepend context from previous iterations ----------
        history_block: list[str] = []
        if self._history:
            history_block.append("[PREVIOUSLY FIXED — DO NOT REVERT]")
            for entry in self._history:
                files_str = ", ".join(entry.files[:3])
                if len(entry.files) > 3:
                    files_str += f" ... (+{len(entry.files) - 3})"
                history_block.append(
                    f"  Iteration {entry.task_id} (P{entry.priority}): "
                    f"{entry.summary[:100]}"
                )
            history_block.append("")

        parts = [
            f"[Fix Task — Priority {priority}: {priority_label}]",
            f"Contract: {self._contract.contract_id}",
            f"Objective: {self._contract.objective}",
            "",
            *history_block,
            f"Files to modify: {', '.join(files) if files else '(automatic)'}",
            f"Error codes: {', '.join(codes) if codes else '(various)'}",
            "",
            "Fix the errors below. Do NOT modify any file outside the Contract's",
            f"target_files ({', '.join(self._contract.target_files)}).",
            "Do NOT change forbidden symbols.",
            "Do NOT revert any code that was fixed in previous iterations (see above).",
            "",
            "Errors to fix (one priority level at a time):",
            *error_lines,
            "",
            "After fixing, the verification pipeline will check for remaining issues.",
        ]

        instruction = "\n".join(parts)
        # Hard cap at 2000 chars (matching FixTask.instruction max_length)
        if len(instruction) > 2000:
            instruction = instruction[:1997] + "..."

        return instruction

    def _record_history(
        self, task: FixTask, diagnostics: list[Diagnostic]
    ) -> None:
        """Record a completed fix iteration in FixHistory."""
        files = sorted(set(d.file for d in diagnostics if d.file))
        codes = sorted(set(d.code for d in diagnostics if d.code))
        # Build a concise summary from the first 3 diagnostics
        first_msgs = [d.message[:60] for d in diagnostics[:3]]
        summary = "; ".join(first_msgs)
        if len(diagnostics) > 3:
            summary += f" ... (+{len(diagnostics) - 3} more)"

        self._history.append(
            FixHistoryEntry(
                task_id=task.task_id,
                priority=task.priority,
                files=files,
                error_codes=codes,
                summary=summary,
            )
        )

    @staticmethod
    def _generate_task_id() -> str:
        """Generate a deterministic ``FixTask`` ID."""
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        hash_input = f"fix-{timestamp}-{datetime.now(UTC).microsecond}"
        task_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:6]
        return f"FT-{timestamp}-{task_hash}"

    # ------------------------------------------------------------------
    # Phase 4: plan_v2 — contract-driven repair
    # ------------------------------------------------------------------

    def plan_v2(
        self,
        tiered_result: TieredDiagnosticResult,
    ) -> list[FixTaskV2]:
        """Plan fix tasks from a tiered diagnostic result.

        Phase 4 enhancement: resolves diagnostics to symbol names via AST
        and generates ``FixTaskV2`` instances with ``target_symbol`` and
        ``editable_scope`` for LibCST-based function isolation.

        Args:
            tiered_result: Result from ``TieredDiagnosticRunner.run()``.

        Returns:
            List of ``FixTaskV2`` instances (one per unique symbol).
            Empty list if no diagnostics remain.
        """
        diagnostics = tiered_result.diagnostics
        if not diagnostics:
            return []

        # Resolve diagnostics to symbols via AST
        symbol_map = SymbolResolver.resolve_symbols_from_diagnostics(diagnostics)

        if not symbol_map:
            # Fallback: create a single task targeting the first diagnostic's file
            first = diagnostics[0]
            return [
                FixTaskV2(
                    task_id=self._generate_v2_task_id(),
                    target_file=first.file,
                    target_symbol="<unknown>",
                    editable_scope="function",
                    diagnostics=diagnostics,
                    acceptance=self._build_v2_acceptance(tiered_result.stage),
                )
            ]

        # Group diagnostics by symbol
        symbol_diagnostics: dict[str, list[Diagnostic]] = {}
        for key in symbol_map:
            symbol_diagnostics[key] = []

        for d in diagnostics:
            try:
                symbol, scope = SymbolResolver.resolve_symbol(d.file, d.line)
                key = f"{d.file}:{symbol}"
                if key in symbol_diagnostics:
                    symbol_diagnostics[key].append(d)
                else:
                    # Fallback: add to first symbol in same file
                    for skey in symbol_diagnostics:
                        if skey.startswith(d.file):
                            symbol_diagnostics[skey].append(d)
                            break
            except (ValueError, OSError):
                # Fallback: add to first symbol in same file
                for skey in symbol_diagnostics:
                    if skey.startswith(d.file):
                        symbol_diagnostics[skey].append(d)
                        break

        # Generate one FixTaskV2 per unique symbol
        tasks: list[FixTaskV2] = []
        for key, sym_diags in symbol_diagnostics.items():
            file_path, symbol_name, scope_type = symbol_map[key]
            tasks.append(
                FixTaskV2(
                    task_id=self._generate_v2_task_id(),
                    target_file=file_path,
                    target_symbol=symbol_name,
                    editable_scope=scope_type,
                    diagnostics=sym_diags,
                    acceptance=self._build_v2_acceptance(tiered_result.stage),
                )
            )

        return tasks

    def _generate_v2_task_id(self) -> str:
        """Generate a ``FixTaskV2`` ID."""
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        hash_input = f"ftv2-{timestamp}-{datetime.now(UTC).microsecond}"
        task_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:6]
        return f"FTV2-{timestamp}-{task_hash}"

    @staticmethod
    def _build_v2_acceptance(stage: DiagnosticStage) -> list[str]:
        """Build acceptance criteria based on the current diagnostic stage."""
        criteria: list[str] = []
        if stage in (DiagnosticStage.RUFF, DiagnosticStage.MYPY):
            criteria.append(f"No {stage.value} errors after fix")
        if stage == DiagnosticStage.MYPY:
            criteria.append("Type-correct with strict mypy")
        if stage == DiagnosticStage.PYTEST:
            criteria.append("All tests pass")
        return criteria


# ---------------------------------------------------------------------------
# SymbolResolver — AST-based line → symbol resolution
# ---------------------------------------------------------------------------


class SymbolResolver:
    """Resolves a line number to a symbol (function/class-method) name using AST.

    Phase 4: This is the bridge between line-based diagnostics (Ruff/Mypy report
    line numbers) and function-level isolation (LibCST needs symbol names).
    """

    @staticmethod
    def resolve_symbol(
        file_path: str, line_number: int
    ) -> tuple[str, Literal["function", "class_method"]]:
        """Given a file path and line number, find the enclosing symbol name.

        Walks the AST to find which function (or class method) contains
        the given line.

        Args:
            file_path:    Path to the Python source file.
            line_number:  The 1-based line number of the diagnostic.

        Returns:
            Tuple of (symbol_name, scope_type).

        Raises:
            ValueError: If the file cannot be parsed or no enclosing symbol found.
        """
        path = Path(file_path)
        if not path.exists():
            raise ValueError(f"File not found: {file_path}")

        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            raise ValueError(f"Cannot parse {file_path}: {e}") from e

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                # Check class methods
                for item in ast.iter_child_nodes(node):
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        end_line = getattr(item, "end_lineno", item.lineno) or item.lineno
                        if item.lineno <= line_number <= end_line:
                            return (f"{node.name}.{item.name}", "class_method")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
                if node.lineno <= line_number <= end_line:
                    return (node.name, "function")

        raise ValueError(
            f"No enclosing symbol found at {file_path}:{line_number}"
        )

    @staticmethod
    def resolve_symbols_from_diagnostics(
        diagnostics: list[Diagnostic],
    ) -> dict[str, tuple[str, str, Literal["function", "class_method"]]]:
        """Resolve file:symbol mappings from a list of diagnostics.

        Iterates through diagnostics, resolves each to a symbol name via
        AST, and returns a deduplicated mapping.

        Args:
            diagnostics: List of ``Diagnostic`` instances.

        Returns:
            Dict mapping ``"file_path:symbol_name"`` to
            ``(file_path, symbol_name, scope_type)``.
        """
        result: dict[str, tuple[str, str, Literal["function", "class_method"]]] = {}
        for d in diagnostics:
            if not d.file or d.line == 0:
                continue
            try:
                symbol, scope = SymbolResolver.resolve_symbol(d.file, d.line)
                key = f"{d.file}:{symbol}"
                if key not in result:
                    result[key] = (d.file, symbol, scope)
            except (ValueError, OSError):
                continue
        return result
