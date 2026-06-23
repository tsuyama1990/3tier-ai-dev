"""ClonerAgent – copies files into isolated sandbox workspace."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .constraints import is_path_allowed


def clone_into(sandbox_path: Path, project_root: Path | None = None) -> tuple[bool, str]:
    """Clone or copy allowed files from project root into sandbox_path/repo."""
    dst_repo = sandbox_path / "repo"
    dst_repo.mkdir(parents=True, exist_ok=True)

    # Determine source for fallback copying
    src_dir = Path(project_root).resolve() if project_root else sandbox_path

    # Try shallow clone first if it's a git repo and project_root was provided
    if project_root:
        try:
            res = subprocess.run(
                ["git", "clone", "--depth", "1", f"file://{src_dir}", str(dst_repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                return True, "Shallow clone succeeded"
        except Exception:
            pass

    # Fallback to manual copying of allowed files
    try:
        for p in src_dir.rglob("*"):
            if p.is_file() and is_path_allowed(p, src_dir):
                # Avoid copying anything inside the destination repo directory itself
                if "repo" in p.relative_to(src_dir).parts:
                    continue
                rel = p.relative_to(src_dir)
                dst = dst_repo / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
        return True, "Fallback copy succeeded"
    except Exception as e:
        return False, f"Cloning and copy fallback failed: {e}"
