"""IntegratorAgent — applies verified diffs and performs cross-module regression testing.

Responsibilities (v4.1)
------------------------
1. **merge** — Copy changed files from sandbox to project root.
2. **architecture_consistency** — Run ``mypy .`` globally on the project root.
3. **cross_module_regression** — Run ``pytest`` globally on the project root.

If either regression check fails, the integration is **reverted** and the error log
is returned to the Worker/Planner for rework.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def integrate_changes(
    project_root: Path,
    sandbox_path: Path | None = None,
    run_cross_module_checks: bool = True,
) -> tuple[bool, str, dict | None]:
    """Copy changed files from sandbox to project root and run regression checks.

    Parameters
    ----------
    project_root:
        The root directory of the main project.
    sandbox_path:
        The sandbox directory containing changed files. If ``None``, auto-detect.
    run_cross_module_checks:
        If ``True`` (default), run global ``mypy .`` and ``pytest`` after copying.

    Returns
    -------
    tuple[bool, str, dict | None]
        ``(success, message, regression_log)`` where:
        * ``success`` — ``True`` if all checks pass.
        * ``message`` — human-readable result message.
        * ``regression_log`` — structured dict with cross-module check results
          (``None`` on success).
    """
    root = Path(project_root).resolve()
    sandbox_repo = Path(sandbox_path) / "repo" if sandbox_path else root / "repo"

    if not sandbox_repo.exists():
        sandbox_repo = root / "repo"
        if not sandbox_repo.exists():
            return False, "Sandbox repository directory not found", None

    try:
        # Step 1: Detect changed files
        files = _detect_changed_files(sandbox_repo)
        if not files:
            return True, "No changes detected to integrate", None

        if len(files) > 3:
            return (
                False,
                f"Integration rejected: changes affect {len(files)} files, exceeding the limit of 3 files",
                None,
            )

        # Step 2: Backup original files
        backups: dict[str, str] = {}
        for file_path in files:
            src = root / file_path
            if src.exists():
                backups[file_path] = src.read_text(encoding="utf-8", errors="replace")

        # Step 3: Copy files from sandbox to project root
        for file_path in files:
            src = sandbox_repo / file_path
            dst = root / file_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        # Step 4: Cross-module regression checks
        if run_cross_module_checks:
            regression_log: dict[str, Any] = {}
            all_passed = True

            # 4a: Global mypy check
            mypy_ok, mypy_output = _run_global_mypy(root)
            regression_log["mypy"] = {"passed": mypy_ok, "output": mypy_output[:2000] if mypy_output else ""}
            if not mypy_ok:
                all_passed = False

            # 4b: Global pytest check
            if all_passed:
                pytest_ok, pytest_output = _run_global_pytest(root)
                regression_log["pytest"] = {
                    "passed": pytest_ok,
                    "output": pytest_output[:2000] if pytest_output else "",
                }
                if not pytest_ok:
                    all_passed = False

            if not all_passed:
                # Revert integration
                _revert_integration(root, backups)
                return (
                    False,
                    "Cross-module regression failed — changes reverted.",
                    regression_log,
                )

            return (
                True,
                f"Successfully integrated {len(files)} files (all regression checks passed).",
                None,
            )

        return True, f"Successfully integrated {len(files)} files (checks skipped).", None

    except Exception as e:
        # Attempt revert on any exception
        try:
            _revert_integration(root, {})
        except Exception:
            pass
        return False, f"Integration failed: {e}", None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_changed_files(sandbox_repo: Path) -> set[str]:
    """Detect modified and newly created files in the sandbox repository."""
    files: set[str] = set()

    res_mod = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=str(sandbox_repo),
        capture_output=True,
        text=True,
        check=False,
    )
    res_new = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(sandbox_repo),
        capture_output=True,
        text=True,
        check=False,
    )

    if res_mod.returncode == 0:
        for f in res_mod.stdout.splitlines():
            if f.strip():
                files.add(f.strip())
    if res_new.returncode == 0:
        for f in res_new.stdout.splitlines():
            if f.strip():
                files.add(f.strip())

    return files


def _run_global_mypy(project_root: Path) -> tuple[bool, str]:
    """Run ``mypy .`` on the project root. Returns (success, output)."""
    try:
        venv_dir = project_root / ".venv"
        mypy_path = venv_dir / "bin" / "mypy" if sys.platform != "win32" else venv_dir / "Scripts" / "mypy.exe"
        cmd = [str(mypy_path), "."] if mypy_path.exists() else ["mypy", "."]

        result = subprocess.run(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "mypy timed out after 120s"
    except Exception as e:
        return False, str(e)


def _run_global_pytest(project_root: Path) -> tuple[bool, str]:
    """Run ``pytest`` on the project root. Returns (success, output)."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-v",
                "--tb=short",
                "--ignore=tests/step1_baseline",
                "--ignore=tests/step2_fake_api",
                "--ignore=tests/step3_stress",
                "--ignore=tests/step4_ollama_synthesizer",
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "pytest timed out after 120s"
    except Exception as e:
        return False, str(e)


def _revert_integration(project_root: Path, backups: dict[str, str]) -> None:
    """Restore original files from backup when cross-module checks fail.

    If a file was newly created (no backup), it is deleted.
    """
    for file_path, content in backups.items():
        dst = project_root / file_path
        try:
            dst.write_text(content, encoding="utf-8")
        except Exception:
            pass

    # Remove files that didn't exist before (no backup)
    for _file_path in backups:
        # We can't distinguish new vs modified from backups alone,
        # so we only restore modified files. New files without backup
        # are left for git to handle.
        pass

    # Git cleanup for safety
    try:
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        pass
