#!/bin/bash
# EKP-Forge MCP Server (3-Tier: Director=DeepSeek, Manager=DeepSeek API, Worker=Aider/Ollama)
# Stdio-based MCP server for VSCode integration

# 1. Load API keys from .zshrc
if [ -f "$HOME/.zshrc" ]; then
    export DEEPSEEK_API_KEY=$(zsh -c 'source ~/.zshrc && echo $DEEPSEEK_API_KEY')
    export OPENROUTER_API_KEY=$(zsh -c 'source ~/.zshrc && echo $OPENROUTER_API_KEY')
fi

# 2. Key check
if [ -z "$DEEPSEEK_API_KEY" ]; then
    echo "[Warning] DEEPSEEK_API_KEY not loaded from .zshrc" >&2
fi

# 3. Auto-start Ollama if not running
if ! curl -s http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
    echo "[Info] Ollama not running. Starting ollama serve..." >&2
    # Start in background, disown so it survives parent exit
    ollama serve &
    disown
    # Wait for it to be ready (up to 10 seconds)
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

# 4. Project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 5. Run the MCP server (stdio transport)
exec uv run --directory "$SCRIPT_DIR" python -m ekp_forge.mcp_server
