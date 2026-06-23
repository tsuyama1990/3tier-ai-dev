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

    @patch("ekp_forge.mcp_server.run_3tier_dev")
    def test_execute_strict_compile(self, mock_run_3tier):
        mock_output = {
            "success": True,
            "files_changed": ["main.py"],
            "status": "success",
            "stdout": "Compiled successfully",
            "stderr": "",
        }
        mock_run_3tier.return_value = mock_output

        result = execute_strict_compile(
            prompt="compile project", target_pkg="my_pkg", target_files=["main.py"], model="ollama/qwen2.5-coder:7b"
        )

        mock_run_3tier.assert_called_once_with(
            prompt="compile project",
            target_pkg="my_pkg",
            target_files=["main.py"],
            model="ollama/qwen2.5-coder:7b",
            timeout=600,
        )

        self.assertEqual(result, mock_output)


if __name__ == "__main__":
    unittest.main()
