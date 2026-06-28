#!/bin/bash

# 1. zshを非対話モードで動かし、.zshrc にある OPENROUTER_API_KEY を抽出して環境変数にエクスポート
if [ -f "$HOME/.zshrc" ]; then
    export OPENROUTER_API_KEY=$(zsh -c 'source ~/.zshrc && echo $OPENROUTER_API_KEY')
fi

# 2. キーが取得できているかチェック（念のため）
if [ -z "$OPENROUTER_API_KEY" ]; then
    echo "[Warning] OPENROUTER_API_KEY could not be loaded from .zshrc" >&2
fi

# 3. Auto-start Ollama if not running
if ! curl -s http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
    echo "[Info] Ollama not running. Starting ollama serve..." >&2
    ollama serve &
    disown
    for i in $(seq 1 10); do
        if curl -s http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
            echo "[Info] Ollama ready" >&2
            break
        fi
        sleep 1
    done
else
    echo "[Info] Ollama already running" >&2
fi

# 4. 現在実行されたリポジトリのパスを動的に取得
CURRENT_REPO_PATH=$(pwd)

# 5. 実際のaider実行可能ファイルのパス
AIDER_BIN="/home/tomo/.local/bin/aider"

# 6. aider-mcp の Python インタプリタを使用して run_aider_mcp.py を実行
#    - run_aider_mcp.py は logging を stderr にリダイレクトしてから aider_mcp を起動する
#    - これにより MCP の JSONRPC プロトコルがログ出力で破損するのを防ぐ
AIDER_MCP_PYTHON="/home/tomo/.local/share/uv/tools/aider-mcp/bin/python3"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$AIDER_MCP_PYTHON" "$SCRIPT_DIR/run_aider_mcp.py" \
  --aider-path "$AIDER_BIN" \
  --repo-path "$CURRENT_REPO_PATH"
