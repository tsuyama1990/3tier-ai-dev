"""IntegratorAgent – copies changed files from sandbox repo to project root."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Tuple


def integrate_changes(project_root: Path, sandbox_path: Path | None = None) -> Tuple[bool, str]:
    """Identify modified/new files in sandbox repo and copy them back to project_root."""
    root = Path(project_root).resolve()
    sandbox_repo = Path(sandbox_path) / "repo" if sandbox_path else root / "repo"

    if not sandbox_repo.exists():
        # Fallback to search if sandbox path is omitted
        sandbox_repo = root / "repo"
        if not sandbox_repo.exists():
            return False, "Sandbox repository directory not found"

    try:
        # Detect changed files relative to HEAD
        res_mod = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(sandbox_repo),
            capture_output=True,
            text=True,
            check=False,
        )
        # Detect newly created files
        res_new = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(sandbox_repo),
            capture_output=True,
            text=True,
            check=False,
        )

        files = set()
        if res_mod.returncode == 0:
            for f in res_mod.stdout.splitlines():
                if f.strip():
                    files.add(f.strip())
        if res_new.returncode == 0:
            for f in res_new.stdout.splitlines():
                if f.strip():
                    files.add(f.strip())

        if not files:
            return True, "No changes detected to integrate"

        if len(files) > 3:
            return False, f"Integration rejected: changes affect {len(files)} files, exceeding the limit of 3 files"

        # Copy each file back to project root
        for file_path in files:
            src = sandbox_repo / file_path
            dst = root / file_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        return True, f"Successfully integrated {len(files)} files"
    except Exception as e:
        return False, f"Integration failed: {e}"
