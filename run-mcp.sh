#!/bin/bash

# 1. zshを非対話モードで動かし、.zshrc にある OPENROUTER_API_KEY を抽出して環境変数にエクスポート
if [ -f "$HOME/.zshrc" ]; then
    export OPENROUTER_API_KEY=$(zsh -c 'source ~/.zshrc && echo $OPENROUTER_API_KEY')
fi

# 2. キーが取得できているかチェック（念のため）
if [ -z "$OPENROUTER_API_KEY" ]; then
    echo "[Error] OPENROUTER_API_KEY could not be loaded from .zshrc" >&2
fi

# 3. 現在実行されたリポジトリのパスを動的に取得
CURRENT_REPO_PATH=$(pwd)

exec /home/tomo/.local/bin/aider-mcp \
  --aider-path "/home/tomo/project/000_devenv/ekp-forge/ekp_forge/orchestrator.py" \
  --repo-path "$CURRENT_REPO_PATH"