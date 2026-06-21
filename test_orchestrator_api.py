import os
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# これから作らせるターゲット関数
from orchestrator_api import run_3tier_dev

class TestOrchestratorAPI(unittest.TestCase):
    def setUp(self):
        self.lock_file = Path(".ekp.lock")
        if self.lock_file.exists():
            self.lock_file.unlink()

    def tearDown(self):
        if self.lock_file.exists():
            self.lock_file.unlink()

    @patch("subprocess.run")
    def test_successful_run_returns_structured_json(self, mock_run):
        """成功時、厳格なJSONスキーマ（辞書）で結果が返ること"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "All tests passed"
        mock_run.return_value = mock_result

        result = run_3tier_dev(
            prompt="Implement dummy feature",
            target_pkg="ase",
            target_files=["src/dummy.py"]
        )

        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("success"))
        self.assertIn("files_changed", result)
        self.assertIn("src/dummy.py", result.get("files_changed", []))

    @patch("subprocess.run")
    def test_timeout_handling(self, mock_run):
        """タイムアウト時、プロセスがハングせず status: timeout が返ること"""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="aider", timeout=600)

        result = run_3tier_dev(
            prompt="Infinite loop prompt",
            target_pkg="ase",
            target_files=["src/dummy.py"],
            timeout=600
        )

        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("status"), "timeout")

    def test_lock_mechanism_prevents_concurrent_runs(self):
        """別プロセス実行中（.ekp.lock存在時）は弾かれ、status: locked が返ること"""
        self.lock_file.write_text("locked by test")

        result = run_3tier_dev(
            prompt="Concurrent prompt",
            target_pkg="ase",
            target_files=["src/dummy.py"]
        )

        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("status"), "locked")

    @patch("sys.stdin.read")
    def test_no_interactive_stdin(self, mock_stdin_read):
        """標準入力に一切依存しないこと（対話モードの排除）"""
        mock_stdin_read.side_effect = RuntimeError("sys.stdin.read() MUST NOT BE CALLED in API mode")
        
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_run.return_value = mock_result
            
            run_3tier_dev("prompt", "ase", ["dummy.py"])

@patch("subprocess.run")
    @patch("os.path.exists")
    def test_fails_fast_if_knowledge_missing(self, mock_exists, mock_run):
        """知識(Knowledge)が存在しない場合、Aiderを起動せずに即時エラーを返すこと"""
        # .ekp.lock は存在しない、.ai-knowledge/{pkg}.md も存在しない設定
        mock_exists.side_effect = lambda path: False
        
        result = run_3tier_dev("prompt", "unknown_pkg", ["dummy.py"])
        
        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("status"), "knowledge_missing")
        mock_run.assert_not_called()

    @patch("subprocess.run")
    @patch("os.path.exists")
    def test_git_rollback_on_failure(self, mock_exists, mock_run):
        """修復ループが上限に達して失敗した場合、git reset --hard が呼ばれること"""
        # 知識は存在する設定
        mock_exists.side_effect = lambda p: True if "unknown_pkg" not in str(p) else False
        
        # Aiderが常に失敗する(returncode=1)ようにモック
        mock_result_fail = MagicMock()
        mock_result_fail.returncode = 1
        mock_run.return_value = mock_result_fail

        result = run_3tier_dev("prompt", "ase", ["dummy.py"])
        
        self.assertFalse(result.get("success"))
        
        # subprocess.run の呼び出し履歴を取得
        calls = mock_run.call_args_list
        # git reset --hard HEAD と git clean -fd が呼ばれているか確認
        git_reset_called = any("reset" in call[0][0] and "--hard" in call[0][0] for call in calls)
        self.assertTrue(git_reset_called, "git reset --hard MUST be called on failure")

if __name__ == "__main__":
    unittest.main()
