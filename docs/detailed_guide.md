# EKP/DSC 詳細ガイド＆運用マニュアル

本ドキュメントでは、Executable Knowledge Platform (EKP) / Dependency Semantic Compiler (DSC) の詳細な接続構成、環境設定、微調整、および運用手順について解説します。

---

## 1. MCP (Model Context Protocol) の設定方法

DSCのオーケストレータは、Aider やその他のエージェントから MCP を通じて呼び出すことが可能です。

### 1.1. 設定ファイル (`mcp_config.json`)
MCPサーバーを認識させるため、プロジェクトルートに `mcp_config.json` を配置します。

```json
{
  "mcpServers": {
    "aider-orchestrator": {
      "command": "/home/tomo/project/000_devenv/3tier_ai_devs/run-mcp.sh",
      "env": {
        "OLLAMA_HOST": "http://127.0.0.1:11434"
      }
    }
  }
}
```

### 1.2. ラッパースクリプト (`run-mcp.sh`)
本スクリプトは、MCP経由でのAiderオーケストレーション起動を安全に行うためのラッパーです。

```bash
#!/bin/bash
# run-mcp.sh
cd /home/tomo/project/000_devenv/3tier_ai_devs
source .venv/bin/activate
exec python3 orchestrator.py "$@"
```

### 1.3. Claude Desktop での接続設定
Claude Desktop アプリケーションから本オーケストレータを MCP ツールとして利用する場合、`~/.config/Claude/claude_desktop_config.json` に以下を登録します。

```json
{
  "mcpServers": {
    "ekp-dsc": {
      "command": "/bin/bash",
      "args": ["/home/tomo/project/000_devenv/3tier_ai_devs/run-mcp.sh"],
      "env": {
        "OPENROUTER_API_KEY": "YOUR_OPENROUTER_API_KEY"
      }
    }
  }
}
```

---

## 2. AIDER の設定方法

Aider が DSC が展開した知識資産（`.ai-knowledge/`）を正しく参照し、境界を超えないように制御するための設定です。

### 2.1. 基本構成 (`.aider.conf.yml`)
プロジェクトディレクトリごとに以下を設定します。

```yaml
model: openrouter/google/gemma-4-31b-it:free
editor-model: ollama/qwen2.5-coder:7b-instruct-q4_K_M
model-settings-file: .aider.model.settings.yml
map-tokens: 1024
timeout: 120
```

- **`model`**: 推論・設計モデル。通常、コンテキストウィンドウが広く優秀な OpenRouter 経由の LLM（`gemma-4-31b-it:free` や `deepseek-v4-flash`）を使用します。
- **`editor-model`**: コード編集を実行するモデル。ローカルの Ollama（`qwen2.5-coder` 等）を指定します。
- **`model-settings-file`**: 後述するモデル個別プロンプトを指定します。

### 2.2. モデルごとのコンテキストウィンドウ設定 (`~/.aider.model.metadata.json`)
OpenRouter 経由のカスタムモデルを使用する場合、コンテキスト制限を Aider に正確に伝えるため、以下のファイルを配置するか起動オプション `--model-metadata-file` で指定します。

```json
{
    "openrouter/deepseek/deepseek-v4-flash": {
        "max_tokens": 8192,
        "max_input_tokens": 163840,
        "max_output_tokens": 8192,
        "input_cost_per_token": 0.00000027,
        "output_cost_per_token": 0.0000011,
        "litellm_provider": "openrouter",
        "mode": "chat"
    },
    "openrouter/google/gemma-4-31b-it:free": {
        "max_tokens": 4096,
        "max_input_tokens": 131072,
        "max_output_tokens": 4096,
        "litellm_provider": "openrouter",
        "mode": "chat"
    }
}
```

### 2.3. ロール定義と行動境界プロンプト (`.aider.model.settings.yml`)
AIが `.ai-knowledge/` 内にない未検証のAPIを「勝手に捏造（ハルシネーション）」するのを防ぐため、厳格なエスカレーションルールを設定します。

