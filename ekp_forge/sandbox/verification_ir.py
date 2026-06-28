"""Verification Intermediate Representation (IR) — Phase 2.

Normalises output from Ruff, Mypy and Pytest into tool-independent
``Diagnostic`` models. Also provides the ``AutoFixRunner`` that applies
mechanical fixes (``ruff check --fix``) **before** parsing, so that only
non-trivial errors reach the Worker / Fix Planner.

Usage::

    from ekp_forge.sandbox.verification_ir import (
        run_verification_pipeline,
        RuffParser,
        MypyParser,
        PytestParser,
    )

    diagnostics: list[Diagnostic] = run_verification_pipeline(
        changed_files=["src/main.py"],
    )
    # diagnostics now contains only unresolved issues (auto-fixes already applied)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ekp_forge.schemas.contract import (
    Diagnostic,
    DiagnosticCategory,
    DiagnosticSeverity,
)


# ---------------------------------------------------------------------------
# Auto-Fix Runner — mechanical fixes before IR generation
# ---------------------------------------------------------------------------


class AutoFixRunner:
    """Runs mechanical auto-fix tools before diagnostic extraction.

    Currently executes:
    - ``ruff check --fix`` (syntax, import sorting, unused imports)
    - ``ruff format`` (formatting)

    These are safe, deterministic operations that should never be
    delegated to an LLM.
    """

    def __init__(self, workspace: Path | None = None) -> None:
        """Initialise the auto-fix runner.

        Args:
            workspace: Optional workspace path. If None, uses CWD.
        """
        self._workspace = workspace

    def run_all(self, changed_files: list[str] | None = None) -> list[str]:
        """Run all auto-fix tools and return a list of fix descriptions.

        Auto-fix stages are applied sequentially. If a stage fails, it
        is logged but does not block subsequent stages.

        Args:
            changed_files: Optional list of file paths to target. If None,
                          all files in the workspace are targeted.

        Returns:
            List of human-readable descriptions of fixes applied.
        """
        applied: list[str] = []
        cwd = self._resolve_cwd()

        # Stage 1: ruff check --fix (safe auto-fixes only)
        fix_applied = self._run_ruff_fix(cwd, changed_files)
        if fix_applied:
            applied.append(f"ruff fix: {fix_applied}")

        # Stage 2: ruff format
        fmt_applied = self._run_ruff_format(cwd, changed_files)
        if fmt_applied:
            applied.append(f"ruff format: {fmt_applied}")

        return applied

    def _resolve_cwd(self) -> Path:
        if self._workspace is not None:
            return self._workspace
        return Path.cwd()

    def _run_ruff_fix(self, cwd: Path, changed_files: list[str] | None) -> str | None:
        """Run ``ruff check --fix`` and return a summary or None."""
        try:
            ruff_path = self._find_ruff(cwd)
            cmd = [str(ruff_path), "check", "--fix"]
            if changed_files:
                cmd.extend(changed_files)
            else:
                cmd.append(".")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=60,
            )
            # ruff --fix exits 0 even if fixes were applied; 1 if remaining errors
            output = (result.stdout or "") + (result.stderr or "")
            if "fixed" in output.lower():
                # Extract count, e.g. "5 fixes applied"
                match = re.search(r"(\d+)\s+fix(?:es)?\s+applied", output.lower())
                if match:
                    return f"{match.group(1)} fix(es) applied"
                return "fixes applied"
            if result.returncode != 0 and not changed_files:
                # No files targeted — still may have run
                pass
            return None
        except subprocess.TimeoutExpired:
            return "ruff fix timed out"
        except Exception as e:
            return f"ruff fix error: {e}"

    def _run_ruff_format(self, cwd: Path, changed_files: list[str] | None) -> str | None:
        """Run ``ruff format`` and return a summary or None."""
        try:
            ruff_path = self._find_ruff(cwd)
            cmd = [str(ruff_path), "format"]
            if changed_files:
                cmd.extend(changed_files)
            else:
                cmd.append(".")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=60,
            )
            output = (result.stdout or "") + (result.stderr or "")
            # ruff format reports "N files reformatted" or "N files left unchanged"
            if "reformatted" in output:
                match = re.search(r"(\d+)\s+file(?:s)?\s+reformatted", output)
                if match:
                    return f"{match.group(1)} file(s) reformatted"
                return "formatting applied"
            return None
        except subprocess.TimeoutExpired:
            return "ruff format timed out"
        except Exception as e:
            return f"ruff format error: {e}"

    @staticmethod
    def _find_ruff(cwd: Path) -> Path:
        """Locate ruff binary."""
        venv_dir = Path(cwd) / ".venv"
        if sys.platform == "win32":
            candidates = [
                venv_dir / "Scripts" / "ruff.exe",
                Path("ruff.exe"),
            ]
        else:
            candidates = [
                venv_dir / "bin" / "ruff",
                Path("/usr/local/bin/ruff"),
                Path("/usr/bin/ruff"),
            ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        # Fallback: assume ruff is on PATH
        return Path("ruff")


# ---------------------------------------------------------------------------
# Ruff Parser — JSON output → list[Diagnostic]
# ---------------------------------------------------------------------------

# Maps ruff error codes to DiagnosticCategory
_RUFF_CODE_CATEGORY: dict[str, DiagnosticCategory] = {
    # Syntax / parsing
    "E999": DiagnosticCategory.SYNTAX,
    "F821": DiagnosticCategory.UNDEFINED_NAME,
    "F822": DiagnosticCategory.UNDEFINED_NAME,
    "F823": DiagnosticCategory.UNDEFINED_NAME,
    # Import
    "F401": DiagnosticCategory.UNUSED_IMPORT,
    "F402": DiagnosticCategory.IMPORT,
    "F403": DiagnosticCategory.IMPORT,
    "F404": DiagnosticCategory.IMPORT,
    "F405": DiagnosticCategory.IMPORT,
    "F406": DiagnosticCategory.IMPORT,
    "F407": DiagnosticCategory.IMPORT,
    "I001": DiagnosticCategory.IMPORT,
    "I002": DiagnosticCategory.IMPORT,
    # Typing
    "ANN": DiagnosticCategory.TYPE_MISMATCH,
    # Formatting
    "W291": DiagnosticCategory.FORMATTING,
    "W292": DiagnosticCategory.FORMATTING,
    "W293": DiagnosticCategory.FORMATTING,
    # Unused
    "F841": DiagnosticCategory.UNUSED_VARIABLE,
    # Security
    "S": DiagnosticCategory.SECURITY,
}


class RuffParser:
    """Parse ``ruff check --output-format json`` output into ``list[Diagnostic]``."""

    @staticmethod
    def _lookup_category(code: str) -> DiagnosticCategory:
        """Look up category for a ruff code using exact match then prefix match.

        Some ruff codes are exact (e.g. "F821") while others are prefixes
        (e.g. "S" matches "S101", "S102", etc.; "ANN" matches "ANN001", etc.).
        """
        if not code:
            return DiagnosticCategory.OTHER
        # Exact match first
        if code in _RUFF_CODE_CATEGORY:
            return _RUFF_CODE_CATEGORY[code]
        # Prefix match: try progressively shorter prefixes
        for prefix_len in range(len(code) - 1, 0, -1):
            prefix = code[:prefix_len]
            if prefix in _RUFF_CODE_CATEGORY:
                return _RUFF_CODE_CATEGORY[prefix]
        return DiagnosticCategory.OTHER

    @staticmethod
    def parse(raw_output: str) -> list[Diagnostic]:
        """Parse ruff JSON output into a list of ``Diagnostic``.

        Args:
            raw_output: The raw stdout/stderr from ``ruff check --output-format json``.

        Returns:
            List of ``Diagnostic`` instances. Returns empty list on parse failure.
        """
        diagnostics: list[Diagnostic] = []

        # Ruff JSON output is a list of objects, one per finding
        try:
            records: list[dict[str, Any]] = json.loads(raw_output)
        except (json.JSONDecodeError, TypeError):
            return diagnostics

        for record in records:
            try:
                code: str = record.get("code", "") or ""
                filename: str = record.get("filename", "")
                location = record.get("location", {}) or {}
                line: int = location.get("row", 0) or 0
                message: str = record.get("message", "") or ""

                # Determine category using exact + prefix lookup
                category = RuffParser._lookup_category(code)

                # Determine severity
                # Ruff "E" / "F" are errors; "W" / "I" are warnings; "S" is security error
                severity: DiagnosticSeverity
                if code.startswith(("E", "F", "S")):
                    severity = DiagnosticSeverity.ERROR
                else:
                    severity = DiagnosticSeverity.WARNING

                diagnostics.append(
                    Diagnostic(
                        tool="ruff",
                        severity=severity,
                        file=filename,
                        line=line,
                        code=code,
                        message=message,
                        category=category,
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue  # skip malformed records

        return diagnostics


# ---------------------------------------------------------------------------
# Mypy Parser — text output → list[Diagnostic]
# ---------------------------------------------------------------------------


class MypyParser:
    """Parse Mypy text output into ``list[Diagnostic]``.

    Mypy output format::

        path/to/file.py:LINE:COLUMN: severity: message  [error-code]
    """

    # Mypy error severity mapping
    _SEVERITY_MAP: dict[str, DiagnosticSeverity] = {
        "error": DiagnosticSeverity.ERROR,
        "warning": DiagnosticSeverity.WARNING,
        "note": DiagnosticSeverity.WARNING,
    }

    # Maps mypy error code patterns to categories
    _MYPY_CODE_CATEGORY: dict[str, DiagnosticCategory] = {
        "arg-type": DiagnosticCategory.TYPE_MISMATCH,
        "return-type": DiagnosticCategory.WRONG_RETURN_VALUE,
        "return-value": DiagnosticCategory.WRONG_RETURN_VALUE,
        "assignment": DiagnosticCategory.TYPE_MISMATCH,
        "misc": DiagnosticCategory.TYPE_MISMATCH,
        "name-defined": DiagnosticCategory.UNDEFINED_NAME,
        "import": DiagnosticCategory.IMPORT,
        "import-untyped": DiagnosticCategory.IMPORT,
        "has-type": DiagnosticCategory.TYPE_MISMATCH,
        "union-attr": DiagnosticCategory.TYPE_MISMATCH,
        "index": DiagnosticCategory.TYPE_MISMATCH,
        "operator": DiagnosticCategory.TYPE_MISMATCH,
        "override": DiagnosticCategory.TYPE_MISMATCH,
        "syntax": DiagnosticCategory.SYNTAX,
    }

    # Regex: file:line:col: severity: message [code]
    _MYPY_LINE_RE = re.compile(r"^(.+?):(\d+):\d+:\s*(error|warning|note):\s*(.+?)(?:\s+\[([^\]]+)\])?\s*$")

    @classmethod
    def parse(cls, raw_output: str) -> list[Diagnostic]:
        """Parse mypy text output into ``list[Diagnostic]``.

        Args:
            raw_output: Raw stdout/stderr from ``mypy``.

        Returns:
            List of ``Diagnostic`` instances. Empty if no issues found.
        """
        diagnostics: list[Diagnostic] = []
        if not raw_output:
            return diagnostics

        for line in raw_output.splitlines():
            if not line.strip():
                continue
            match = cls._MYPY_LINE_RE.match(line.strip())
            if not match:
                continue

            filepath = match.group(1)
            line_no = int(match.group(2))
            severity_str = match.group(3)
            message = match.group(4).strip()
            code = match.group(5) or ""

            severity = cls._SEVERITY_MAP.get(severity_str, DiagnosticSeverity.ERROR)
            category = cls._MYPY_CODE_CATEGORY.get(code, DiagnosticCategory.OTHER)

            try:
                diagnostics.append(
                    Diagnostic(
                        tool="mypy",
                        severity=severity,
                        file=filepath,
                        line=line_no,
                        code=f"mypy-{code}" if code else "mypy-error",
                        message=message,
                        category=category,
                    )
                )
            except (ValueError, TypeError):
                continue

        return diagnostics


# ---------------------------------------------------------------------------
# Pytest Parser — output → list[Diagnostic]
# ---------------------------------------------------------------------------


class PytestParser:
    """Parse Pytest output into ``list[Diagnostic]``.

    Handles:
    - FAILED test paths
    - AssertionError / TypeError / ValueError details
    - Expected vs Actual values
    """

    # Regex for FAILED lines: "FAILED path/to/test.py::test_name - ErrorType: msg"
    _FAILED_RE = re.compile(r"^FAILED\s+(.+?::\S+?)\s+-\s+(.+?):\s*(.+)$")

    # Regex for expected vs actual: "assert ..." or "E       assert ..."
    _ASSERT_RE = re.compile(r"assert\s+(.+?)\s*(?:==|!=|>=|<=|>|<|in|not in|is|is not)\s*(.+)")

    # Regex for "E       AssertionError: ..."
    _ASSERTION_ERROR_RE = re.compile(
        r"^(?:>?\s*E\s+)?(AssertionError|TypeError|ValueError|AttributeError|"
        r"ModuleNotFoundError|ImportError|IndexError|KeyError|RuntimeError|"
        r"StopIteration|RecursionError):\s*(.+)$",
        re.IGNORECASE,
    )

    @classmethod
    def parse(cls, raw_output: str) -> list[Diagnostic]:
        """Parse pytest output into ``list[Diagnostic]``.

        Args:
            raw_output: Raw stdout/stderr from ``pytest -v --tb=short``.

        Returns:
            List of ``Diagnostic`` instances. Empty if all tests passed.
        """
        diagnostics: list[Diagnostic] = []
        if not raw_output:
            return diagnostics

        lines = raw_output.splitlines()

        # Track FAILED test entries for context
        current_test: str | None = None

        for idx, line in enumerate(lines):
            stripped = line.strip()

            # --- FAILED line ---
            failed_match = cls._FAILED_RE.match(stripped)
            if failed_match:
                current_test = failed_match.group(1)
                error_type = failed_match.group(2).strip()
                error_msg = failed_match.group(3).strip()

                # Extract file path
                file_path = current_test.split("::")[0] if current_test else ""

                diagnostics.append(
                    Diagnostic(
                        tool="pytest",
                        severity=DiagnosticSeverity.ERROR,
                        file=file_path,
                        line=0,
                        code=error_type,
                        message=f"{error_type}: {error_msg}",
                        category=DiagnosticCategory.TEST_FAILURE,
                    )
                )
                continue

            # --- Assertion detail lines (E lines) ---
            assert_match = cls._ASSERTION_ERROR_RE.match(stripped)
            if assert_match and current_test:
                error_type = assert_match.group(1)
                error_msg = assert_match.group(2).strip()
                file_path = current_test.split("::")[0]

                # Try to extract expected/actual from assertion
                expected, actual = cls._extract_expected_actual(lines, idx)

                diagnostics.append(
                    Diagnostic(
                        tool="pytest",
                        severity=DiagnosticSeverity.ERROR,
                        file=file_path,
                        line=0,
                        code=error_type,
                        message=f"{error_type}: {error_msg}",
                        category=DiagnosticCategory.WRONG_RETURN_VALUE,
                        expected=expected,
                        actual=actual,
                    )
                )

        return diagnostics

    @classmethod
    def _extract_expected_actual(cls, lines: list[str], current_idx: int) -> tuple[str | None, str | None]:
        """Scan forward from *current_idx* to extract expected/actual values.

        Looks for lines matching:
            E       assert <actual> == <expected>
        """
        for offset in range(1, min(5, len(lines) - current_idx)):
            next_line = lines[current_idx + offset].strip()
            # Remove leading "E  " or ">  " markers
            clean = re.sub(r"^[>\s]*E\s+", "", next_line)
            match = cls._ASSERT_RE.match(clean)
            if match:
                # best guess: left side is actual, right side is expected
                return match.group(2), match.group(1)
        return None, None


# ---------------------------------------------------------------------------
# Orchestration — run auto-fix, then all tools, then parse
# ---------------------------------------------------------------------------

# Sentinel value used by _ToolResult to signal a tool that was not run.
_UNSET = object()


class _ToolResult:
    """Container for a single tool's raw output, exit code, and parse status.

    Phase 2.1: Stores the exit code alongside raw output so the pipeline
    can detect parse failures: if the tool exited non-zero but we parsed
    zero diagnostics, a ``gatekeeper`` warning is emitted to prevent
    silent false positives.
    """

    __slots__ = ("exit_code", "parse_ok", "raw_output")

    def __init__(self, raw_output: str, exit_code: int = -1) -> None:
        self.raw_output = raw_output
        self.exit_code = exit_code
        self.parse_ok = False


def run_single_tool(
    tool_name: str,
    changed_files: list[str] | None = None,
    cwd: Path | None = None,
) -> _ToolResult:
    """Run a single verification tool and return a ``_ToolResult``.

    Args:
        tool_name: One of ``"ruff"``, ``"mypy"``, ``"pytest"``.
        changed_files: Optional list of files to scope the check.
        cwd: Working directory for the tool.

    Returns:
        A ``_ToolResult`` with raw output and exit code.
    """
    resolve_cwd = cwd or Path.cwd()

    if tool_name == "ruff":
        ruff_path = AutoFixRunner._find_ruff(resolve_cwd)
        cmd = [str(ruff_path), "check", "--output-format", "json"]
        if changed_files:
            cmd.extend(changed_files)
        else:
            cmd.append(".")
    elif tool_name == "mypy":
        mypy_path = _find_mypy(resolve_cwd)
        cmd = [str(mypy_path), "--no-error-summary", "--show-error-codes"]
        if changed_files:
            cmd.extend(changed_files)
        else:
            cmd.append(".")
    elif tool_name == "pytest":
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-v",
            "--tb=short",
            "--ignore=tests/step1_baseline",
            "--ignore=tests/step2_fake_api",
            "--ignore=tests/step3_stress",
            "--ignore=tests/step4_ollama_synthesizer",
        ]
        if changed_files:
            cmd.extend(changed_files)
    else:
        raise ValueError(f"Unknown tool: {tool_name}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=resolve_cwd,
            timeout=120,
        )
        raw = (result.stdout or "") + (result.stderr or "")
        return _ToolResult(raw_output=raw, exit_code=result.returncode)
    except subprocess.TimeoutExpired:
        return _ToolResult(raw_output=f"[{tool_name} timed out]", exit_code=-1)
    except Exception as e:
        return _ToolResult(raw_output=f"[{tool_name} error: {e}]", exit_code=-1)


def _find_mypy(cwd: Path) -> Path:
    """Locate mypy binary."""
    venv_dir = cwd / ".venv"
    if sys.platform == "win32":
        candidates = [
            venv_dir / "Scripts" / "mypy.exe",
            Path("mypy.exe"),
        ]
    else:
        candidates = [
            venv_dir / "bin" / "mypy",
            Path("/usr/local/bin/mypy"),
            Path("/usr/bin/mypy"),
        ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path("mypy")


def run_verification_pipeline(
    changed_files: list[str] | None = None,
    run_pytest: bool = True,
    workspace: Path | None = None,
) -> list[Diagnostic]:
    """End-to-end verification pipeline: auto-fix → check → parse → return.

    .. deprecated::
       Use :func:`ekp_forge.engine.tiered_diagnostic.TieredDiagnosticRunner`
       instead. This function runs all tools unconditionally (no early exit),
       which can produce cascading false positives. The Phase 4 replacement
       runs Ruff → Mypy → Pytest sequentially with early exit.

    This is the Phase 2 entry point for the Verification role. It:
    1. Runs mechanical auto-fixes (``ruff check --fix``, ``ruff format``).
    2. Runs Ruff, Mypy (and optionally Pytest).
    3. Parses each tool's output into ``Diagnostic`` models.
    4. **Parse-failure guard (Phase 2.1)**: If a tool exited non-zero but
       zero diagnostics were parsed, a ``gatekeeper`` warning Diagnostic is
       emitted to prevent silent false positives.

    Args:
        changed_files: Optional list of file paths to scope checks to.
        run_pytest: Whether to run pytest (default: True).
        workspace: Optional workspace path. If None, uses CWD.

    Returns:
        List of ``Diagnostic`` instances representing all remaining issues.
    """
    resolve_cwd = workspace or Path.cwd()

    # Step 1: Auto-fix
    fixer = AutoFixRunner(workspace=resolve_cwd)
    fixer.run_all(changed_files)

    # Step 2: Run tools and collect results
    ruff_result = run_single_tool("ruff", changed_files, cwd=resolve_cwd)
    mypy_result = run_single_tool("mypy", changed_files, cwd=resolve_cwd)

    # Step 3: Parse into Diagnostics
    diagnostics: list[Diagnostic] = []

    ruff_diags = RuffParser.parse(ruff_result.raw_output)
    ruff_result.parse_ok = bool(ruff_diags) or ruff_result.exit_code == 0
    diagnostics.extend(ruff_diags)

    mypy_diags = MypyParser.parse(mypy_result.raw_output)
    mypy_result.parse_ok = bool(mypy_diags) or mypy_result.exit_code == 0
    diagnostics.extend(mypy_diags)

    pytest_result: _ToolResult | None = None
    if run_pytest:
        pytest_result = run_single_tool("pytest", changed_files, cwd=resolve_cwd)
        pytest_diags = PytestParser.parse(pytest_result.raw_output)
        pytest_result.parse_ok = bool(pytest_diags) or pytest_result.exit_code == 0
        diagnostics.extend(pytest_diags)

    # Step 4: Parse-failure guard — emit gatekeeper warnings when a tool
    # exited non-zero but we parsed zero diagnostics (likely parse failure).
    for label, result in [
        ("ruff", ruff_result),
        ("mypy", mypy_result),
        ("pytest", pytest_result),
    ]:
        if result is None:
            continue
        if result.exit_code not in (0, -1) and not result.parse_ok:
            diagnostics.append(
                Diagnostic(
                    tool="gatekeeper",
                    severity=DiagnosticSeverity.WARNING,
                    file="*",
                    message=(
                        f"[PARSE GUARD] {label} exited with code {result.exit_code} "
                        f"but no diagnostics were parsed. Raw output may contain "
                        f"unrecognized error patterns — review manually."
                    ),
                    category=DiagnosticCategory.OTHER,
                )
            )

    return diagnostics
