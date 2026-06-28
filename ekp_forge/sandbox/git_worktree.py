"""GitWorktree – context manager for ``git worktree``-based isolated workspace.

Provides a lightweight, millisecond-fast alternative to ``git clone`` for
creating isolated working directories. The worktree uses **symbolic links**
to the main repository's object database, so no file copying occurs.

Usage::

    from ekp_forge.sandbox.git_worktree import GitWorktree

    with GitWorktree() as worktree_path:
        # worktree_path is a temporary directory with a valid git worktree
        # Changes here do NOT affect the main repository until committed.
        ...
    # On exit: worktree is removed, main repo is clean.

Edge Cases
----------
- **.git is a file, not a directory**: Inside a worktree, ``.git`` is a plain
  text file (``gitdir: /path/to/main/.git/worktrees/xxx``), not a ``.git/``
  directory. Use ``os.path.exists(".git")`` or ``git rev-parse --git-dir``
  instead of ``os.path.isdir(".git")``.
- **Detached HEAD**: Uses ``--detach`` to avoid polluting branch namespaces.
- **Cleanup guarantee**: ``__exit__`` runs ``git worktree remove --force``
  followed by ``shutil.rmtree`` on the temp directory.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class GitWorktreeError(RuntimeError):
    """Raised when ``git worktree`` operations fail."""


class GitWorktree:
    """Context manager for ``git worktree``-based isolated workspace.

    Parameters
    ----------
    repo_root : Path | None
        Path to the root of the git repository. Defaults to the current
        working directory.
    branch : str | None
        Branch name to check out in the worktree. If ``None`` (default),
        uses ``HEAD`` in detached mode.
    """

    def __init__(self, repo_root: Path | None = None, branch: str | None = None) -> None:
        self.repo_root = Path(repo_root or Path.cwd()).resolve()
        self._branch = branch
        self._temp_dir: Path | None = None

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> Path:
        """Create a ``git worktree`` and return its path.

        Returns
        -------
        Path
            Path to the temporary worktree directory.

        Raises
        ------
        GitWorktreeError
            If the current directory is not a git repository or if the
            ``git worktree add`` command fails.
        """
        # Verify that we are in a git repository
        self._check_git_repo()

        self._temp_dir = Path(tempfile.mkdtemp(prefix="git_worktree_"))
        branch_arg = self._branch or "HEAD"

        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(self._temp_dir), branch_arg],
            capture_output=True,
            text=True,
            cwd=self.repo_root,
        )

        if result.returncode != 0:
            # Clean up temp dir on failure
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
            raise GitWorktreeError(
                f"git worktree add failed (exit code {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

        return self._temp_dir

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        """Remove the worktree and temporary directory.

        Uses ``git worktree remove --force`` to handle uncommitted changes,
        then removes the temp directory with ``shutil.rmtree``.
        """
        if self._temp_dir is None:
            return

        # Step 1: Remove the git worktree reference (force to handle dirty state)
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(self._temp_dir)],
                capture_output=True,
                text=True,
                cwd=self.repo_root,
            )
        except Exception:
            pass  # Best-effort; shutil.rmtree handles the filesystem cleanup

        # Step 2: Remove the temporary directory itself
        try:
            if self._temp_dir.exists():
                shutil.rmtree(self._temp_dir, ignore_errors=True)
        except Exception:
            pass

        self._temp_dir = None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_git_dir(self) -> Path:
        """Resolve the actual ``.git`` directory path via ``git rev-parse --git-dir``.

        This is the reliable way to find the git metadata directory regardless
        of whether we are in a main repo or a worktree (where ``.git`` is a file).

        Returns
        -------
        Path
            Absolute path to the ``.git`` directory.

        Raises
        ------
        GitWorktreeError
            If ``git rev-parse --git-dir`` fails.
        """
        root = self._temp_dir if self._temp_dir else self.repo_root
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            cwd=root,
        )
        if result.returncode != 0:
            raise GitWorktreeError(f"git rev-parse --git-dir failed: {result.stderr.strip()}")
        return (root / result.stdout.strip()).resolve()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_git_repo(self) -> None:
        """Verify that ``repo_root`` is a valid git repository.

        Uses ``git rev-parse --git-dir`` for reliable detection (works for
        main repos, worktrees, and submodules).

        Raises
        ------
        GitWorktreeError
            If the directory is not a git repository.
        """
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            cwd=self.repo_root,
        )
        if result.returncode != 0:
            raise GitWorktreeError(
                f"Not a git repository: {self.repo_root}. GitWorktree requires an existing git repository."
            )

    @property
    def path(self) -> Path:
        """Return the worktree path.

        Raises
        ------
        RuntimeError
            If the worktree has not been entered yet.
        """
        if self._temp_dir is None:
            raise RuntimeError("GitWorktree has not been entered – use 'with' statement.")
        return self._temp_dir

    @property
    def is_active(self) -> bool:
        """Return ``True`` if the worktree has been created and not yet removed."""
        return self._temp_dir is not None
