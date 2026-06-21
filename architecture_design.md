# Executable Knowledge Platform (EKP) / Dependency Semantic Compiler (DSC) 詳細設計書

## 1. システム概要

本システムは、静的テキスト検索（RAG）によるハルシネーションを排除し、動作保証済みのコードと実行トレースから「実行可能な知識資産（Executable Knowledge）」をコンパイルする基盤である。抽出された知識はグローバルキャッシュに蓄積され、各プロジェクトのローカル環境へ展開された後、基幹AI（Aider/Ollama）によって参照される。

---

## 2. ディレクトリ・アーキテクチャ

プロジェクト間の依存関係（バージョン）衝突を防ぐため、知識資産の保管場所（グローバル）と展開場所（ローカル）を完全に分離する。シンボリックリンクはGit管理との互換性欠如のため使用せず、実体コピー（Hard Copy）を採用する。

### 2.1. グローバルキャッシュ領域

システム全体の「知識のハブ」として機能する中央保管庫。APIの破壊的変更による汚染を防ぐため、パッケージ名およびその厳密なバージョン番号ごとに完全に隔離されたディレクトリ階層で管理される。

```
~/.knowledge-cache/
├── {package_name}/             # 対象ライブラリ名 (例: mesa, ase, pacemaker)
│   └── {version}/              # 厳密なセマンティックバージョニング (例: 3.5.1, 3.28.0)
│       ├── integration_graph.md  # クラス・メソッドの依存関係とデータ構造の制約を記述した意味論的マップ
│       ├── workflow_graph.md     # 複数ライブラリを跨ぐ研究フローの境界条件を記述した統合グラフ
│       ├── verified_examples/    # Trust Score 0.9以上の動作確認済み最小実装コード
│       │   └── *.py
│       └── verified_tests/       # CUDA/MPI等の重依存を排除した軽量ヘルスチェック用テスト
│           └── smoke_*.py
```

このグローバルキャッシュは新規プロジェクト立ち上げ時のリファレンスプールとして機能し、知識資産の再計算コストを大幅に削減する。

### 2.2. プロジェクトローカル領域（適用先）

```
project/
├── .venv/                     # uv隔離環境
├── .ai-knowledge/             # グローバルキャッシュからの実体コピー
│   ├── {package}.md           # integration_graph.md のコピー（リネーム）
│   └── workflow_graph.md
├── verified_examples/         # 動作保証済み写経用テンプレート
├── verified_tests/            # 疎通確認用テストコード
├── api_schema.yaml            # MVGインポートホワイトリスト
└── src/
```

---

## 3. DSC (Dependency Semantic Compiler) パイプライン

知識の抽出・評価・アセット化を行う4段階のコンパイラプロセス。

### 3.1. Package Inspector

プロジェクトの `.venv` を走査し、ターゲットパッケージの正確なバージョンとソース元（GitHubリポジトリ等）を特定する。

### 3.2. Source & CI Miner

対象リポジトリから `tests/` および `examples/` を優先的に抽出する。

### 3.3. Smoke Tracer

フルテスト（pytest）の実行をスキップし（CUDA/MPI等への環境依存による失敗を回避するため）、インターフェース境界の初期化（例: `Trainer()` のインスタンス化）のみを行う最小コード（Smoke Trace）をローカルで実行検証する。

### 3.4. Asset Synthesizer

トレース結果とTrust Scoreに基づき、最終的なナレッジアセット（MarkdownグラフおよびPythonスクリプト）を生成し、`~/.knowledge-cache/` へ出力する。

**LLM連携（`--llm` オプトイン）:**
- **デフォルト（`--no-llm`）:** オフライン・高速モード。AST解析やトレース情報のみに基づき、APIサーフェステーブルを格納した基本的な `integration_graph.md` を生成する。
- **LLMモード（`--llm`）:** オンライン・セマンティック生成。収集した動作保証コード（Trust Score 0.9以上）と実行トレース情報をOpenRouter経由でLLMに送信し、クラス・メソッドの意味論的依存関係や制約条件を記述した高度なドキュメントを生成する。デフォルトモデルは `deepseek/deepseek-v4-flash`（`--llm-model` で変更可能）。

---

## 4. Trust Score (信頼度) 評価モデル

静的ドキュメントの腐敗リスクを定量化し、AIへの情報優先順位を決定するスコアリングロジック。

