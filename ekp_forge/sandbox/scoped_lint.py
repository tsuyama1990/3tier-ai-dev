"""Run Ruff and MyPy only on files that have changed in the current git repo.

The helpers are thin wrappers around the existing ``run_ruff`` and ``run_mypy``
functions from :pymod:`orchestrator`. They compute the list of changed files via
``git diff --name-only`` and invoke the linters with the appropriate arguments
to limit their scope.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ekp_forge import orchestrator


def _changed_files() -> list[Path]:
    """Return a list of Python files changed relative to HEAD.

    If the git command fails, an empty list is returned, causing the linters to
    run on the whole project (fallback behaviour).
    """
    result = subprocess.run(
        ["git", "diff", "--name-only"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [Path(p) for p in result.stdout.splitlines() if p.endswith(".py")]


def run_scoped_linters() -> tuple[bool, str]:
    """Execute Ruff and MyPy on the set of changed Python files only.

    Returns a combined success flag and concatenated output.
    """
    files = _changed_files()
    file_strs = [str(f) for f in files] if files else None

    success, out = orchestrator.run_ruff(file_strs)
    if not success:
        return False, f"Ruff failed on changed files:\n{out}"
    success, out_mypy = orchestrator.run_mypy(file_strs)
    if not success:
        return False, f"Mypy failed on changed files:\n{out_mypy}"
    return True, "Ruff and MyPy passed on changed files."
