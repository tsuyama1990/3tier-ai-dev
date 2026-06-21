"""
Step 2: Fake API Reference Test

Purpose: Verify Ollama reads and follows .ai-knowledge/fake_lib.md constraints.
The test checks that generated code uses FakeCalculator(use_magic_mode=True, offset=-99)
exactly as documented, without hallucinating standard constructor patterns.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator_api import run_3tier_dev


def test_fake_api_reference():
    """
    Step 2-A, 2-B, 2-C, 2-D: Verify Ollama follows fake_lib constraints.

    The test checks that the generated code contains:
    - FakeCalculator(use_magic_mode=True, offset=-99) - the exact constructor signature
    - No positional arguments for FakeCalculator
    """
    # Read the prompt from file
    prompt_file = os.path.join(os.path.dirname(__file__), "prompt_fake_calculator.txt")
    with open(prompt_file, encoding="utf-8") as f:
        prompt = f.read()

    # Target file for generated code
    target_file = "tests/step2_fake_api/generated/my_app.py"

    # Ensure generated directory exists
    os.makedirs(os.path.dirname(target_file), exist_ok=True)

    # Temporarily create api_schema.yaml to allow fake_lib imports
    schema_path = Path("api_schema.yaml")
    schema_existed = schema_path.exists()
    if not schema_existed:
        schema_path.write_text("allowed_imports:\n  - fake_lib\n  - typing\n")

    try:
        # Force Ollama model via explicit --model override
        result = run_3tier_dev(
            prompt=prompt,
            target_pkg="fake_lib",
            target_files=[target_file],
            timeout=600,
            model="ollama/qwen2.5-coder:7b",
            skip_self_healing=True,
        )
    finally:
        # Clean up temporary api_schema.yaml
        if not schema_existed and schema_path.exists():
            schema_path.unlink()

    # Log result for debugging
    print(
        f"\n[Step 2 Result] status={result.get('status')}, success={result.get('success')}"
    )

    # Verify file was generated
    assert os.path.exists(target_file), f"Generated file {target_file} should exist"

    with open(target_file, encoding="utf-8") as f:
        content = f.read()

    print(f"\n[Step 2 Generated code]:\n{content}")

    # Step 2-B: use_magic_mode=True must be present
    assert "use_magic_mode=True" in content, (
        "Generated code must contain use_magic_mode=True (per fake_lib.md)"
    )

    # Step 2-C: offset=-99 must be present
    assert "offset=-99" in content, (
        "Generated code must contain offset=-99 (per fake_lib.md)"
    )

    # Step 2-D: No positional args (FakeCalculator must use keyword args)
    import ast
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            func_name = ""
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name == "FakeCalculator":
                assert len(node.args) == 0, (
                    "FakeCalculator must use keyword arguments only, got positional args"
                )


def test_my_app_class_exists():
    """
    Step 2-E: Verify MyApp class has add and multiply methods.
    """
    target_file = "tests/step2_fake_api/generated/my_app.py"

    if not os.path.exists(target_file):
        pytest.skip("my_app.py not generated - run test_fake_api_reference first")

    with open(target_file, encoding="utf-8") as f:
        content = f.read()

    assert "class MyApp" in content, "Generated file must define MyApp class"
    assert "def add" in content, "MyApp must have add method"
    assert "def multiply" in content, "MyApp must have multiply method"
