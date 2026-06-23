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
from typing import Any

_BACKUP_SUFFIX = ".safe_factory.bak"


def backup_config(config_path: Path) -> tuple[bool, str]:
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


def restore_config(config_path: Path) -> tuple[bool, str]:
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


def serialize_toml(data: dict) -> str:
    """Recursive TOML serializer supporting dict values as tables."""
    import json

    def val_to_toml(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        return json.dumps(v)

    def serialize_section(d: dict, prefix: str = "") -> list[str]:
        lines = []
        # Write flat values first
        for k, v in sorted(d.items()):
            if not isinstance(v, dict):
                lines.append(f"{k} = {val_to_toml(v)}")
        # Write sub-tables
        for k, v in sorted(d.items()):
            if isinstance(v, dict):
                section_name = f"{prefix}.{k}" if prefix else k
                lines.append(f"\n[{section_name}]")
                lines.extend(serialize_section(v, section_name))
        return lines

    lines = []
    # Flat top-level values
    for k, v in sorted(data.items()):
        if not isinstance(v, dict):
            lines.append(f"{k} = {val_to_toml(v)}")
    # Top-level tables
    for k, v in sorted(data.items()):
        if isinstance(v, dict):
            lines.append(f"\n[{k}]")
            lines.extend(serialize_section(v, k))

    # Clean up empty lines / spacing
    output = []
    prev_was_empty = False
    for line in lines:
        if not line.strip():
            if not prev_was_empty:
                output.append("")
                prev_was_empty = True
        else:
            output.append(line)
            prev_was_empty = False

    return "\n".join(output)


def apply_config_changes(config_path: Path, requests: list[Any]) -> bool:
    """Apply config change requests to TOML at config_path.

    Uses tomllib to load and parse, applies updates recursively, and writes back using serialize_toml.
    """
    if not config_path.is_file():
        return False

    try:
        import sys
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib

        content = config_path.read_text(encoding="utf-8")
        data = tomllib.loads(content)

        # Apply each request
        for req in requests:
            key_path = req.key_path
            action = req.action
            val = req.value

            if not key_path:
                continue

            # Traverse to the target dictionary parent
            curr = data
            for part in key_path[:-1]:
                if part not in curr or not isinstance(curr[part], dict):
                    curr[part] = {}
                curr = curr[part]

            last_key = key_path[-1]

            if action == "set":
                curr[last_key] = val
            elif action == "append":
                if last_key not in curr:
                    curr[last_key] = []
                if isinstance(curr[last_key], list):
                    if isinstance(val, list):
                        curr[last_key].extend(val)
                    else:
                        curr[last_key].append(val)
            elif action == "remove":
                if last_key in curr:
                    if isinstance(curr[last_key], list) and val in curr[last_key]:
                        curr[last_key].remove(val)
                    else:
                        curr.pop(last_key, None)

        # Serialize back to TOML
        new_content = serialize_toml(data)
        config_path.write_text(new_content, encoding="utf-8")
        return True
    except Exception as exc:
        print(f"Error applying config changes: {exc}")
        return False

