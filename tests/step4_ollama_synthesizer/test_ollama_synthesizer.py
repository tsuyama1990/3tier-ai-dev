"""
Step 4: Asset Synthesizer Ollama Native Test

Purpose: Verify _call_ollama() is implemented and synthesize() works
with llm_provider="ollama", generating semantically rich integration_graph.md.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import requests  # Ollama 疎通確認用

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dsc.asset_synthesizer import (
    _call_ollama,
    synthesize,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5-coder:7b"


def test_ollama_service_available():
    """Step 4-A: Ollama サービスの疎通確認"""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        assert resp.status_code == 200
    except requests.exceptions.ConnectionError:
        pytest.fail("Ollama is not running. Start with 'ollama serve'.")


def test_call_ollama_returns_string():
    """
    Step 4-B: _call_ollama() が文字列を返すこと。

    最小限のプロンプトで Ollama を呼び出し、非空文字列が返ることを検証。
    """
    result = _call_ollama(
        prompt="Reply with exactly one word: hello",
        model=OLLAMA_MODEL,
        max_tokens=50,
        timeout=120,
        ollama_base_url=OLLAMA_URL,
    )
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert len(result.strip()) > 0, "Response must not be empty"
    print(f"\n[Step 4-B] Ollama response: {result[:100]}")


def test_synthesize_with_ollama_provider():
    """
    Step 4-C: synthesize() が llm_provider="ollama" で動作し、
    意味論的な integration_graph.md を生成できること。

    PASS条件:
    - integration_graph.md が生成されること
    - ファイルサイズが 100 bytes 以上（実質的な内容がある）
    - "ase" または "Integration Graph" が含まれること
    """
    # フィクスチャを一時ディレクトリにコピー
    cache_dir = Path(tempfile.mkdtemp(prefix="step4_test_"))
    try:
        shutil.copytree(FIXTURES_DIR, cache_dir, dirs_exist_ok=True)

        result = synthesize(
            cache_dir=cache_dir,
            target_pkg="ase",
            version="3.28.0",
            dry_run=False,
            use_llm=True,
            llm_model=OLLAMA_MODEL,
            llm_provider="ollama",
            ollama_base_url=OLLAMA_URL,
        )

        # ファイル生成確認
        assert result["integration_graph_written"], "integration_graph.md must be written"

        ig_path = cache_dir / "integration_graph.md"
        assert ig_path.exists(), "integration_graph.md file must exist"

        content = ig_path.read_text()
        assert len(content) > 100, f"integration_graph.md must have substantial content, got {len(content)} bytes"

        # Markdown の基本構造確認
        assert any(kw in content for kw in ["ase", "Integration Graph", "API", "#"]), (
            "integration_graph.md must contain meaningful content"
        )

        print(f"\n[Step 4-C] Generated {len(content)} bytes")
        print(f"[Step 4-C] First 300 chars:\n{content[:300]}")

    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_ollama_fallback_on_error():
    """
    Step 4-D: _call_ollama() が接続失敗時に RuntimeError を送出し、
    synthesize() がテンプレートモードにフォールバックすること。
    """
    cache_dir = Path(tempfile.mkdtemp(prefix="step4_fallback_"))
    try:
        shutil.copytree(FIXTURES_DIR, cache_dir, dirs_exist_ok=True)

        # 存在しないポートを指定して接続失敗をシミュレート
        result = synthesize(
            cache_dir=cache_dir,
            target_pkg="ase",
            version="3.28.0",
            dry_run=False,
            use_llm=True,
            llm_model=OLLAMA_MODEL,
            llm_provider="ollama",
            ollama_base_url="http://localhost:19999",  # 存在しないポート
        )

        # フォールバックで template モードで生成されること
        assert result["integration_graph_written"], "Should fall back to template mode and write the file"

        ig_content = (cache_dir / "integration_graph.md").read_text()
        assert "Integration Graph" in ig_content, "Fallback template should contain 'Integration Graph'"

        print("\n[Step 4-D] Fallback to template mode: PASS")

    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_cli_ollama_provider():
    """
    Step 4-E: CLI から --llm --llm-provider ollama が使えること。
    build_parser() が --llm-provider と --ollama-url を受け付けること。
    """
    from dsc.asset_synthesizer import build_parser

    args = build_parser().parse_args(
        [
            "--cache-dir",
            "/tmp/test",
            "--target",
            "ase",
            "--version",
            "3.28.0",
            "--llm",
            "--llm-provider",
            "ollama",
            "--ollama-url",
            "http://localhost:11434",
        ]
    )

    assert args.llm_provider == "ollama"
    assert args.ollama_url == "http://localhost:11434"
    assert args.llm is True
    print("\n[Step 4-E] CLI parser accepts --llm-provider ollama: PASS")
