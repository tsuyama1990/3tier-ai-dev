import subprocess
import unittest
from unittest.mock import MagicMock, patch

# Import the tools to be tested
from ekp_forge.mcp_server import execute_simple_aider, execute_strict_compile


class TestMCPServer(unittest.TestCase):
    @patch("subprocess.run")
    def test_execute_simple_aider_without_model(self, mock_run):
        # Mock subprocess.run return value
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "Aider ran successfully"
        mock_res.stderr = ""
        mock_run.return_value = mock_res

        result = execute_simple_aider(prompt="implement binary search", target_files=["search.py"])

        # Assert subprocess.run was called correctly
        mock_run.assert_called_once_with(
            ["aider", "--message", "implement binary search", "--yes", "search.py"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

        # Assert returned dict structure
        self.assertEqual(result, {"success": True, "stdout": "Aider ran successfully", "stderr": ""})

    @patch("subprocess.run")
    def test_execute_simple_aider_with_model(self, mock_run):
        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_res.stdout = "Aider failed"
        mock_res.stderr = "Error trace"
        mock_run.return_value = mock_res

        result = execute_simple_aider(prompt="add logging", target_files=["app.py", "utils.py"], model="gpt-4")

        mock_run.assert_called_once_with(
            ["aider", "--message", "add logging", "--yes", "--model", "gpt-4", "app.py", "utils.py"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

        self.assertEqual(result, {"success": False, "stdout": "Aider failed", "stderr": "Error trace"})

    @patch("ekp_forge.mcp_server.run_managed_task")
    def test_execute_strict_compile(self, mock_run_managed):
        """execute_strict_compile now delegates to run_managed_task via WorkflowEngine."""
        mock_output = {
            "status": "success",
            "task_id": "T-20260627000000-abcdef",
            "adr_path": None,
            "rejection_reason": None,
            "help_request": None,
            "error_summary": None,
        }
        mock_run_managed.return_value = mock_output

        result = execute_strict_compile(
            prompt="compile project",
            target_pkg="my_pkg",
            target_files=["main.py"],
            model="ollama/qwen2.5-coder:7b",
        )

        # Verify run_managed_task was called with a TaskSchema dict
        mock_run_managed.assert_called_once()
        call_arg = mock_run_managed.call_args[0][0]
        self.assertIsInstance(call_arg, dict)
        self.assertEqual(call_arg.get("goal"), "compile project")
        self.assertEqual(call_arg.get("affected_modules"), ["main.py"])
        self.assertIn("task_id", call_arg)
        self.assertIn("manager_id", call_arg)

        self.assertEqual(result, mock_output)


if __name__ == "__main__":
    unittest.main()
