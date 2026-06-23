"""Utility functions that define what may be copied into a sandbox.

The *Safe Factory* principle requires that potentially dangerous files – for
example, global configuration files that could be overwritten – are excluded
from the sandbox. The rules are intentionally simple but can be extended later.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Global allow‑list / deny‑list definitions
# ---------------------------------------------------------------------------
_DENY_DIRS = {".venv", "__pycache__", ".git", "tests/generated"}
_DENY_FILES = {"pyproject.toml", "mcp_config.json", "README.md"}


def is_path_allowed(path: Path, project_root: Path) -> bool:
    """Return ``True`` if *path* is safe to copy into the sandbox.

    The function checks the following:

    * The path must be inside *project_root*.
    * It must not reside in a denied directory (e.g., virtual‑env folders).
    * Files listed in ``_DENY_FILES`` are excluded – they are considered
      configuration that should stay outside the sandbox.
    * All other files are allowed.
    """
    try:
        rel = path.resolve().relative_to(project_root.resolve())
    except Exception:
        return False

    if any(part in _DENY_DIRS for part in rel.parts):
        return False
    if rel.name in _DENY_FILES:
        return False
    return True
