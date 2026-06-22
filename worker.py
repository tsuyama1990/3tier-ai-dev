"""Worker Agent — Tier 3: executes tasks via Aider + verification loop with escalation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

from schemas.task_schema import (
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


class WorkerAgent:
    """Executes implementation plans through Aider + verification loop."""

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

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def execute_verification_loop(
        self,
        task: Any,  # TaskSchema (avoid import-time circular issues with typing)
        plan: str,
        rag_context: str = "",
        run_adversarial: bool = True,
    ) -> dict[str, Any]:
        """
        Run Aider + pytest in a loop with escalation policy and optional adversarial auditing.

        Returns:
            {
                "status": "success" | "failed" | "escalated",
                "retries": int,
                "error_chunk_summary": ErrorChunkSummary,
                "help_request": HelpRequestSchema | None,
                "git_diff": str,   # present on "success"
                "patch_report": dict | None,
                "adversarial_passed": bool | None,
            }
        """
        from schemas.task_schema import TaskSchema  # local import

        assert isinstance(task, TaskSchema), "task must be a TaskSchema instance"

        error_chunk = ErrorChunkSummary(task_id=task.task_id)
        prev_error_hash: str | None = None
        aider_cmd = self._build_aider_command(task, plan, rag_context)

        for attempt in range(1, self.max_retries + 1):
            # --- Step 1: Aider execution ---
            aider_ok, aider_msg = self._run_aider(aider_cmd, attempt)
            if not aider_ok:
                error_chunk.add_entry(
                    ErrorChunkEntry(
                        attempt=attempt,
                        error_type="AiderExecutionError",
                        module=task.affected_modules[0],
                        action_taken=aider_msg,
                    )
                )
                # Aider failure → break out (non-recoverable tool error)
                break

            # --- Step 2: AST gatekeeper (validate imports) ---
            import_ok, import_err = self._validate_imports()
            if not import_ok:
                error_chunk.add_entry(
                    ErrorChunkEntry(
                        attempt=attempt,
                        error_type="ImportViolation",
                        module="*",
                        action_taken=import_err,
                    )
                )
                # Gatekeeper failure → try next iteration
                continue

            # --- Step 3: pytest execution ---
            pytest_ok, pytest_output = self._run_pytest()
            if pytest_ok:
                # Success!
                git_diff = self._get_git_diff()
                self._update_reflection_log(task, error_chunk, success=True)
                
                result = {
                    "status": "success",
                    "retries": attempt,
                    "error_chunk_summary": error_chunk,
                    "help_request": None,
                    "git_diff": git_diff,
                    "patch_report": None,
                    "adversarial_passed": None,
                }

                if run_adversarial:
                    try:
                        from adversarial_tester import AdversarialTester
                        tester = AdversarialTester()
                        test_file, _ = tester.generate_edge_case_tests(task, git_diff, model=self.model)
                        adv_ok, adv_output = tester.run_adversarial_tests(test_file)
                        patch_report = tester.generate_patch_report(task, result, (adv_ok, adv_output))
                        result["patch_report"] = patch_report
                        result["adversarial_passed"] = adv_ok
                    except Exception as e:
                        result["patch_report"] = {"error": f"Adversarial audit failed: {e!s}"}
                        result["adversarial_passed"] = False

                return result

            # Print failure output for diagnostic visibility
            print(f"\n--- ATTEMPT {attempt} PYTEST FAILURE ---\n{pytest_output}\n----------------------------------\n", file=sys.stderr)  # noqa: T201

            # --- Failure handling ---
            error_chunk.add_entry(
                ErrorChunkEntry(
                    attempt=attempt,
                    error_type=self._classify_error(pytest_output),
                    module=self._error_module(pytest_output, task.affected_modules),
                    action_taken="pytest failed, attempting repair",
                )
            )

            # Escalation Policy checks
            esc_result = self._check_escalation_policy(attempt, error_chunk, pytest_output, prev_error_hash, task)
            if esc_result is not None:
                # Escalation triggered
                git_diff = self._get_git_diff()
                # Rollback on escalation
                self._git_rollback()
                return {
                    "status": "escalated",
                    "retries": attempt,
                    "error_chunk_summary": error_chunk,
                    "help_request": esc_result,
                    "git_diff": git_diff,
                }

            prev_error_hash = _error_fingerprint(pytest_output)

        # --- Loop exhausted without success ---
        self._git_rollback()
        self._update_reflection_log(task, error_chunk, success=False)
        return {
            "status": "failed",
            "retries": self.max_retries,
            "error_chunk_summary": error_chunk,
            "help_request": None,
            "git_diff": "",
        }

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _build_aider_command(self, task: Any, plan: str, _rag_context: str) -> list[str]:
        """Build the Aider CLI command from the task and plan."""
        cmd = [
            "aider",
            "--yes",
            "--no-git",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        # Add knowledge files if they exist
        if os.path.exists(".ai-knowledge"):
            for f in sorted(os.listdir(".ai-knowledge")):
                fpath = os.path.join(".ai-knowledge", f)
                if os.path.isfile(fpath) and f.endswith(".md"):
                    cmd.extend(["--read", fpath])
        # Use message file for plan
        temp_msg = self._write_temp_message(plan)
        cmd.extend(["--message-file", temp_msg])
        cmd.extend(task.affected_modules)
        return cmd

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
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
            if res.returncode != 0:
                return False, f"Aider failed with code {res.returncode}: {res.stderr[:500]}"
            return True, res.stdout
        except subprocess.TimeoutExpired:
            return False, f"Aider timed out after {timeout}s"
        finally:
            self._cleanup_temp_message()

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
            if py_file.name in {
                "orchestrator.py",
                "orchestrator_api.py",
                "manager.py",
                "worker.py",
                "mcp_server.py",
                "test_orchestrator_api.py",
                "test_mcp_server.py",
                "rag_crawler.py",
                "adversarial_tester.py",
                "task_tree.py",
            }:
                continue
            if "dsc" in py_file.parts:
                continue
            if "schemas" in py_file.parts:
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
    def _classify_error(pytest_output: str) -> str:
        """Extract error type from pytest output."""
        import re

        for line in pytest_output.splitlines():
            # Match error type patterns: AssertionError, TypeError, ValueError, etc.
            m = re.search(r"(\w+(?:Error|Exception|Warning))", line)
            if m:
                return m.group(1)
        # Fallback markers
        for marker in ["FAILED", "assert", "SyntaxError", "TypeError", "ValueError"]:
            if marker in pytest_output:
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
