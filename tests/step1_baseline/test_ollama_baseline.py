"""
Step 1: Baseline Communication Test

Purpose: Verify Ollama can be reached, responds within timeout,
and returns structured JSON via run_3tier_dev().
"""

import os
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator_api import run_3tier_dev

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "ollama/qwen2.5-coder:7b"


def test_ollama_service_available():
    """Step 1-A: Ollama service is reachable at localhost:11434."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    except requests.exceptions.ConnectionError:
        pytest.fail(
            "Ollama is not running. Start with: ollama serve"
        )


def test_ollama_baseline_communication():
    """
    Step 1-B: run_3tier_dev() communicates with Ollama without timeout.

    Sends a simple fibonacci implementation prompt and verifies:
    - Returns a dict with 'success' and 'status' keys
    - status != 'timeout'
    - Completes within the timeout window
    """
    target_file = "tests/step1_baseline/generated/fibonacci.py"
    Path(target_file).parent.mkdir(parents=True, exist_ok=True)

    prompt = (
        "Implement a function `fibonacci(n: int) -> list[int]` that returns "
        "the first n Fibonacci numbers as a list. Include a docstring."
    )

    result = run_3tier_dev(
        prompt=prompt,
        target_pkg="fake_lib",
        target_files=[target_file],
        timeout=600,
        model=OLLAMA_MODEL,
        skip_self_healing=True,
    )

    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert "success" in result, f"Missing 'success' key: {result}"
    assert "status" in result, f"Missing 'status' key: {result}"
    assert result.get("status") != "timeout", (
        "Ollama communication timed out — check service and model availability"
    )

    print(f"\n[Step 1-B] status={result.get('status')}, success={result.get('success')}")


def test_fibonacci_implementation_exists():
    """
    Step 1-C: Verify the generated fibonacci.py exists and contains valid Python.
    """
    target_file = "tests/step1_baseline/generated/fibonacci.py"

    if not os.path.exists(target_file):
        pytest.skip(
            "Fibonacci file not generated - run test_ollama_baseline_communication first"
        )

    with open(target_file, encoding="utf-8") as f:
        content = f.read()

    # Verify it's valid Python by compiling
    try:
        compile(content, target_file, "exec")
    except SyntaxError as e:
        pytest.fail(f"Generated file has syntax error: {e}")

    # Verify fibonacci function exists
    assert "def fibonacci" in content, (
        "Generated file must contain fibonacci function definition"
    )
