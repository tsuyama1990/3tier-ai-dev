"""Adversarial Tester — Phase B: Auto-generates edge case tests via Ollama and executes them."""

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

from schemas.task_schema import TaskSchema, _estimate_confidence


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

        payload = json.dumps({
            "model": model_name,
            "messages": [
                {"role": "system", "content": "You are a senior test automation engineer. Output only pytest code inside python block."},
                {"role": "user", "content": prompt}
            ],
            "stream": False,
        }).encode("utf-8")

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
        from schemas.task_schema import ErrorChunkSummary
        summary_dict = worker_result.get("error_chunk_summary", {})
        if isinstance(summary_dict, dict):
            entries_list = summary_dict.get("entries", [])
            from schemas.task_schema import ErrorChunkEntry
            entries = [ErrorChunkEntry(**e) if isinstance(e, dict) else e for e in entries_list]
            error_chunk = ErrorChunkSummary(
                task_id=task.task_id,
                entries=entries,
                total_retries=summary_dict.get("total_retries", len(entries))
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
