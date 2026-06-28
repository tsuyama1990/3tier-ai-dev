# EKP/DSC 詳細ガイド＆運用マニュアル

本ドキュメントでは、Executable Knowledge Platform (EKP) / Dependency Semantic Compiler (DSC) の詳細な接続構成、環境設定、微調整、および運用手順について解説します。

より包括的なプロジェクト概要については [`README.md`](../README.md) を参照してください。

---

## 1. MCP (Model Context Protocol) の設定方法

EKP-Forge は **2つのMCPサーバー** を提供しています。どちらも stdio トランスポート（VSCodeが自動起動）です。

| MCPサーバー | スクリプト | モデル | 用途 |
|------------|-----------|--------|------|
| `aider-orchestrator` | [`run-mcp.sh`](../run-mcp.sh) | Ollama 7B | Aider直接実行 |
| `ekp-forge-manager` | [`run-mcp-ekp.sh`](../run-mcp-ekp.sh) | DeepSeek V4 Flash(Mgr) + Ollama 7B(Worker) | 3-Tier管理パイプライン |

### 1.1. 設定ファイル (`mcp_config.json`)
MCPサーバーを認識させるため、プロジェクトルートに [`mcp_config.json`](../mcp_config.json) を配置します。

```json
{
  "mcpServers": {
    "aider-orchestrator": {
      "command": "/home/tomo/project/000_devenv/ekp-forge/run-mcp.sh",
      "args": [],
      "env": {
        "OLLAMA_HOST": "http://127.0.0.1:11434"
      }
    },
    "ekp-forge-manager": {
      "command": "/home/tomo/project/000_devenv/ekp-forge/run-mcp-ekp.sh",
      "args": [],
      "env": {
        "OLLAMA_HOST": "http://127.0.0.1:11434"
      }
    }
  }
}
```

### 1.2. ラッパースクリプト

#### `run-mcp.sh`（aider-orchestrator用）
`aider-mcp`（Aider MCPブリッジ）を使用。`OPENROUTER_API_KEY` を `~/.zshrc` から動的に解決。

#### `run-mcp-ekp.sh`（ekp-forge-manager用）
[`ekp_forge/mcp_server.py`](../ekp_forge/mcp_server.py) を起動。`DEEPSEEK_API_KEY` と `OPENROUTER_API_KEY` を `~/.zshrc` から解決。
FastMCP（Python MCP SDK v1.28.0+）を使用した stdio サーバー。

#### Ollama自動起動
両スクリプトとも、起動時にOllamaの稼働状態をチェックし、停止中であれば自動起動します：
```bash
if ! curl -s http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
    ollama serve &
    disown
    # 最大10秒待機
fi
```

### 1.3. Claude Desktop での接続設定
Claude Desktop アプリケーションから本オーケストレータを MCP ツールとして利用する場合、`~/.config/Claude/claude_desktop_config.json` に以下を登録します。

```json
{
  "mcpServers": {
    "ekp-forge": {
      "command": "/bin/bash",
      "args": ["/home/tomo/project/000_devenv/ekp-forge/run-mcp.sh"],
      "env": {
        "OPENROUTER_API_KEY": "YOUR_OPENROUTER_API_KEY"
      }
    }
  }
}
```

### 1.4. 公開されているMCPツール

| ツール | 説明 |
|--------|------|
| `execute_simple_aider` | 静的解析や自己修復なしでAiderを実行 |
| `execute_strict_compile` | `run_3tier_dev` パイプライン全体を実行: サンドボックス → Aider → 検証 → 統合 |
| `run_managed_task` | 完全な管理パイプライン: トリアージ → アーキテクト → ワーカー → 検証 → 統合 |
| `run_epic_task` | エピックタスクをサブタスクに分解し、TaskTreeで並列実行 |
| `generate_task_id` | 目標文字列から決定論的なタスクIDを生成 |

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

---

## 5. 既知の問題：2-Tierモデルのプロトコル遵守問題

### 問題
2-Tier構成（Director/Manager=DeepSeek, Worker=Ollama 7B, MCPサーバー無し）では、トップ層のモデル（DeepSeek）がWorkerへの委譲プロトコルを守らず、自分でコードを書いてしまう問題が確認されています。

### 原因
1. **強制メカニズムの不在**: 3-Tier（MCPあり）ではMCPサーバーがプロトコルをコードで強制するが、2-Tierではトップ層の自律性に依存する
2. **インセンティブの不一致**: DeepSeekはWorker（7B）より高品質なコードを生成できるため、「自分で書いた方が速い」という誘惑に負ける
3. **Fixサイクルでの退化**: Worker（7B）に修正指示を出すと、ファイル全体を再生成して正常なコードまで破壊する傾向がある

### 対策
- **3-Tier（MCPあり）を使用する**: `ekp-forge-manager` MCPサーバー経由の `run_managed_task` でプロトコルを強制
- **FixはManagerが担当する**: 修正はWorkerに委譲せず、Manager（DeepSeek）が `apply_diff` で外科的修正を行う
- **監視**: 人間がトップ層の行動を監視し、プロトコル違反を指摘する（現時点では唯一の確実な対策）

### 参考
- 試験詳細: [`plans/2layer_abm_fbs_capability_plan.md`](../plans/2layer_abm_fbs_capability_plan.md)
- 試験ログ: `/home/tomo/project/001_abm/mcp_test/abm_fbs_sim_2tier/`
- Worker性能分析: [`QCD_EKP_VS_PURE.md`](../../001_abm/mcp_test/QCD_EKP_VS_PURE.md)
