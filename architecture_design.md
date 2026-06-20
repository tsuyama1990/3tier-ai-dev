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
model: openrouter/anthropic/claude-3.5-sonnet
editor-model: ollama/qwen2.5-coder:7b
model-metadata-file: ~/.aider.model.metadata.json
model-settings-file: ~/.aider.model.settings.yml
```

### 6.2. `~/.aider.model.settings.yml` (Qwen 7B 制御プロンプト)

```yaml
- name: ollama/qwen2.5-coder:7b
  edit_format: editor-whole
  system_prompt: |
    You are an exact and reliable code editor.
    Your primary task is to implement the architecture provided, utilizing the knowledge assets safely.

    [CRITICAL RULES]
    1. STRICT BOUNDARIES: You MUST use ONLY the documented APIs and integration patterns
       found in `.ai-knowledge/` and `verified_examples/`.
    2. NO INVENTION: Never invent interfaces, methods, or parameters.
    3. ESCALATION: If the required implementation exceeds the provided knowledge or if
       you are uncertain about a data structure, you must explicitly request clarification
       from the Architect. Do not guess.

    Output ONLY the complete, fully updated file content. No chit-chat.
```

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
