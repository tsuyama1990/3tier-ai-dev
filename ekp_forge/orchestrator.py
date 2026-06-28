#!/usr/bin/env python3
"""Orchestrator helper utilities — linting, type-checking, environment setup.

Phase 3 cleanup: removed dead functions (run_tests, cleanup_files, validate_imports, run_cleanup).
Only the functions actively used by the protocol-based architecture are retained.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REAL_AIDER = "aider"


def log(msg: str) -> None:
    """Print a timestamped orchestrator message."""
    print(f"[Orchestrator] {msg}")


def setup_ruff_mypy() -> None:
    """Ensure ruff and mypy are installed and pyproject.toml has strict settings."""
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
            subprocess.run(["uv", "add", "--dev", "ruff", "mypy"], capture_output=True, text=True, check=True)
            log("Ruff and Mypy installed successfully.")
        except Exception as e:
            log(f"Failed to install via uv: {e}. Trying pip...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "ruff", "mypy"], capture_output=True, text=True, check=True
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
        content += '\n\n[tool.mypy]\nfiles = ["."]\nstrict = true\nignore_missing_imports = true\n'
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


def run_ruff(files: list[str] | None = None) -> tuple[bool, str]:
    """Run ruff check on specified files (or whole project)."""
    if os.environ.get("IN_ORCHESTRATOR_TEST"):
        return True, "Ruff bypassed in test context"
    log("Running ruff...")
    venv_dir = Path(".venv")
    ruff_path = venv_dir / "bin" / "ruff" if sys.platform != "win32" else venv_dir / "Scripts" / "ruff.exe"
    targets = files if files else ["."]
    cmd = [str(ruff_path), "check"] + targets if ruff_path.exists() else ["ruff", "check"] + targets
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        log(f"Ruff exit code: {result.returncode}")
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        log(f"Ruff failed to run: {e}")
        return False, str(e)


def run_mypy(files: list[str] | None = None) -> tuple[bool, str]:
    """Run mypy on the project, optionally filtering output to target files."""
    if os.environ.get("IN_ORCHESTRATOR_TEST"):
        return True, "Mypy bypassed in test context"
    log("Running mypy...")
    venv_dir = Path(".venv")
    mypy_path = venv_dir / "bin" / "mypy" if sys.platform != "win32" else venv_dir / "Scripts" / "mypy.exe"

    cmd = [str(mypy_path), "."] if mypy_path.exists() else ["mypy", "."]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        output = result.stdout + result.stderr

        if files:
            filtered_lines = []
            for line in output.splitlines():
                for target_file in files:
                    clean_path = target_file.replace("\\", "/")
                    if clean_path in line.replace("\\", "/"):
                        filtered_lines.append(line)
                        break

            if not filtered_lines:
                return True, "Mypy passed on target files (other errors ignored)"
            return False, "\n".join(filtered_lines)

        log(f"Mypy exit code: {result.returncode}")
        return result.returncode == 0, output
    except Exception as e:
        log(f"Mypy failed to run: {e}")
        return False, str(e)
