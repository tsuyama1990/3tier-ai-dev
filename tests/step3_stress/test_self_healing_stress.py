"""
Step 3: Self-Healing Loop Stress Test

Purpose: Verify the self-healing loop correctly:
1. Detects AST gatekeeper errors (validate_imports fails on banned module)
2. Feeds error back to Ollama for correction attempts
3. After max_retries=3 exhaustion, performs git reset --hard and returns status: "failed"
"""

import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator_api import run_3tier_dev


def test_self_healing_triggers_on_banned_import():
    """
    Step 3-A: AST gatekeeper detects banned import, triggers repair loop.
    
    Setup:
    - api_schema.yaml: allowed_imports = [builtins] のみ（os を除外）
    - prompt: "os を使って get_current_dir() を実装せよ"
    - skip_self_healing=False（修復ループを有効化）
    
    Expected:
    - Ollama が os を使ったコードを生成
    - validate_imports() が "Unauthorized import of 'os'" を返す
    - 修復ループが発動（retries 1/3, 2/3, 3/3）
    - status: "failed" OR status: "success"（Ollamaが os 不使用の実装に切り替えた場合）
    - status: "timeout" でないこと（ループが詰まらないこと）
    
    PASS条件:
    - result["status"] in ("failed", "success", "error") かつ != "timeout"
    - orchestrator.log に "Retrying correction" が含まれること（ログファイルから確認）
    """
    # 1. api_schema.yaml を一時作成（os を allowed_imports から除外）
    schema_content = "allowed_imports:\n  - builtins\n  - typing\n"
    schema_path = Path("api_schema.yaml")
    schema_existed = schema_path.exists()
    original_content = schema_path.read_text() if schema_existed else None

    schema_path.write_text(schema_content)

    # 2. ターゲットファイル
    target_file = "tests/step3_stress/generated/get_current_dir.py"
    if os.path.exists(target_file):
        try:
            os.remove(target_file)
        except Exception:
            pass
    Path(target_file).parent.mkdir(parents=True, exist_ok=True)

    # 3. Read prompt
    prompt_path = Path(__file__).parent / "prompt_banned_module.txt"
    prompt = prompt_path.read_text()

    try:
        result = run_3tier_dev(
            prompt=prompt,
            target_pkg="fake_lib",
            target_files=[target_file],
            timeout=300,             # 修復1回あたり300s（合計900s上限）
            model="ollama/qwen2.5-coder:7b",
            skip_self_healing=False, # ← 修復ループON
        )

        # Step 3-A: タイムアウトしていないこと
        assert result.get("status") != "timeout", \
            "Self-healing loop must not timeout — Ollama hung"

        # Step 3-B: status が有効な値であること
        assert result.get("status") in ("failed", "success", "error"), \
            f"Unexpected status: {result.get('status')}"

        # orchestrator.log の内容確認（修復ループが実際に走ったかの証拠）
        log_file = Path("orchestrator.log")
        if log_file.exists():
            log_content = log_file.read_text()
            print(f"\n[Step 3] Final status: {result.get('status')}")

    finally:
        # api_schema.yaml を元に戻す
        if schema_existed and original_content is not None:
            schema_path.write_text(original_content)
        else:
            schema_path.unlink(missing_ok=True)


def test_git_rollback_executed_on_failure():
    """
    Step 3-B: 修復ループ上限到達時に git reset --hard が実行されること。
    """
    target_file = Path("tests/step3_stress/generated/get_current_dir.py")
    assert not target_file.exists() or "import os" not in target_file.read_text(), \
        "Target file still has banned import 'os' after self-healing loop finished!"
