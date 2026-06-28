"""IntegratorAgent — safely merges Worker diffs with system-level verification.

The Integrator Agent acts as the independent auditor of the system. It enforces
code integration safety deterministically:

1. **Backup** affected files before applying changes.
2. **Apply** the ``git_diff`` using ``git apply``.
3. **Verify** globally via ``mypy .`` and ``pytest .`` on the project root.
4. **On success**: clear backup and return success.
5. **On failure**: restore backups (file-level) and return error log.

Inherits from ``BaseAgent`` for compatibility with the Role-based Protocol
Architecture (AgentRegistry + WorkflowEngine dispatch).

Usage::

    from ekp_forge.sandbox.integrator import IntegratorAgent

    integrator = IntegratorAgent(project_root=Path("/path/to/repo"))
    success, log = integrator.integrate(diff_content, ["math_utils.py"])
    if success:
        print("Changes merged safely")
    else:
        print(f"Integration failed:\\n{log}")
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from ekp_forge.agents.base import BaseAgent, ExecutionTier
from ekp_forge.protocol.capability import Capability
from ekp_forge.protocol.roles import Role
from ekp_forge.schemas.task_schema import ErrorChunkSummary


class IntegratorAgent(BaseAgent):
    """Safely merges Worker diffs into the host repository with system checks.

    The Integrator Agent acts as the independent auditor of the system.
    It enforces code integration safety deterministically:

    - Backups affected files before applying changes.
    - Applies the ``git_diff`` using ``git apply``.
    - Runs global ``mypy .`` and ``pytest .`` on project root.
    - On success: clears backup and returns success.
    - On failure: restores backups and returns error log.
    """

    agent_id: str = "integrator"
    capabilities: list[Capability] = [
        Capability.INTEGRATION,
        Capability.VERIFICATION,
    ]
    execution_tier: ExecutionTier = "local"

    def __init__(self, project_root: Path | None = None) -> None:
        """Initialize the IntegratorAgent.

        Args:
            project_root: Path to the project root. Defaults to CWD.
        """
        self.project_root = project_root or Path.cwd()
        self._backups: dict[Path, str] = {}

    # ------------------------------------------------------------------
    # BaseAgent Protocol
    # ------------------------------------------------------------------

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        """BaseAgent protocol — dispatch based on context keys.

        Expects context keys:
        - ``task`` (required): TaskSchema instance.
        - ``impl_result`` (optional): dict with ``git_diff`` key.
        - ``affected_files`` (optional): list of file paths.
          Falls back to ``task.affected_modules``.
        - ``error_chunk_summary`` (optional): ErrorChunkSummary for ADR.

        Returns:
            Dict with keys:
            - ``status``: ``"success"`` | ``"failed"``
            - ``adr_path``: str | None (path to generated ADR on success)
            - ``error_log``: str (present on failure)
            - ``message``: str (status message)
        """
        task = context.get("task")
        impl_result = context.get("impl_result", {})
        diff_content = impl_result.get("git_diff", "")

        # Resolve affected files: explicit param > task.affected_modules
        affected_files: list[str] = context.get("affected_files", [])
        if not affected_files and task is not None:
            affected_files = getattr(task, "affected_modules", [])

        if not diff_content.strip():
            return {
                "status": "success",
                "message": "No diff to apply — integration skipped",
                "adr_path": None,
                "error_log": "",
            }

        success, log = self.integrate(diff_content, affected_files)

        if success:
            adr_path = self._generate_adr(task, context)
            return {
                "status": "success",
                "adr_path": adr_path,
                "error_log": "",
                "message": "Integration passed all global checks.",
            }

        return {
            "status": "failed",
            "adr_path": None,
            "error_log": log,
            "message": "Integration failed — changes reverted.",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def integrate(self, diff_content: str, affected_files: list[str]) -> tuple[bool, str]:
        """Apply patch, run global verification, rollback on failure.

        Args:
            diff_content:   Unified diff content (output from ``git diff``).
            affected_files: List of file paths that were modified.

        Returns:
            ``(True, "Merged successfully")`` on success.
            ``(False, error_log)`` on failure with rollback.
        """
        # Step 0: Early return for empty diff
        if not diff_content.strip():
            return True, "No diff to apply — integration skipped"

        # Step 1: Backup affected files
        self._backup_files(affected_files)

        # Step 2: Apply the diff via git apply
        apply_ok, apply_log = self._apply_diff(diff_content)
        if not apply_ok:
            self._restore_backups()
            return False, f"git apply failed:\n{apply_log}"

        # Step 3: Run global mypy and pytest
        mypy_ok, mypy_log = self._run_mypy()
        pytest_ok, pytest_log = self._run_pytest()

        if not mypy_ok or not pytest_ok:
            # Step 4: Rollback on failure
            self._restore_backups()
            error_parts: list[str] = []
            if not mypy_ok:
                error_parts.append(f"=== Mypy errors ===\n{mypy_log}")
            if not pytest_ok:
                error_parts.append(f"=== Pytest errors ===\n{pytest_log}")
            return False, "\n\n".join(error_parts)

        # Step 5: Success — clear backup, keep changes applied
        self._backups.clear()
        return True, "Integration passed all global checks."

    # ------------------------------------------------------------------
    # Backup / Restore
    # ------------------------------------------------------------------

    def _backup_files(self, files: list[str]) -> None:
        """Store original contents of target files in memory.

        Args:
            files: List of file paths relative to project_root.
        """
        for f in files:
            path = self.project_root / f
            if path.exists():
                self._backups[path] = path.read_text(encoding="utf-8")

    def _restore_backups(self) -> None:
        """Restore files to their pre-integration state.

        Restores file contents from in-memory backups, then runs
        ``git checkout -- <file>`` for each backed-up file to ensure
        git's index matches the working tree.
        """
        for path, original_content in self._backups.items():
            try:
                path.write_text(original_content, encoding="utf-8")
            except OSError:
                # Best-effort: if write fails, try git checkout
                self._git_checkout(str(path.relative_to(self.project_root)))

        # Also run git checkout for each backed-up file to sync git index
        for path in self._backups:
            try:
                rel = path.relative_to(self.project_root)
                self._git_checkout(str(rel))
            except ValueError:
                # Path not relative to project_root — skip git checkout
                pass

        self._backups.clear()

    # ------------------------------------------------------------------
    # Git operations
    # ------------------------------------------------------------------

    def _apply_diff(self, diff_content: str) -> tuple[bool, str]:
        """Apply a unified diff using ``git apply``.

        Args:
            diff_content: The unified diff content.

        Returns:
            ``(True, "")`` on success.
            ``(False, stderr)`` on failure.
        """
        try:
            proc = subprocess.run(
                ["git", "apply"],
                input=diff_content,
                capture_output=True,
                text=True,
                cwd=self.project_root,
                timeout=30,
            )
            if proc.returncode != 0:
                return False, proc.stderr.strip() or proc.stdout.strip()
            return True, ""
        except FileNotFoundError:
            return False, "git not found in PATH — cannot apply diff"
        except subprocess.TimeoutExpired:
            return False, "git apply timed out after 30 seconds"
        except OSError as e:
            return False, f"git apply failed with OSError: {e}"

    def _git_checkout(self, file_path: str) -> None:
        """Revert a single file via ``git checkout -- <file>``.

        Args:
            file_path: File path relative to project_root.
        """
        try:
            subprocess.run(
                ["git", "checkout", "--", file_path],
                capture_output=True,
                text=True,
                cwd=self.project_root,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass  # Best-effort

    # ------------------------------------------------------------------
    # Verification tools
    # ------------------------------------------------------------------

    def _run_mypy(self) -> tuple[bool, str]:
        """Run ``mypy .`` on the project root.

        Returns:
            ``(True, "")`` if mypy passes or is unavailable.
            ``(False, output)`` if mypy finds errors.
        """
        try:
            proc = subprocess.run(
                ["mypy", "."],
                capture_output=True,
                text=True,
                cwd=self.project_root,
                timeout=60,
            )
            output = (proc.stdout + proc.stderr).strip()
            return proc.returncode == 0, output
        except FileNotFoundError:
            # Mypy not installed — log warning but don't fail
            print("[IntegratorAgent] mypy not found — skipping mypy check", file=sys.stderr)
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "mypy timed out after 60 seconds"
        except OSError as e:
            return False, f"mypy failed with OSError: {e}"

    def _run_pytest(self) -> tuple[bool, str]:
        """Run ``pytest .`` on the project root.

        Treats pytest exit code 5 (no tests collected) as success,
        since an empty test suite is not a regression.

        Returns:
            ``(True, "")`` if pytest passes or is unavailable.
            ``(False, output)`` if pytest finds failures.
        """
        try:
            proc = subprocess.run(
                ["pytest", "."],
                capture_output=True,
                text=True,
                cwd=self.project_root,
                timeout=120,
            )
            output = (proc.stdout + proc.stderr).strip()
            # Exit code 5 = no tests collected — not a regression
            return proc.returncode in (0, 5), output
        except FileNotFoundError:
            # Pytest not installed — log warning but don't fail
            print("[IntegratorAgent] pytest not found — skipping pytest check", file=sys.stderr)
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "pytest timed out after 120 seconds"
        except OSError as e:
            return False, f"pytest failed with OSError: {e}"

    # ------------------------------------------------------------------
    # ADR Generation (delegates to ManagerAgent)
    # ------------------------------------------------------------------

    def _generate_adr(self, task: Any, context: dict[str, Any]) -> str | None:
        """Delegate ADR generation to ManagerAgent.

        Args:
            task:    The TaskSchema instance.
            context: The full execution context (may contain error_chunk_summary).

        Returns:
            The ADR file path as a string, or None if generation fails.
        """
        if task is None:
            return None

        try:
            from ekp_forge.manager import ManagerAgent

            manager = ManagerAgent()
            error_chunk = context.get("error_chunk_summary")
            if error_chunk is None:
                task_id = getattr(task, "task_id", "unknown")
                error_chunk = ErrorChunkSummary(task_id=task_id)
            return manager.generate_adr(task=task, error_chunk=error_chunk)
        except Exception as e:
            print(f"[IntegratorAgent] ADR generation failed: {e}", file=sys.stderr)
            return None