```yaml
- name: openrouter/deepseek/deepseek-v4-flash
  edit_format: architect
  system_prompt_prefix: |
    You are an exact and reliable software architect.
    Your primary task is to design changes based on the user's request, utilizing the provided knowledge assets safely.

    [CRITICAL RULES]
    1. STRICT BOUNDARIES: You MUST use ONLY the documented APIs and integration patterns found in `.ai-knowledge/` and `verified_examples/`.
    2. NO INVENTION: Never invent interfaces, methods, or parameters that are not documented in the knowledge assets.
    3. ESCALATION: If the user request refers to or requires any class, function, method, parameter, or module that is not explicitly documented in the provided `.ai-knowledge/` files or `verified_examples/`, you MUST refuse to design or implement the change. Immediately output a response starting with: "ESCALATION: API <name> is undocumented." and stop. Do NOT guess, do NOT mock, and do NOT write stub classes, and do NOT proceed with the implementation.
```

---

## 3. うまく行かないときのパラメータ微調整方法

### 3.1. Smoke Tracer (`smoke_tracer.py`) のしきい値調整
スモークテストでの検証時、想定したファイルがキャッシュから除外されてしまう（または不要なファイルが残る）場合、以下の定数を調整します。

- **`_STATIC_THRESHOLD = 2.0` (L115付近):**
  - AST解析時の最小通過スコア。ダミーや単純すぎるスクリプトを弾きます。
  - スコアが低くテストが除外される場合、この値を下げるか、対象コードに加点要素（例: `if __name__ == "__main__":` の記述、ターゲットパッケージの複数インポート、20行以上のダミーコメント追加）を記述します。
- **`_FINAL_THRESHOLD = 0.9` (L116付近):**
  - 実行トレース結果と静的スコアを掛け合わせた最終評価基準。
  - テストが `CLEAN` (正常終了) であっても、静的スコアが低いファイル（例: 3行程度のスクリプト）は `static_norm = 0.3` 等となり、最終スコアが `0.3 * 1.0 = 0.3` に下がり除外されます。これを防ぐにはしきい値を下げるか、テストコード自体の加点要素を増やします。

### 3.2. LLMによるアセット合成のモデル変更
`asset_synthesizer.py` で意味論的グラフを生成する際、OpenRouter のモデルを切り替えるには、実行時に `--llm-model` 引数を指定します。

```bash
python3 dsc/asset_synthesizer.py --manifest manifest.json --llm --llm-model openrouter/deepseek/deepseek-chat
```

### 3.3. ASTインポートゲートキーパー (`api_schema.yaml`) のカスタマイズ
`orchestrator.py` は、インポートが `api_schema.yaml` にリストされているモジュールのみに制限します。
- 外部パッケージを追加したのにインポートエラーでブロックされる場合、`api_schema.yaml` の `allowed_imports` リストに手動で追記します。
- `dsc/deploy.py` でデプロイを実行すると、デプロイ対象パッケージが `api_schema.yaml` に自動追記されます（既存の手動追加部分やコメントは保護されます）。

---

## 4. ドキュメント収集（マイニング）方法

### 4.1. 自動マイニング手順
DSCは、ターゲットリポジトリの `tests/` や `examples/` から Python コードをマイニングします。

1. **VCS情報の自動検出:**
   `package_inspector.py` を走らせると、インポート元情報から `direct_url.json` (VCSインストール時) または PyPI の公式 URL からリポジトリ情報を自動抽出します。
2. **Git Sparse Checkout:**
   `source_miner.py` は Git 2.25 以上の機能である `sparse-checkout` を自動的に試し、`tests` や `examples` に類似したフォルダツリー構造のみをピンポイントでクローンします。これにより、大規模なリポジトリでも数秒でマイニングが完了します。
3. **ローカルリポジトリへの適用:**
   E2Eテストのように、ネットワークから取得できないクローズドな環境では、`direct_url.json` 内の URL をローカルディレクトリ（`file:///path/to/local/git/repo`）に向けることで、ローカルのGitリポジトリからテスト/サンプルをマイニングさせることが可能です。
