"""SandboxWorkspace – isolated working directory for Safe Factory.

The workspace is created in a temporary directory (via :pyfunc:`tempfile.mkdtemp`).
Only a whitelist of files is copied to avoid leaking sensitive configuration
or global state. The class implements the context‑manager protocol so callers can
use ``with SandboxWorkspace() as ws:`` and be guaranteed that the temporary
directory is removed afterwards.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

from .constraints import is_path_allowed


class SandboxWorkspace:
    """Create an isolated copy of the project for safe execution.

    Parameters
    ----------
    root: :class:`Path`
        The root of the original project. Defaults to the current working
        directory.
    whitelist: Iterable[Path] | None
        Optional explicit whitelist of paths to copy. If ``None`` a default set
        is derived (source files, ``pyproject.toml`` and ``README.*``). Paths are
        resolved relative to *root*.
    """

    def __init__(self, root: Path | None = None, whitelist: Iterable[Path] | None = None):
        self.root = Path(root or Path.cwd()).resolve()
        self._temp_dir: Path | None = None
        self.whitelist: list[Path] = []
        if whitelist is None:
            # Default whitelist – copy everything that is not explicitly excluded
            # by :func:`sandbox.constraints.is_path_allowed`.
            for p in self.root.rglob("*"):
                if p.is_file() and is_path_allowed(p, self.root):
                    self.whitelist.append(p.relative_to(self.root))
        else:
            self.whitelist = [Path(p).relative_to(self.root) for p in whitelist]

    # ---------------------------------------------------------------------
    # Context manager protocol
    # ---------------------------------------------------------------------
    def __enter__(self) -> Path:
        self._temp_dir = Path(tempfile.mkdtemp(prefix="safe_factory_"))
        self._populate()
        return self._temp_dir

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object | None
    ) -> None:
        """Remove the temporary directory regardless of success or failure."""
        if self._temp_dir and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------
    def _populate(self) -> None:
        """Copy whitelisted files into the temporary workspace.

        The directory structure is preserved. Files that already exist in the
        destination are overwritten – this is safe because the destination is a
        freshly created temporary directory.
        """
        assert self._temp_dir is not None, "Workspace not initialised"
        for rel_path in self.whitelist:
            src = self.root / rel_path
            dst = self._temp_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    @property
    def path(self) -> Path:
        """Return the path to the temporary workspace.

        Raises
        ------
        RuntimeError
            If the workspace has not been entered yet.
        """
        if self._temp_dir is None:
            raise RuntimeError("SandboxWorkspace has not been entered – use 'with' statement.")
        return self._temp_dir
