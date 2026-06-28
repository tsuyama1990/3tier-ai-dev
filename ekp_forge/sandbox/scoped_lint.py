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