| 情報ソース | 基本スコア | 評価・変動条件 | 役割 |
|---|---|---|---|
| Smoke Tests (`tests/`) | 1.0 | CIおよびSmoke Tracerでパスしたコード | 絶対的真実・最優先インターフェース |
| Examples (`examples/`) | 0.9 | 実行時エラー（非推奨API等）で 0.0 へ降格 | 実装テンプレート |
| Type/Docstring (`*.pyi`) | 0.7 | 静的解析。意味論が不足 | 型制約の補助 |
| README (`*.md`) | 0.4 | 最終更新日時により減点 | 概念把握のみ（コードは棄却） |

---

## 5. 出力ナレッジアセット定義

単なる説明書ではなく、**実行可能な仕様書**として機能する。

- **`verified_examples/*.py`**: SLMが直接コピー＆ペースト（写経）可能な、動作確認済みの最小実装コード。
- **`integration_graph.md`**: 単体ライブラリ内部でのデータの変形と依存制約を記述したグラフ。
- **`workflow_graph.md`**: 複数ライブラリ間（例: ASE → pacemaker → LAMMPS）の境界におけるデータフロー、必須パラメータ（`info["energy"]` 等）、および型変換の制約を定義した統合グラフ。

---

## 6. 基幹AIシステム構成

AIエージェントがハルシネーションを起こさず、上記アセットを厳格に遵守するためのグローバル設定。

### 6.1. `~/.aider.conf.yml`

```yaml
model: openrouter/google/gemma-4-31b-it:free
editor-model: ollama/qwen2.5-coder:7b-instruct-q4_K_M
model-settings-file: .aider.model.settings.yml
map-tokens: 1024
timeout: 120
```

- **`model`**: 基幹となる推論/設計AIモデル。`openrouter/google/gemma-4-31b-it:free` や `openrouter/deepseek/deepseek-v4-flash` 等のLLMを指定。
- **`editor-model`**: コード編集を実行するローカルLLM（`ollama/qwen2.5-coder:7b-instruct-q4_K_M` 等）。
- **`model-settings-file`**: モデルごとのシステムプロンプト接頭辞や編集フォーマットを定義する設定ファイル名。
- **`model-metadata-file`**: モデルのトークン制限やプロバイダ情報を定義したメタデータファイル（`~/.aider.model.metadata.json` 等）。Aider起動パラメータで明示的に指定可能（`--model-metadata-file /home/tomo/.aider.model.metadata.json`）。
- **`map-tokens`**: トークンマップの最大サイズ。
- **`timeout`**: API呼び出しのタイムアウト秒数。

### 6.2. `~/.aider.model.settings.yml` (ロール定義・制御プロンプト)

本ファイルでは、Aider が利用する各種モデルの役割（`edit_format`）や、行動境界を定義するプロンプト接頭辞（`system_prompt_prefix`）をモデルごとに設定する。

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

- name: ollama/qwen2.5-coder:7b-instruct-q4_K_M
  edit_format: editor-whole
  system_prompt_prefix: |
    You are an exact and reliable code developer.
    Your task is to design and implement changes safely based on the user request.

    [CRITICAL RULES]
    1. STRICT BOUNDARIES: You MUST use ONLY the documented APIs in `.ai-knowledge/` and `verified_examples/`.
    2. NO INVENTION: Never invent interfaces, methods, or parameters.
    3. ESCALATION: If the user request refers to or requires any class, function, method, parameter, or module that is not explicitly documented in the `.ai-knowledge/` files or `verified_examples/`, you MUST refuse to proceed. Immediately respond with: "ESCALATION: API <name> is undocumented." and stop. Do NOT guess, do NOT mock, and do NOT implement stub classes, and do NOT write any implementation code.
    4. Output ONLY the raw file content. Do NOT wrap the code in markdown code blocks or backticks.
