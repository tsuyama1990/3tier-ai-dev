# EKP/DSC Pipeline Skills (実行可能スキル・手順書)

本リポジトリで提供される Dependency Semantic Compiler (DSC) パイプラインおよび EKP-Forge オーケストレーションフレームワークを、開発者やAIエージェントが「実行可能なスキル」として活用するためのコマンド体系およびインターフェース仕様をまとめます。本ファイルはリポジトリ内に永続的に保持され、Gitで管理可能です。

---

## 1. DSC パイプライン・スキル一覧

### Skill 1: パッケージの依存関係検出と解析 (Inspection)
プロジェクト内の `.venv` からターゲットパッケージの正確なバージョンとインストール元ソースURL（GitHub/GitLab等）を検出します。

- **コマンド:**
  ```bash
  python3 dsc/package_inspector.py --project <対象プロジェクトの絶対パス> --target <パッケージ名1> [パッケージ名2 ...] --output <出力マニフェストパス>
  ```
- **実行例:**
  ```bash
  python3 dsc/package_inspector.py --project /home/tomo/project/001_abm/test --target mesa --output /tmp/mesa_manifest.json
  ```
- **出力アセット:** 指定されたパスにマニフェストJSONファイルが書き出されます。

---

### Skill 2: テスト＆サンプルコードのマイニング (Mining)
検出されたソースURLから、`git clone`（またはGit sparse-checkout）を利用してテストコードとサンプルスクリプトを自動収集し、グローバルキャッシュへ初期格納します。

- **コマンド:**
  ```bash
  python3 dsc/source_miner.py --manifest <マニフェストJSONのパス>
  ```
- **実行例:**
  ```bash
  python3 dsc/source_miner.py --manifest /tmp/mesa_manifest.json
  ```
- **出力アセット:** `~/.knowledge-cache/{package_name}/{version}/verified_tests/` および `verified_examples/` に Python ファイルがコピーされます。

---

### Skill 3: 最小実行トレースの実行と検証 (Tracing)
マイニングされたスクリプトを隔離環境（`.venv` の python インタプリタ）で最小限実行し、動作保証判定と Trust Score の割り当てを行います。

- **コマンド:**
  ```bash
  python3 dsc/smoke_tracer.py --manifest <マニフェストJSONのパス>
  ```
- **実行例:**
  ```bash
  python3 dsc/smoke_tracer.py --manifest /tmp/mesa_manifest.json
  ```
- **出力アセット:** キャッシュディレクトリ内に `smoke_trace_report.json` が出力され、動作しなかったスクリプトやエラーを起こしたモジュールはキャッシュから除外されます。

---

### Skill 4: 意味論的アセットの生成 (Synthesis)
トレース結果と Trust Score に基づき、APIサーフェステーブルおよび制約マップを含む `integration_graph.md` をキャッシュに合成します。

- **コマンド (オフライン高速モード - デフォルト):**
  ```bash
  python3 dsc/asset_synthesizer.py --manifest <マニフェストJSONのパス> --no-llm
  ```
- **コマンド (LLMオプトイン・セマンティック生成):**
  ```bash
  # 実行前に OPENROUTER_API_KEY 環境変数を設定してください
  python3 dsc/asset_synthesizer.py --manifest <マニフェストJSONのパス> --llm
  ```
- **コマンド (ローカルOllamaを使用):**
  ```bash
  python3 dsc/asset_synthesizer.py --manifest <マニフェストJSONのパス> --llm-provider ollama
  ```
- **出力アセット:** キャッシュディレクトリ内に `integration_graph.md` が合成出力されます。

---

### Skill 5: キャッシュからのアセット配備 (Deployment)
グローバルキャッシュから開発プロジェクトのローカル環境へ、安全な実体コピー契約（Hard Copy）に基づいてアセットを展開し、`api_schema.yaml` のインポートホワイトリストを自動的かつ保護しながらマージします。

- **コマンド:**
  ```bash
  python3 dsc/deploy.py --project <配備先プロジェクトの絶対パス> --packages <パッケージ名=バージョン> [--force]
  ```
- **実行例 (バージョン自動検出機能を利用):**
  ```bash
  # バージョンを省略した場合、キャッシュ内の最新バージョンを自動選択
  python3 dsc/deploy.py --project /home/tomo/project/001_abm/test --packages mesa
  ```
- **出力アセット:**
  - `project/.ai-knowledge/{package_name}.md` (依存関係マップの配備)
  - `project/verified_examples/` & `project/verified_tests/` (コードの実展開)
  - `project/api_schema.yaml` (ユーザーコメントを保護したインポートリストのマージ)

---

## 2. EKP-Forge オーケストレーション・スキル

