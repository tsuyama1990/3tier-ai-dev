"""Unit tests for the sandbox package introduced for the Safe Factory pattern.

The tests cover the core public API of each component:

* ``SandboxWorkspace`` – creation of an isolated temporary directory and file
  copying respecting ``sandbox.constraints.is_path_allowed``.
* ``cloner.clone_into`` – cloning (or fallback copying) of the current repository
  into a sandbox workspace. The test monkey‑patches ``subprocess.run`` to simulate
  a successful shallow clone.
* ``integrator.integrate_changes`` – detection of changed files and safe copy
  back into the original project.
* ``config_agent.backup_config`` / ``restore_config`` – backup and restore of a
  configuration file.
* ``scoped_lint.run_scoped_linters`` – execution of Ruff and MyPy on the set of
  changed files only. The underlying ``orchestrator.run_ruff`` and ``run_mypy``
  functions are monkey‑patched to avoid external dependencies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import sandbox.workspace as workspace
import sandbox.constraints as constraints
import sandbox.verification as verification
import sandbox.cloner as cloner
import sandbox.integrator as integrator
import sandbox.config_agent as config_agent
import sandbox.scoped_lint as scoped_lint


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    """Create a minimal project structure used by multiple tests.

    The layout includes a regular source file, a ``.venv`` directory (which should
    be excluded by the constraints) and a configuration file that the
    ``ConfigAgent`` will manipulate.
    """
    # regular source file
    (tmp_path / "app.py").write_text("print('hello')", encoding="utf-8")
    # virtual‑env placeholder – should be ignored
    venv_dir = tmp_path / ".venv" / "bin"
    venv_dir.mkdir(parents=True)
    (venv_dir / "python").write_text("# dummy", encoding="utf-8")
    # config file – simple TOML content; the exact values are irrelevant for the test.
    (tmp_path / "pyproject.toml").write_text("[tool]\nvalue = 1", encoding="utf-8")
    return tmp_path


def test_workspace_copies_allowed_files(sample_project: Path) -> None:
    # The workspace should copy ``app.py`` but skip the ``.venv`` directory.
    with workspace.SandboxWorkspace(root=sample_project) as ws_path:
        # ``app.py`` must exist in the sandbox
        assert (ws_path / "app.py").exists()
        # ``.venv`` must not be copied
        assert not (ws_path / ".venv").exists()
        # The ``path`` property is only valid after entering the context.
        ws = workspace.SandboxWorkspace(root=sample_project)
        with ws as ws_path:
            assert ws.path == ws_path


def test_config_agent_backup_and_restore(tmp_path: Path) -> None:
    cfg = tmp_path / "settings.cfg"
    cfg.write_text("original", encoding="utf-8")
    ok, backup_path = config_agent.backup_config(cfg)
    assert ok
    assert Path(backup_path).exists()
    # Modify original and restore
    cfg.write_text("modified", encoding="utf-8")
    ok, msg = config_agent.restore_config(cfg)
    assert ok
    assert cfg.read_text(encoding="utf-8") == "original"
    # Backup should be removed after restore
    assert not Path(backup_path).exists()


def test_cloner_fallback_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Simulate a repository root that is just ``tmp_path``.
    # Force ``git rev-parse`` to raise so the fallback path is exercised.
    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:  # noqa: D401
        # Return non‑zero for any git command.
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="git error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Create a dummy file that should be copied.
    (tmp_path / "dummy.txt").write_text("data", encoding="utf-8")
    # Use the cloner to copy into a new workspace.
    with workspace.SandboxWorkspace(root=tmp_path) as ws_path:
        success, msg = cloner.clone_into(ws_path)
        assert success, msg
        # The file should now exist under ``repo`` inside the workspace.
        assert (ws_path / "repo" / "dummy.txt").exists()


def test_integrator_applies_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Set up a sandbox workspace with a ``repo`` subdirectory.
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    # Original file in the project root.
    original = tmp_path / "data.txt"
    original.write_text("v1", encoding="utf-8")
    # Modified version inside the sandbox repo.
    (repo_dir / "data.txt").write_text("v2", encoding="utf-8")

    # Mock ``git diff --name-only`` to report the changed file.
    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        if args[0][:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="data.txt\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Run integration.
    success, msg = integrator.integrate_changes(tmp_path)
    assert success, msg
    # The original file should now contain the sandbox version.
    assert original.read_text(encoding="utf-8") == "v2"


def test_scoped_lint_delegates_to_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force ``git diff`` to return two python files.
    def fake_run_git(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        if args[0][:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="a.py\nb.py\n", stderr="")
        # For ruff/mypy invocations we simply return success.
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_git)

    # Monkey‑patch orchestrator functions to verify they are called.
    called = {"ruff": False, "mypy": False}

    def fake_ruff(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        called["ruff"] = True
        return True, "ruff ok"

    def fake_mypy(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        called["mypy"] = True
        return True, "mypy ok"

    import orchestrator

    monkeypatch.setattr(orchestrator, "run_ruff", fake_ruff)
    monkeypatch.setattr(orchestrator, "run_mypy", fake_mypy)

    success, msg = scoped_lint.run_scoped_linters()
    assert success
    assert called["ruff"] and called["mypy"]
    assert "Ruff and MyPy passed" in msg
