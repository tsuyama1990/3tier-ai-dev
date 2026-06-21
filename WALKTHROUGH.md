# Ollama 連携検証および機能実装のウォークスルー

本開発では、開発支援エージェント（Aider）および Asset Synthesizer のバックエンドとしてローカルの Ollama (`qwen2.5-coder:7b`) を統合・検証するための環境および各種テストを整備しました。

---

## 実施した変更点

### 1. 統合検証スクリプトの作成
- [validate_ollama.sh](file:///home/tomo/project/000_devenv/ekp-forge/validate_ollama.sh)
  - Ollama の疎通確認から、Step 1（疎通）、Step 2（知識優先）、Step 3（自己修復ストレス）、Step 4（Asset Synthesizer 呼び出し）の全4テストケースを順次実行し、サマリーを出力するマスタースクリプト。

### 2. 各検証ステップの実装

#### **Step 1: Baseline Communication**
- [test_ollama_baseline.py](file:///home/tomo/project/000_devenv/ekp-forge/tests/step1_baseline/test_ollama_baseline.py)
  - Ollama の `/api/chat` API に接続し、タイムアウト時間内に有効なレスポンスが取得できることを確認するベースラインテスト。

#### **Step 2: Fake API Reference**
- [.ai-knowledge/fake_lib.md](file:///home/tomo/project/000_devenv/ekp-forge/.ai-knowledge/fake_lib.md)
  - 一般的な知識とは異なる特殊な仕様（`FakeCalculator(use_magic_mode=True, offset=-99)` の強制）を記述した API リファレンス。
- [test_fake_api_ref.py](file:///home/tomo/project/000_devenv/ekp-forge/tests/step2_fake_api/test_fake_api_ref.py)
  - Aider を経由してコードを生成させる際、Ollama が `.ai-knowledge/fake_lib.md` を正しく読み込み、一般常識（通常の `Calculator` など）に逃げずに指定された特殊な引数を満たして実装できるか検証するテスト。

#### **Step 3: Self-Healing Loop Stress**
- [test_self_healing_stress.py](file:///home/tomo/project/000_devenv/ekp-forge/tests/step3_stress/test_self_healing_stress.py)
  - AST ゲートキーパー（`validate_imports`）が禁止インポート（`os`）を検出し、自己修復ループを始動できるかを検証する。
  - ループが上限（3回）に達した際に `git reset --hard` / `git clean` が正しく作動し、失敗状態で終了することを確認。

#### **Step 4: Asset Synthesizer Ollama Integration**
- [test_ollama_synthesizer.py](file:///home/tomo/project/000_devenv/ekp-forge/tests/step4_ollama_synthesizer/test_ollama_synthesizer.py)
  - `asset_synthesizer.py` に追加した `--llm-provider ollama` 経由のグラフ合成処理を検証するテスト。
  - 疎通不全時のテンプレートモードへの自動フォールバック動作を含めた耐久性を保証。

### 3. コアコードへの機能拡張

- [orchestrator_api.py](file:///home/tomo/project/000_devenv/ekp-forge/orchestrator_api.py)
  - 呼び出しインターフェースを拡張し、`model` 引数および `skip_self_healing` 引数を追加。
  - Aider 起動時に `--yes` や `--no-git` フラグを適切に設定し、非対話型での実行を安定化。
  - パス補正時に pathlib モックと干渉しないよう、ロックファイル検査を `os.path.exists` に変更。
- [asset_synthesizer.py](file:///home/tomo/project/000_devenv/ekp-forge/dsc/asset_synthesizer.py)
  - ローカル Ollama API 向けの接続メソッド `_call_ollama` を標準ライブラリ（`urllib.request`）のみで実装。
  - CLI パーサーに `--llm-provider` と `--ollama-url` を追加。

---

## 検証方法と結果

マスター検証スクリプトを実行し、全テストステージがクリアされることを確認します。

```bash
bash validate_ollama.sh
```

### 期待される出力例 (SUMMARY)
```text
=== [SUMMARY] ===
Step 1: PASS ✅
Step 2: PASS ✅
Step 3: PASS ✅
Step 4: PASS ✅
```
