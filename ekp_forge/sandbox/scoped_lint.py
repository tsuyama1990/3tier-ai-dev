"""Utilities for detecting changed files via git diff.

Phase 4 note: ``run_scoped_linters()`` was removed in Phase 4 as it was
dead code (no callers). Only ``_changed_files()`` is retained for use by
the Worker's verification pipeline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _changed_files() -> list[Path]:
    """Return a list of Python files changed relative to HEAD.

    Includes:
    - Modified tracked files (``git diff --name-only``)
    - Staged files (``git diff --name-only --cached``)
    - Untracked Python files (``git ls-files --others --exclude-standard``)

    If all git commands fail, an empty list is returned, causing the linters to
    run on the whole project (fallback behaviour).
    """
    files: set[str] = set()

    # 1. Modified tracked files
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            files.update(p for p in result.stdout.splitlines() if p.endswith(".py"))
    except Exception:
        pass

    # 2. Staged files
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            files.update(p for p in result.stdout.splitlines() if p.endswith(".py"))
    except Exception:
        pass

    # 3. Untracked Python files (new files created by Aider)
    # Note: Do NOT use --exclude-standard as it respects .gitignore which may
    # exclude the test_output/ directory where generated files live.
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            files.update(p for p in result.stdout.splitlines() if p.endswith(".py"))
    except Exception:
        pass

    return [Path(p) for p in sorted(files)]
