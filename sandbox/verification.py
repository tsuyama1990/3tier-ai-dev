"""Verification helpers that run linting / type‑checking inside a sandbox.

These helpers are thin wrappers around the existing ``run_ruff`` and ``run_mypy``
functions defined in :pymod:`orchestrator`. They accept a path to a sandbox
workspace, temporarily change the working directory, invoke the checks and then
restore the original cwd.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple, Callable

from orchestrator import run_ruff, run_mypy


def _run_in_workspace(workspace: Path, func: Callable[[], Tuple[bool, str]]) -> Tuple[bool, str]:
    """Execute *func* with ``cwd`` set to *workspace*.

    The original working directory is restored afterwards, even if *func*
    raises. ``func`` must be a callable that returns ``Tuple[bool, str]``.
    """
    original_cwd = Path.cwd()
    try:
        os.chdir(workspace)
        return func()
    finally:
        os.chdir(original_cwd)


def verify_workspace(workspace: Path) -> Tuple[bool, str]:
    """Run Ruff and MyPy inside *workspace* and return a combined result.

    The function short‑circuits on the first failure, returning ``False`` and
    the corresponding output. If both checks succeed, ``True`` and a concatenated
    success message are returned.
    """
    success, out = _run_in_workspace(workspace, run_ruff)
    if not success:
        return False, f"Ruff failed:\n{out}"
    success, out_mypy = _run_in_workspace(workspace, run_mypy)
    if not success:
        return False, f"Mypy failed:\n{out_mypy}"
    return True, "Ruff and MyPy passed in sandbox."
