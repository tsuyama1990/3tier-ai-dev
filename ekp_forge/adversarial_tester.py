"""Adversarial Reviewer — independent gate that runs AFTER Worker verification and BEFORE Integrator.

This module provides two classes:

1. :class:`AdversarialTester` — original class (kept for backward compatibility).
2. :class:`AdversarialReviewer` — standalone gate with ``review()`` method.

The :class:`AdversarialReviewer` is called **between** Worker success and Integrator merge.
Its failures are **warnings, not blockers** — they inform robustness but don't prevent
integration. This is because adversarial tests find edge-case issues
(e.g., "crashes on 10GB CSV input"), not correctness issues.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ekp_forge.schemas.task_schema import TaskSchema, _estimate_confidence


class AdversarialTester:
    """Generates and executes edge case tests to audit patch quality."""

    def generate_edge_case_tests(
        self,
        task: TaskSchema,
        git_diff: str,
        model: str = "ollama/qwen2.5-coder:7b",
    ) -> tuple[str, str]:
        """
        Query Ollama to generate edge case tests for the task and diff.
        Returns (test_file_path, test_content).
        """
        model_name = model.replace("ollama/", "")

        prompt = f"""You are a QA engineer. Generate edge case tests using pytest for the following task and diff.
Goal: {task.goal}
Constraints: {task.constraints}
Affected Modules: {task.affected_modules}

Git Diff of implementation:
```diff
{git_diff}
```

Write a complete pytest test file. Include edge cases, boundary conditions, and invalid inputs.
Return ONLY the raw python test code inside a single ```python ... ``` code block.
Do NOT write any explanation outside the code block.
"""

        payload = json.dumps(
            {
                "model": model_name,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a senior test automation engineer. Output only pytest code inside python block.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            }
        ).encode("utf-8")

        url = "http://localhost:11434/api/chat"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                response_content = data["message"]["content"]
        except Exception:
            response_content = ""

        # Extract code block
        code_match = re.search(r"```python\s*(.*?)\s*```", response_content, re.DOTALL)
        if code_match:
            extracted_code = code_match.group(1)
        else:
            code_match2 = re.search(r"```\s*(.*?)\s*```", response_content, re.DOTALL)
            extracted_code = code_match2.group(1) if code_match2 else response_content

        # Save to tests/test_adversarial_generated.py
        test_file_path = "tests/test_adversarial_generated.py"
        Path(test_file_path).write_text(extracted_code, encoding="utf-8")

        return test_file_path, extracted_code

    def run_adversarial_tests(self, test_file_path: str) -> tuple[bool, str]:
        """Run pytest on the generated test file. Returns (success, output)."""
        try:
            res = subprocess.run(
                [sys.executable, "-m", "pytest", "-v", "--tb=short", test_file_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return res.returncode == 0, res.stdout + res.stderr
        except Exception as e:
            return False, str(e)

    def generate_patch_report(
        self,
        task: TaskSchema,
        worker_result: dict[str, Any],
        adversarial_result: tuple[bool, str],
    ) -> dict[str, Any]:
        """Generate the Patch Quality Report."""
        retries = worker_result.get("retries", 1)

        # Parse error_chunk_summary
        from ekp_forge.schemas.task_schema import ErrorChunkSummary

        summary_dict = worker_result.get("error_chunk_summary", {})
        if isinstance(summary_dict, dict):
            entries_list = summary_dict.get("entries", [])
            from ekp_forge.schemas.task_schema import ErrorChunkEntry

            entries = [ErrorChunkEntry(**e) if isinstance(e, dict) else e for e in entries_list]
            error_chunk = ErrorChunkSummary(
                task_id=task.task_id, entries=entries, total_retries=summary_dict.get("total_retries", len(entries))
            )
        else:
            error_chunk = summary_dict

        confidence = _estimate_confidence(retries, error_chunk)
        adv_passed = adversarial_result[0]

        # Quality metrics
        if retries <= 1 and adv_passed:
            quality = "high"
        elif retries >= 3 or not adv_passed:
            quality = "low"
        else:
            quality = "medium"

        # Summarize error type distribution
        error_types: dict[str, int] = {}
        for entry in error_chunk.entries:
            error_types[entry.error_type] = error_types.get(entry.error_type, 0) + 1

        return {
            "task_id": task.task_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "verification_retries": retries,
            "adversarial_tests_generated": 1,
            "adversarial_tests_passed": adv_passed,
            "error_type_distribution": error_types,
            "confidence_final": confidence,
            "patch_quality": quality,
        }


# ---------------------------------------------------------------------------
# Adversarial Reviewer — Independent Gate (v4.1)
# ---------------------------------------------------------------------------


class AdversarialReviewer:
    """Independent adversarial testing gate — runs AFTER Worker verification passes.

    Unlike the old :class:`AdversarialTester` which was embedded inside the Worker's
    verification loop, this class is called as a **separate phase** between Worker
    success and Integrator merge.

    Key design decisions
    --------------------
    * Adversarial failures are **warnings, not blockers** — they inform robustness
      but don't prevent integration.
    * The review runs in the sandbox directory to avoid polluting the main project.
    * No self-healing loop: failures are reported, not fixed.
    """

    def __init__(self, model: str = "ollama/qwen2.5-coder:7b"):
        self.model = model
        self._tester = AdversarialTester()

    def review(
        self,
        task: TaskSchema,
        git_diff: str,
        sandbox_path: Path | None = None,
    ) -> tuple[bool, str, dict]:
        """Run adversarial edge‑case testing against a verified diff.

        Parameters
        ----------
        task:
            The task schema for which the diff was generated.
        git_diff:
            The unified diff of the verified changes.
        sandbox_path:
            Optional sandbox path to run adversarial tests in. If ``None``,
            tests run in the current working directory.

        Returns
        -------
        tuple[bool, str, dict]
            ``(passed, output_summary, report_dict)`` where:
            * ``passed`` — ``True`` if all adversarial tests passed (or generation failed)
            * ``output_summary`` — human-readable summary of findings
            * ``report_dict`` — structured report for logging
        """
        original_cwd: str | None = None
        test_file: str | None = None

        try:
            # Change to sandbox if provided
            if sandbox_path:
                import os

                original_cwd = os.getcwd()
                os.chdir(sandbox_path)

            # Step 1: Generate edge-case tests
            test_file, test_content = self._tester.generate_edge_case_tests(task, git_diff, model=self.model)
            if not test_content.strip():
                return True, "Adversarial review skipped (no tests generated)", {}

            # Step 2: Run the tests
            adv_ok, adv_output = self._tester.run_adversarial_tests(test_file)

            # Step 3: Build report
            report = {
                "task_id": task.task_id,
                "adversarial_passed": adv_ok,
                "adversarial_output": adv_output[:500] if adv_output else "",
                "warning": not adv_ok,
            }

            if adv_ok:
                return True, "Adversarial review passed — all edge-case tests OK.", report
            return False, f"Adversarial review found edge-case issues:\n{adv_output[:1000]}", report

        except Exception as e:
            return True, f"Adversarial review encountered error (skipped): {e}", {"error": str(e)}

        finally:
            # Cleanup test file
            if test_file:
                try:
                    Path(test_file).unlink(missing_ok=True)
                except Exception:
                    pass
            # Restore original directory
            if original_cwd and sandbox_path:
                import os

                os.chdir(original_cwd)
