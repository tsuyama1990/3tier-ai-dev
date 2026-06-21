# EKP/DSC Pipeline Skills (実行可能スキル・手順書)

本リポジトリで提供される Dependency Semantic Compiler (DSC) パイプラインの各ステージを、開発者やAIエージェントが「実行可能なスキル」として活用するためのコマンド体系およびインターフェース仕様をまとめます。本ファイルはリポジトリ内に永続的に保持され、Gitで管理可能です。

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

## 2. 開発エージェント向けインテグレーション・スキル

AIエージェント（Aider等）が本リポジトリの能力を使い、ハルシネーションを起こさずに開発するための連携スキルです。

### ゲートキーパー（AST-Based MVG）の実行
プロジェクトコードを実行する前に、ホワイトリスト `api_schema.yaml` に違反するインポートが混入していないか静的チェックを実行します。

- **コマンド:**
  ```bash
  python3 orchestrator.py --check <検証対象のPythonスクリプトファイル>
  ```
- **エスカレーションフロー:** 
  違反（ホワイトリストにないライブラリのインポートや、危険な `eval()` 等の使用）が検出された場合、AST解析によって即時ブロックされエラーを返します。AIはこれを基に、憶測でのコード記述をやめて実装を修正するか、アーキテクトにエスカレーションします。
