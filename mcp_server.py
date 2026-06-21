import subprocess
from mcp.server.fastmcp import FastMCP
from orchestrator import REAL_AIDER
from orchestrator_api import run_3tier_dev

mcp = FastMCP("EKP-Forge")

@mcp.tool()
def execute_simple_aider(prompt: str, target_files: list[str], model: str = None) -> dict:
    """
    Execute aider with a simple message without static analysis or self-repair.
    """
    cmd = [REAL_AIDER, "--message", prompt, "--yes"]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(target_files)
    
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL
    )
    
    return {
        "success": res.returncode == 0,
        "stdout": res.stdout,
        "stderr": res.stderr
    }

@mcp.tool()
def execute_strict_compile(
    prompt: str,
    target_pkg: str,
    target_files: list[str],
    model: str = "ollama/qwen2.5-coder:7b"
) -> dict:
    """
    Execute strict compilation pipeline through run_3tier_dev.
    """
    return run_3tier_dev(
        prompt=prompt,
        target_pkg=target_pkg,
        target_files=target_files,
        model=model,
        timeout=600
    )

if __name__ == "__main__":
    mcp.run()
