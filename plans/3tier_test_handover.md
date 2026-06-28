# 3-Tier MCP Pipeline テスト引継書

## 目的
ABM-FBS論文（Bott & Mesmer 2019）のシミュレーションコード生成を題材に、
**3-Tier MCPパイプライン（Director=DeepSeek, Manager=DeepSeek V4 Flash API, Worker=Ollama 7B）**
の実力把握テストを実施する。

---

## 前提条件（セットアップ済み）

### 環境
| 項目 | 状態 | 確認方法 |
|------|------|---------|
| Python 3.13 + uv | ✅ | `uv run python --version` |
| Ollama (qwen2.5-coder:7b) | ✅ 稼働中 | `curl http://127.0.0.1:11434/api/tags` |
| Aider CLI (v0.86.2) | ✅ | `which aider` → `/home/tomo/.local/bin/aider` |
| `mcp` パッケージ (v1.28.0) | ✅ | `uv pip show mcp` |
| DeepSeek API Key | ✅ 環境変数 `DEEPSEEK_API_KEY` | `echo $DEEPSEEK_API_KEY` |
| OpenRouter API Key | ✅ 環境変数 `OPENROUTER_API_KEY` | |

### 設定ファイル
| ファイル | パス | 説明 |
|---------|------|------|
| MCPサーバー起動スクリプト | [`run-mcp-ekp.sh`](/home/tomo/project/000_devenv/ekp-forge/run-mcp-ekp.sh) | DeepSeek API + Ollama自動起動 |
| Aider-orchestrator起動 | [`run-mcp.sh`](/home/tomo/project/000_devenv/ekp-forge/run-mcp.sh) | aider-mcpラッパー |
| Cline MCP設定 | `~/.config/Antigravity IDE/.../mcp_settings.json` | `timeout: 300`, `alwaysAllow` 設定済 |
| プロジェクトMCP設定 | [`mcp_config.json`](/home/tomo/project/000_devenv/ekp-forge/mcp_config.json) | プロジェクトルート設定 |

### コード変更履歴
| 変更 | ファイル | 内容 |
|------|---------|------|
| `_call_deepseek()` 追加 | [`ekp_forge/manager.py`](/home/tomo/project/000_devenv/ekp-forge/ekp_forge/manager.py) | DeepSeek API呼び出しメソッド追加 |
| Validation優先順位変更 | [`ekp_forge/manager.py`](/home/tomo/project/000_devenv/ekp-forge/ekp_forge/manager.py) | DeepSeek→Ollama→OpenRouterの順に試行 |
| Ollama自動起動 | [`run-mcp-ekp.sh`](/home/tomo/project/000_devenv/ekp-forge/run-mcp-ekp.sh) + [`run-mcp.sh`](/home/tomo/project/000_devenv/ekp-forge/run-mcp.sh) | 起動時にOllama稼働確認、停止時は自動起動 |

---

## テスト手順

### Step 0: 事前準備（起動確認）
```bash
# Ollama自動起動は run-mcp-ekp.sh がハンドリングするので不要
# ただしTimeout対策のため Reload は必須

# MCPサーバーが生きているか確認
ps aux | grep mcp_server | grep -v grep
# 無ければReload: Ctrl+Shift+P → Developer: Reload Window
```

### Step 1: `generate_task_id` でタスクID発行
MCPツール `generate_task_id(goal: str)` を呼び出し、有効なタスクIDを生成する。
戻り値例: `{"task_id": "T-20260627-XXXXXX-XXXXXX"}`

### Step 2: `run_managed_task` で3-Tierパイプライン実行
TaskSchemaを作成し、`run_managed_task` を呼び出す。

