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
            "manager.py",
            "worker.py",
            "rag_crawler.py",
            "adversarial_tester.py",
            "task_tree.py",
        ]:
            continue
        # Skip schemas files
        if "schemas" in py_file.parts:
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


def setup_ruff_mypy() -> None:
    venv_dir = Path(".venv")
    if sys.platform == "win32":
        ruff_exe = venv_dir / "Scripts" / "ruff.exe"
        mypy_exe = venv_dir / "Scripts" / "mypy.exe"
    else:
        ruff_exe = venv_dir / "bin" / "ruff"
        mypy_exe = venv_dir / "bin" / "mypy"

    if not ruff_exe.exists() or not mypy_exe.exists():
        log("Ruff or Mypy missing in .venv. Installing via uv...")
        try:
            subprocess.run(
                ["uv", "add", "--dev", "ruff", "mypy"],
                capture_output=True,
                text=True,
                check=True
            )
            log("Ruff and Mypy installed successfully.")
        except Exception as e:
            log(f"Failed to install via uv: {e}. Trying pip...")
            try:
                pip_path = venv_dir / "bin" / "pip" if sys.platform != "win32" else venv_dir / "Scripts" / "pip.exe"
                subprocess.run(
                    [str(pip_path), "install", "ruff", "mypy"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                log("Ruff and Mypy installed successfully via pip.")
            except Exception as pip_err:
                log(f"Failed to install via pip: {pip_err}")

    toml_path = Path("pyproject.toml")
    if not toml_path.exists():
        log("No pyproject.toml found. Creating one with strict settings.")
        toml_content = """[tool.ruff]
target-version = "py313"
line-length = 120

[tool.ruff.lint]
select = ["E", "W", "F", "I", "UP", "B", "A", "C4", "T20", "RET", "SIM", "ARG", "ERA", "PL", "RUF", "C90", "N", "ANN", "S", "BLE", "FBT"]

[tool.mypy]
files = ["."]
strict = true
ignore_missing_imports = true
"""
        toml_path.write_text(toml_content, encoding="utf-8")
        return

    content = toml_path.read_text(encoding="utf-8")
    modified = False

    if "[tool.mypy]" not in content:
        content += "\n\n[tool.mypy]\nfiles = [\".\"]\nstrict = true\nignore_missing_imports = true\n"
        modified = True
    else:
        lines = content.splitlines()
        mypy_idx = -1
        next_sec_idx = -1
        for idx, line in enumerate(lines):
            line_s = line.strip()
            if line_s == "[tool.mypy]":
                mypy_idx = idx
            elif mypy_idx != -1 and line_s.startswith("[") and line_s.endswith("]"):
                next_sec_idx = idx
                break
        
        section_lines = lines[mypy_idx:next_sec_idx] if next_sec_idx != -1 else lines[mypy_idx:]
        has_strict = any(line.strip().replace(" ", "") == "strict=true" for line in section_lines)
        if not has_strict:
            lines.insert(mypy_idx + 1, "strict = true")
            content = "\n".join(lines)
            modified = True

    required_lints = ["C90", "N", "ANN", "S", "BLE", "FBT"]
    if "[tool.ruff.lint]" not in content:
        content += "\n\n[tool.ruff.lint]\nselect = " + str(required_lints) + "\n"
        modified = True
    else:
        import re
        match = re.search(r"select\s*=\s*\[([^\]]*)\]", content, re.DOTALL)
        if match:
            select_content = match.group(1)
            existing_rules = re.findall(r'["\']([^"\']+)["\']', select_content)
            added_rules = [r for r in required_lints if r not in existing_rules]
            if added_rules:
                new_rules = existing_rules + added_rules
                new_select = "select = [\n" + ",\n".join(f'    "{r}"' for r in new_rules) + ",\n]"
                content = content.replace(match.group(0), new_select)
                modified = True
        else:
            lines = content.splitlines()
            lint_idx = -1
            for idx, line in enumerate(lines):
                if line.strip() == "[tool.ruff.lint]":
                    lint_idx = idx
                    break
            if lint_idx != -1:
                lines.insert(lint_idx + 1, "select = " + str(required_lints))
                content = "\n".join(lines)
                modified = True

    if modified:
        log("Writing updated strict rules to pyproject.toml.")
        toml_path.write_text(content, encoding="utf-8")


def run_ruff() -> tuple[bool, str]:
    if os.environ.get("IN_ORCHESTRATOR_TEST"):
        return True, "Ruff bypassed in test context"
    log("Running ruff...")
    venv_dir = Path(".venv")
    ruff_path = venv_dir / "bin" / "ruff" if sys.platform != "win32" else venv_dir / "Scripts" / "ruff.exe"
    cmd = [str(ruff_path), "check", "."] if ruff_path.exists() else ["ruff", "check", "."]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True
        )
        log(f"Ruff exit code: {result.returncode}")
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        log(f"Ruff failed to run: {e}")
        return False, str(e)


def run_mypy() -> tuple[bool, str]:
    if os.environ.get("IN_ORCHESTRATOR_TEST"):
        return True, "Mypy bypassed in test context"
    log("Running mypy...")
    venv_dir = Path(".venv")
    mypy_path = venv_dir / "bin" / "mypy" if sys.platform != "win32" else venv_dir / "Scripts" / "mypy.exe"
    cmd = [str(mypy_path), "."] if mypy_path.exists() else ["mypy", "."]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True
        )
        log(f"Mypy exit code: {result.returncode}")
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        log(f"Mypy failed to run: {e}")
        return False, str(e)