### Skill 6: 管理タスクパイプラインの実行 (Managed Task)
MCPサーバー経由で、トリアージ → アーキテクト → ワーカー → 検証 → 統合の完全パイプラインを実行します。

- **MCPツール:**
  ```
  run_managed_task(task_schema_json)
  ```
- **Python API:**
  ```python
  from ekp_forge.manager import ManagerAgent
  from ekp_forge.schemas.task_schema import TaskSchema

  manager = ManagerAgent()
  task = TaskSchema(
      task_id="T-20260624120000-abc123",
      manager_id="MGR-01",
      goal="Add input validation",
      constraints=["Must use Pydantic"],
      acceptance_tests=["pytest tests/"],
      affected_modules=["src/auth.py"],
  )
  status, plan = manager.triage(task)
  ```

### Skill 7: エピックタスクの分解と並列実行 (Epic Task)
大規模タスクをサブタスクに分解し、TaskTreeで並列実行します。

- **MCPツール:**
  ```
  run_epic_task(epic_task_json, subtasks_json_list)
  ```
- **Python API:**
  ```python
  from ekp_forge.task_tree import TaskTree
  from ekp_forge.worker import WorkerAgent

  tree = TaskTree()
  tree.decompose(epic_task, [subtask1, subtask2])
  results = tree.execute_parallel(worker, manager, max_workers=4)
  ```

### Skill 8: 簡易Aider実行 (Simple Aider)
静的解析や自己修復なしでAiderを直接実行します。

- **MCPツール:**
  ```
  execute_simple_aider(prompt, target_files, model?)
  ```
- **Python API:**
  ```python
  from ekp_forge.orchestrator_api import run_3tier_dev

  result = run_3tier_dev(
      prompt="Add docstrings to all functions",
      target_pkg="myapp",
      target_files=["src/utils.py"],
      model="ollama/qwen2.5-coder:7b",
  )
  ```

---

## 3. 開発エージェント向けインテグレーション・スキル

AIエージェント（Aider等）が本リポジトリの能力を使い、ハルシネーションを起こさずに開発するための連携スキルです。

### ゲートキーパー（AST-Based MVG）の実行
プロジェクトコードを実行する前に、ホワイトリスト `api_schema.yaml` に違反するインポートが混入していないか静的チェックを実行します。

- **Python API:**
  ```python
  from ekp_forge.orchestrator import validate_imports

  success, message = validate_imports()
  # success=False の場合、ホワイトリストにないインポートや eval/exec が検出されています
  ```
- **エスカレーションフロー:** 
  違反（ホワイトリストにないライブラリのインポートや、危険な `eval()` 等の使用）が検出された場合、AST解析によって即時ブロックされエラーを返します。AIはこれを基に、憶測でのコード記述をやめて実装を修正するか、アーキテクトにエスカレーションします。

### セルフヒーリングループの実行
Worker Agent は、Aider によるコード生成 → 検証 → エラー収集 → Aider再実行のサイクルを自動的に実行します。

- **Python API:**
  ```python
  from ekp_forge.worker import WorkerAgent

  worker = WorkerAgent(model="ollama/qwen2.5-coder:7b", max_retries=3)
  result = worker.execute_verification_loop(task, plan)
  ```

### サンドボックス分離実行
コード生成を一時的なサンドボックスディレクトリで実行し、ホストリポジトリを保護します。

- **Python API:**
  ```python
  from ekp_forge.sandbox.workspace import SandboxWorkspace
  from ekp_forge.sandbox.cloner import clone_into
  from ekp_forge.sandbox.integrator import integrate_changes

  with SandboxWorkspace() as ws_path:
      clone_ok, clone_err = clone_into(ws_path)
      # ... Worker が ws_path/repo/ 内でコード生成 ...
      success, msg, log = integrate_changes(project_root, ws_path)
  ```

### アーキテクト承認ゲート（ADR準拠チェック）
生成された実装計画が既存のADR（アーキテクチャ決定記録）に準拠しているかを、決定論的（非LLM）に検証します。

- **Python API:**
  ```python
  from ekp_forge.sandbox.architect_review import check_adr_compliance

  result = check_adr_compliance(task, plan_text)
  # result.compliant=False の場合、違反箇所と根拠が result.violations に格納
  ```

---

## 4. 参考情報

- **プロジェクト概要:** [`README.md`](README.md)
- **MCP/Aider設定詳細:** [`docs/detailed_guide.md`](docs/detailed_guide.md)
- **組織設計ドキュメント:** [`docs/organization_design.md`](docs/organization_design.md)
- **Safe Factory設計:** [`plans/safe_factory_architecture.md`](plans/safe_factory_architecture.md)
- **アーキテクチャ改善計画:** [`plans/review_driven_improvements.md`](plans/review_driven_improvements.md)