```

- **`system_prompt_prefix` の採用:** 既存の `system_prompt`（Aider全体のプロンプトを上書き）の代わりに `system_prompt_prefix` を使用し、Aider本来のプロトコル用命令を維持しつつ開発者の行動境界を定義する。
- **エスカレーション・ルールの明記:** 必要な知識が `.ai-knowledge/` や `verified_examples/` に不足している場合、LLMによる憶測（ハルシネーション）を徹底的に排除するため、「インポートできない/実装できない」として即時エラー応答（`ESCALATION:`）を返すよう制御する。

---

## 7. 決定論的ゲートキーパー (AST-Based MVG)

プロンプトによる確率論的な制御の限界を克服するため、オーケストレータにAST解析を用いた Minimal Viable Gatekeeper (MVG) を導入する。`pytest` 実行前に静的解析を行い、違反を即座にコンパイラエラーとしてフィードバックする。

### 7.1. 動作仕様

**静的および動的インポートの検証:**
`api_schema.yaml` に定義されたホワイトリストに基づき、ASTの `Import`, `ImportFrom` ノードを検証する。さらに `ast.Call` を走査し、`__import__` や `importlib.import_module` による動的インポートを検知。非リテラル引数の場合は無条件でブロックする。

**ローカルモジュールの自動ホワイトリスト化:**
`_discover_local_modules()` によってプロジェクト内のローカルパッケージ（`src/`, `tests/` 等）を動的スキャンし、`api_schema.yaml` の手動保守なしに自動的に許可リストへ追加する。

**危険な組み込み関数の完全禁止:**
`eval()`, `exec()`, `compile()` はインポートの有無にかかわらず無条件でブロックする。

### 7.2. 運用上の限界とトレードオフ

**ローカルモジュール自動検出のリスク:**
既存ファイルを無条件に安全とみなすため、LLMが悪意のある（または意図しない）ローカルファイルを作成し別ファイルからインポートさせるトロイの木馬的アプローチを防ぐことができない。

**リフレクションと動的ディスパッチによる回避:**
`getattr(__builtins__, 'ev' + 'al')` のような文字列操作や変数を経由したモジュールインポートは ASTレベルの静的解析では捕捉できない。これを完全に防ぐには制御フロー解析が必要となるが、MVGの設計意図（軽量な静的チェック）を超えるため意図的に除外されている。

**APIメソッドレベルの統制:**
許可されたモジュール内における架空のクラス・メソッドの呼び出しはASTでは静的型付けなしに判断できない。`pytest` によるランタイム実行時のTraceback に依存して検知・修復を行う設計となっている。

---

## 8. デプロイ・パイプライン (dsc/deploy.py)

グローバルキャッシュ領域（`~/.knowledge-cache/`）に蓄積された「実行可能な知識資産（Executable Knowledge）」を、ターゲットプロジェクトのローカル環境へ自動展開するための配備メカニズム。本パイプラインは、シンボリックリンクを使用しない「実体コピー（Hard Copy）契約」に基づき動作する。

### 8.1. 展開マッピングと処理フロー

デプロイスクリプトを実行すると、指定されたパッケージおよびバージョンのアセットが以下のルールでプロジェクトのローカルディレクトリに展開される。

1. **`integration_graph.md` → `.ai-knowledge/{package_name}.md`**
   - パッケージ別の依存マップファイルを、パッケージ名に対応するMarkdownファイル名でコピーする。
2. **`workflow_graph.md` → `.ai-knowledge/workflow_graph.md` (マージ処理)**
   - 複数パッケージ（例: `ase` と `pacemaker`）が同時に展開される場合、それぞれの `workflow_graph.md` を解析し、自動的に連結・マージして単一の `workflow_graph.md` として書き出す。
3. **`verified_examples/` → `verified_examples/`**
   - 動作保証済みの実装例（Trust Score 0.9以上）をローカルに実体コピーする。
4. **`verified_tests/` → `verified_tests/`**
   - 疎通確認用テストファイルをローカルに実体コピーする。

### 8.2. `api_schema.yaml` の自動生成と保護

決定論的ゲートキーパー（AST-Based MVG）のインポートホワイトリストとなる `api_schema.yaml` を自動的に作成または更新する。

- **差分追記（デフォルト）:** 既存の `api_schema.yaml` が既に存在する場合、デプロイ対象のパッケージ名を `allowed_imports` リストに自動追加する。この際、ユーザーが手動で定義した既存 of インポート許可（コメントや外部ライブラリ等）は削除・破壊されず、安全に保持される。
- **強制上書き (`--force`):** 新規にクリーンな `api_schema.yaml` を生成し、デプロイパッケージと標準ライブラリ、テスト用ライブラリのみに初期化する。

### 8.3. バージョン自動検出

デプロイ時にバージョン指定が省略された場合（例: `--packages mesa`）、グローバルキャッシュ領域（`~/.knowledge-cache/mesa/`）を自動的にスキャンし、最も更新日時（mtime）の新しいバージョンを自動選択してデプロイする。

### 8.4. 主要コマンドライン引数

- `--project DIR`: **[必須]** 展開先のプロジェクトの絶対パス（例: `--project /home/tomo/project/001_abm/test`）。
- `--packages PKG[=VER] ...`: **[manifestと排他]** デプロイするパッケージの指定。複数指定可能（例: `--packages ase=3.28.0 pacemaker=0.8.4`）。
- `--manifest FILE`: **[packagesと排他]** `package_inspector.py` が生成した JSON マニフェストファイルを指定し、検出された依存パッケージを一括でデプロイする。
- `--dry-run`: 実際のファイル書き込みや更新を行わず、実行されるコピーと生成内容をプレビューする。
- `--force`: 既存のアセットや `api_schema.yaml` を警告なしに強制上書きする。
