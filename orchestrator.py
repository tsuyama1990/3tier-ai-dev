import os
import subprocess
import sys
from pathlib import Path

import yaml

REAL_AIDER = "aider"

def log(msg: str) -> None:
    print(f"[Orchestrator] {msg}")

def run_tests() -> tuple[bool, str]:
    # Prevent infinite recursion when called from test context
    if os.environ.get("IN_ORCHESTRATOR_TEST"):
        return True, "test_calc.py"

    log("Running pytest...")
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest", "-v", "--tb=short",
                "--ignore=tests/step1_baseline",
                "--ignore=tests/step2_fake_api",
                "--ignore=tests/step3_stress",
                "--ignore=tests/step4_ollama_synthesizer"
            ],
            capture_output=True,
            text=True
        )
        log(f"Pytest exit code: {result.returncode}")
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        log(f"Pytest failed to run: {e}")
        return False, str(e)

def cleanup_files() -> None:
    # Remove common temporary files
    for pattern in ["*.pyc", "__pycache__", ".pytest_cache"]:
        subprocess.run(["rm", "-rf", pattern], capture_output=True)

def validate_imports() -> tuple[bool, str]:
    schema_path = Path("api_schema.yaml")
    if not os.path.exists("api_schema.yaml"):
        return True, "No schema file found"

    with open(schema_path) as f:
        schema = yaml.safe_load(f)

    allowed = set(schema.get("allowed_imports", []))
    dangerous_builtins = {"eval", "exec", "compile", "open"}

    for py_file in Path().rglob("*.py"):
        # Skip virtual environment files
        if ".venv" in py_file.parts:
            continue
        # Skip core orchestrator and system files
        if py_file.name in [
            "orchestrator.py",
            "orchestrator_api.py",
            "test_orchestrator_api.py",
            "mcp_server.py",
            "test_mcp_server.py",
        ]:
            continue
        # Skip DSC synthesizer files
        if "dsc" in py_file.parts:
            continue
        # Skip validation on tests themselves, but allow generated test targets
        if "tests" in py_file.parts and "generated" not in py_file.parts:
            continue

        content = py_file.read_text()
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("import ") or line.startswith("from "):
                pkg = line.split()[1].split(".")[0]
                if pkg not in allowed and not pkg.startswith("_"):
                    return False, f"Unauthorized import of '{pkg}' in {py_file}"
            for danger in dangerous_builtins:
                if f"{danger}(" in line:
                    return False, f"Dangerous builtin '{danger}()' in {py_file}"

    return True, "All imports valid"

def run_cleanup() -> None:
    cleanup_files()
    # Also clean git lock files
    lock_path = Path(".git/index.lock")
    if lock_path.exists():
        lock_path.unlink(missing_ok=True)