**推奨テストケース：FBSモデル生成**
```json
{
  "task_id": "(generate_task_idで生成したID)",
  "manager_id": "MGR-Default-01",
  "goal": "Create FBSModel class with 5 states (R=0, F=1, Be=2, S=3, D=4)",
  "constraints": [
    "Use numpy for matrix operations",
    "Each row must sum to exactly 1.0",
    "Use 'if draw > prob' logic (NOT while loop)",
    "File must be at test_output/fbs_model.py"
  ],
  "acceptance_tests": [
    "FBSModel().transition_matrix.shape == (5,5)",
    "all(abs(FBSModel().transition_matrix.sum(axis=1) - 1.0) < 1e-9)",
    "FBSModel().get_state() == 0",
    "reset() sets state to R(0)"
  ],
  "affected_modules": ["test_output/fbs_model.py"],
  "assumptions_required": {},
  "force_accept": true
}
```

**期待されるパイプライン：**
```
1. ManagerAgent.triage()  → DeepSeek APIでタスク検証・計画
2. WorkerContract 生成   → Aiderに委譲
3. Aider(Ollama) コード生成 → test_output/fbs_model.py 作成
4. ruff/mypy 自動検証    → Diagnostic[] 生成
5. FixPlanner 優先順位付け → FixTask 生成
6. apply_diff で修正    → Managerが外科的修正
7. 全Diagnostic合格 → ADR生成・完了
```

### Step 3: 結果検証
```bash
# 生成ファイル確認
cat test_output/fbs_model.py

# ユニットテスト
uv run python -c "
from test_output.fbs_model import FBSModel
m = FBSModel(seed=42)
assert m.get_state() == 0  # R
assert all(abs(m.transition_matrix.sum(axis=1) - 1.0) < 1e-9)
ns, adv = m.attempt_transition_to_target(1)  # R→F
assert isinstance(ns, int) and isinstance(adv, bool)
print('ALL TESTS PASSED')
"
```

---

## 既知の問題・注意点

### 1. MCPタイムアウト（解決済み）
- **原因**: MCPクライアントのデフォルトタイムアウト60秒
- **対策**: `mcp_settings.json` に `"timeout": 300` を設定済み
  - それでもタイムアウトする場合は、`--keep-alive -1` でモデルをプリロードする

### 2. 2-Tierプロトコル遵守問題（アーキテクチャ課題）
- 2-Tier（MCP無し）ではトップ層（DeepSeek）が委譲プロトコルを守らない
- 3-Tier（MCPあり）はMCPサーバーがプロトコルをコードで強制するため問題なし
- 詳細: [`plans/2layer_abm_fbs_capability_plan.md`](/home/tomo/project/000_devenv/ekp-forge/plans/2layer_abm_fbs_capability_plan.md)

### 3. Worker（Ollama 7B）の修正時の退化
- 初回生成は詳細Specで精度95%
- 修正指示でファイル全体再生成 → 正常部分を破壊
- **対策**: FixループはManager（apply_diff）が担当し、Workerに修正させない

### 4. MCPサーバー切断問題
- タイムアウトなどでMCPサーバープロセスが死んだ場合、VSCode再読み込みが必要
- `ps aux | grep mcp_server` で死活確認可能

---

## 参考情報

| ドキュメント | パス | 内容 |
|------------|------|------|
| 2層試験計画 | [`plans/2layer_abm_fbs_capability_plan.md`](/home/tomo/project/000_devenv/ekp-forge/plans/2layer_abm_fbs_capability_plan.md) | 試験設計・QCD評価基準 |
| QCD比較 | `/home/tomo/project/001_abm/mcp_test/QCD_COMPARISON.md` | v1 Pure DeepSeek品質評価 |
| EKP vs Pure比較 | `/home/tomo/project/001_abm/mcp_test/QCD_EKP_VS_PURE.md` | v2 ekp-forge+Ollama品質評価 |
| 2層実装サンプル | `/home/tomo/project/001_abm/mcp_test/abm_fbs_sim_2layer/` | DeepSeek直接コード（参考実装） |
| README | [`README.md`](/home/tomo/project/000_devenv/ekp-forge/README.md) | プロジェクト概要・MCP設定説明 |
| 詳細ガイド | [`docs/detailed_guide.md`](/home/tomo/project/000_devenv/ekp-forge/docs/detailed_guide.md) | MCP設定・既知の問題 |
