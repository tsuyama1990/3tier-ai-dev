"""ConfigAgent – protect and manage configuration files during Safe Factory runs.

The agent provides two simple utilities:

* ``backup_config`` – copy a configuration file to a temporary backup location.
* ``restore_config`` – restore the backup, ensuring that the original file is not
  left in a modified state after a sandbox execution.

Both functions are deliberately lightweight and operate on absolute paths.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Tuple

_BACKUP_SUFFIX = ".safe_factory.bak"


def backup_config(config_path: Path) -> Tuple[bool, str]:
    """Create a backup of *config_path*.

    Returns ``(True, backup_path)`` on success. If the file does not exist, the
    function returns ``(False, "File not found")``.
    """
    if not config_path.is_file():
        return False, "File not found"
    backup_path = config_path.with_name(config_path.name + _BACKUP_SUFFIX)
    try:
        shutil.copy2(config_path, backup_path)
        return True, str(backup_path)
    except Exception as exc:
        return False, f"Backup failed: {exc}"


def restore_config(config_path: Path) -> Tuple[bool, str]:
    """Restore a previously backed‑up configuration file.

    The function looks for a file with the ``_BACKUP_SUFFIX``. If found, it
    overwrites the original and removes the backup. Returns ``(True, "restored")``
    on success, otherwise ``(False, reason)``.
    """
    backup_path = config_path.with_name(config_path.name + _BACKUP_SUFFIX)
    if not backup_path.is_file():
        return False, "Backup not found"
    try:
        shutil.copy2(backup_path, config_path)
        backup_path.unlink(missing_ok=True)
        return True, "restored"
    except Exception as exc:
        return False, f"Restore failed: {exc}"
