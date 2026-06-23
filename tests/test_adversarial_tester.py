"""Tests for adversarial_tester.py — AdversarialTester class implementation verification."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from ekp_forge.adversarial_tester import AdversarialTester
from ekp_forge.schemas.task_schema import TaskSchema


@pytest.fixture
def task_schema() -> TaskSchema:
    return TaskSchema(
        task_id="T-20260623000000-abcdef",
        manager_id="MGR-Bootstrap-01",
        goal="Implement user authentication middleware",
        constraints=["No external libraries outside api_schema.yaml"],
        acceptance_tests=["Auth token is validated"],
        affected_modules=["src/middleware/auth.py"],
        assumptions_required={"api_schema_version": "v1.0"},
    )


@pytest.fixture
def tester() -> AdversarialTester:
    return AdversarialTester()


class TestGenerateEdgeCaseTests:
    @patch("urllib.request.urlopen")
    def test_generate_edge_case_tests_returns_file(
        self, mock_urlopen, tester: AdversarialTester, task_schema: TaskSchema
    ) -> None:
        """Ollama API からエッジケーステストを生成し、適切なファイルを返すこと"""
        # Mock Ollama API response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "message": {
                "role": "assistant",
                "content": "```python\ndef test_edge_case():\n    assert True\n```"
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with patch("pathlib.Path.write_text") as mock_write_text:
            file_path, content = tester.generate_edge_case_tests(
                task=task_schema,
                git_diff="diff --git a/src/middleware/auth.py",
                model="ollama/qwen2.5-coder:7b"
            )
            
            assert "test_adversarial_generated.py" in file_path
            assert "test_edge_case" in content
            mock_write_text.assert_called_once_with(content, encoding="utf-8")


class TestRunAdversarialTests:
    @patch("subprocess.run")
    def test_run_adversarial_tests_success(self, mock_run, tester: AdversarialTester) -> None:
        """テストが成功した時に success=True を返すこと"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "All tests passed"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        success, output = tester.run_adversarial_tests("tests/test_adversarial_generated.py")
        assert success is True
        assert "All tests passed" in output

    @patch("subprocess.run")
    def test_run_adversarial_tests_failure(self, mock_run, tester: AdversarialTester) -> None:
        """テストが失敗した時に success=False を返すこと"""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "Tests failed"
        mock_result.stderr = ""
        mock_run.value = mock_result
        mock_run.return_value = mock_result

        success, output = tester.run_adversarial_tests("tests/test_adversarial_generated.py")
        assert success is False
        assert "Tests failed" in output


class TestGeneratePatchReport:
    def test_patch_report_structure(self, tester: AdversarialTester, task_schema: TaskSchema) -> None:
        """Patch Report の必須キーが揃っていること"""
        worker_result = {
            "retries": 1,
            "error_chunk_summary": {
                "entries": [
                    {"attempt": 1, "error_type": "AssertionError", "module": "auth.py", "action_taken": "fix"}
                ]
            }
        }
        report = tester.generate_patch_report(
            task=task_schema,
            worker_result=worker_result,
            adversarial_result=(True, "Passed")
        )
        
        assert report["task_id"] == task_schema.task_id
        assert "timestamp" in report
        assert report["verification_retries"] == 1
        assert report["adversarial_tests_passed"] is True
        assert "patch_quality" in report
        assert "error_type_distribution" in report

    def test_patch_quality_high_on_zero_retries(self, tester: AdversarialTester, task_schema: TaskSchema) -> None:
        """リトライが0または1で、アドバーサリアルテストもパスした場合は quality=high であること"""
        worker_result = {"retries": 1, "error_chunk_summary": {"entries": []}}
        report = tester.generate_patch_report(
            task=task_schema,
            worker_result=worker_result,
            adversarial_result=(True, "Passed")
        )
        assert report["patch_quality"] == "high"

    def test_patch_quality_low_on_max_retries(self, tester: AdversarialTester, task_schema: TaskSchema) -> None:
        """リトライが最大（3）またはアドバーサリアルテストが失敗した場合は quality=low であること"""
        worker_result = {"retries": 3, "error_chunk_summary": {"entries": []}}
        report = tester.generate_patch_report(
            task=task_schema,
            worker_result=worker_result,
            adversarial_result=(False, "Failed")
        )
        assert report["patch_quality"] == "low"
