"""Structured hint generator — creates Reference objects based on diagnostic types.

Phase 4, Priority 4.

The Hint Generator creates precise, deterministic ``Reference`` objects
based on error type. **No LLM calls** — all hints are generated via
static analysis or sandboxed introspection.

Hint strategies by error type:

- **AttributeError / UndefinedName**: Uses ``IntrospectionTool`` to run
  ``dir()`` on the failing module in a sandboxed subprocess.
- **Type mismatch (mypy arg-type)**: Parses the file's AST to extract the
  function signature with type annotations.
- **Import error**: Scans the project environment for available modules.
- **Assertion error**: Formats expected vs. actual values from the diagnostic.

Usage::

    from ekp_forge.sandbox.hint_generator import HintGenerator

    generator = HintGenerator()
    references = generator.generate_hints(diagnostics)
    # references is list[Reference] to attach to FixTaskV2
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from ekp_forge.sandbox.introspection import IntrospectionTool
from ekp_forge.schemas.contract import (
    Diagnostic,
    DiagnosticCategory,
    Reference,
)


class HintGenerator:
    """Generates structured hints (``Reference`` objects) per diagnostic type.

    Each diagnostic type triggers a different deterministic hint strategy.
    Returns an empty list if no hints could be generated for any diagnostic.
    """

    def __init__(self, workspace: Path | None = None) -> None:
        """Initialise the hint generator.

        Args:
            workspace: Optional workspace path. If None, uses CWD.
        """
        self._workspace = workspace or Path.cwd()
        self._introspection = IntrospectionTool(workspace=self._workspace)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_hints(self, diagnostics: list[Diagnostic]) -> list[Reference]:
        """Generate structured hints for a list of diagnostics.

        Each diagnostic is routed to the appropriate hint strategy based
        on its category and message content.

        Args:
            diagnostics: The diagnostics to generate hints for.

        Returns:
            List of ``Reference`` objects (empty if no hints generated).
        """
        references: list[Reference] = []

        for diag in diagnostics:
            ref = self._generate_for_diagnostic(diag)
            if ref is not None:
                references.append(ref)

        return references

    # ------------------------------------------------------------------
    # Diagnostic routing
    # ------------------------------------------------------------------

    def _generate_for_diagnostic(self, diag: Diagnostic) -> Reference | None:
        """Route a single diagnostic to the appropriate hint generator."""
        # Ruff/mypy AttributeError or undefined name → introspect module
        if (
            diag.category == DiagnosticCategory.UNDEFINED_NAME
            or "has no attribute" in diag.message.lower()
            or ("module" in diag.message.lower() and "not defined" in diag.message.lower())
        ):
            return self._handle_attribute_or_name_error(diag)

        # Mypy type mismatch → extract function signature
        if diag.category == DiagnosticCategory.TYPE_MISMATCH:
            return self._handle_type_mismatch(diag)

        # Import errors → scan valid modules
        if diag.category == DiagnosticCategory.IMPORT:
            return self._handle_import_error(diag)

        # Wrong return value (Pytest assertion) → expected vs actual
        if diag.category == DiagnosticCategory.WRONG_RETURN_VALUE:
            return self._handle_assertion_error(diag)

        return None

    # ------------------------------------------------------------------
    # Strategy 1: AttributeError / Undefined Name → Introspection
    # ------------------------------------------------------------------

    def _handle_attribute_or_name_error(self, diag: Diagnostic) -> Reference | None:
        """Use ``IntrospectionTool`` to resolve AttributeError/undefined name.

        Extracts module name from the diagnostic message, runs ``dir()``
        in a sandboxed subprocess, and returns a structured Reference.
        """
        module_name = self._extract_module_name(diag.message)
        if not module_name:
            return None

        result = self._introspection.inspect_module(module_name)
        if result.error and not result.attributes:
            return None

        formatted = IntrospectionTool.format_for_prompt(result)

        return Reference(
            reference_type="introspection_dir",
            target=module_name,
            content=formatted,
            source_tool="hint_generator",
        )

    @staticmethod
    def _extract_module_name(message: str) -> str | None:
        """Extract module name from error messages."""
        # Pattern 1: module 'X' has no attribute 'Y'
        m = re.search(r"module\s+'([^']+)'", message)
        if m:
            return m.group(1)

        # Pattern 2: name 'X' is not defined
        m = re.search(r"name\s+'([^']+)'\s+is not defined", message)
        if m:
            return m.group(1)

        # Pattern 3: 'X' object has no attribute
        m = re.search(r"'([^']+)'\s+object has no attribute", message)
        if m:
            return m.group(1)

        return None

    # ------------------------------------------------------------------
    # Strategy 2: Type Mismatch → Function Signature
    # ------------------------------------------------------------------

    def _handle_type_mismatch(self, diag: Diagnostic) -> Reference | None:
        """Extract function signature from source using AST.

        For mypy arg-type errors, parse the file and extract the function
        signature containing the error line.
        """
        file_path = self._workspace / diag.file if not Path(diag.file).is_absolute() else Path(diag.file)
        if not file_path.exists():
            return None

        source = file_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None

        # Find function at the diagnostic's line
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
                if node.lineno <= diag.line <= end_line:
                    # Build signature string
                    args: list[str] = []
                    for arg in node.args.args:
                        arg_annotation = ""
                        if arg.annotation:
                            arg_annotation = ast.unparse(arg.annotation)
                        args.append(f"{arg.arg}: {arg_annotation}" if arg_annotation else arg.arg)

                    returns = ""
                    if node.returns:
                        returns = f" -> {ast.unparse(node.returns)}"

                    signature = f"def {node.name}({', '.join(args)}){returns}:"

                    return Reference(
                        reference_type="function_signature",
                        target=f"{diag.file}:{node.name}",
                        content=(f"Function signature:\n{signature}\n\nError: {diag.message}\nLine {diag.line}"),
                        source_tool="hint_generator",
                    )

        return None

    # ------------------------------------------------------------------
    # Strategy 3: Import Error → Valid Module List
    # ------------------------------------------------------------------

    def _handle_import_error(self, diag: Diagnostic) -> Reference | None:
        """Scan project for valid importable modules.

        Returns a list of available top-level packages/modules in the
        project's virtual environment and standard library.
        """
        valid_modules = self._scan_available_modules()
        if not valid_modules:
            return None

        import_name = self._extract_import_name(diag.message)
        suggestions = ""
        if import_name:
            similar = [
                m
                for m in valid_modules
                if import_name.lower() in m.lower() or m.lower().startswith(import_name.lower()[:3])
            ]
            if similar:
                suggestions = f"Did you mean: {', '.join(similar[:10])}?"

        content_parts = [
            "Available modules in project environment:",
            ", ".join(sorted(valid_modules)[:30]),
        ]
        if len(valid_modules) > 30:
            content_parts.append("... and more")

        if suggestions:
            content_parts.append("")
            content_parts.append(suggestions)

        return Reference(
            reference_type="valid_modules",
            target=import_name or "unknown",
            content="\n".join(content_parts),
            source_tool="hint_generator",
        )

    def _scan_available_modules(self) -> list[str]:
        """Scan the project environment for importable modules.

        Checks .venv site-packages and adds curated stdlib modules.
        """
        modules: set[str] = set()

        # Check .venv site-packages
        venv_base = self._workspace / ".venv"
        if venv_base.exists():
            for site_pkg in venv_base.rglob("site-packages"):
                if site_pkg.is_dir():
                    for p in site_pkg.iterdir():
                        if p.is_dir() and not p.name.startswith("_"):
                            modules.add(p.name)
                        elif p.is_file() and p.suffix == ".py" and not p.name.startswith("_"):
                            modules.add(p.stem)

        # Common stdlib modules
        stdlib = {
            "os",
            "sys",
            "json",
            "re",
            "math",
            "datetime",
            "pathlib",
            "typing",
            "collections",
            "functools",
            "itertools",
            "subprocess",
            "hashlib",
            "base64",
            "abc",
            "enum",
            "dataclasses",
            "inspect",
            "ast",
            "importlib",
            "threading",
            "multiprocessing",
            "io",
            "textwrap",
            "string",
            "random",
            "statistics",
            "uuid",
            "copy",
            "pprint",
            "logging",
            "warnings",
            "contextlib",
            "fractions",
            "decimal",
            "socket",
            "email",
            "html",
            "http",
            "urllib",
            "xml",
            "configparser",
            "csv",
            "gzip",
            "zipfile",
            "tarfile",
            "tempfile",
            "shutil",
            "glob",
            "fnmatch",
            "linecache",
        }
        modules.update(stdlib)

        return sorted(modules)

    @staticmethod
    def _extract_import_name(message: str) -> str | None:
        """Extract the import name from an import error message."""
        m = re.search(r"No module named '([^']+)'", message)
        if m:
            return m.group(1)

        m = re.search(r"cannot import name '([^']+)'", message)
        if m:
            return m.group(1)

        m = re.search(r"import\s+(\w+)", message)
        if m:
            return m.group(1)

        return None

    # ------------------------------------------------------------------
    # Strategy 4: Assertion Error → Expected vs Actual
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_assertion_error(diag: Diagnostic) -> Reference | None:
        """Format expected vs actual values from a test failure diagnostic."""
        content_parts: list[str] = ["Assertion failure details:"]

        if diag.expected:
            content_parts.append(f"  Expected: {diag.expected}")
        if diag.actual:
            content_parts.append(f"  Actual:   {diag.actual}")
        if diag.message:
            content_parts.append(f"  Message:  {diag.message}")

        if len(content_parts) <= 1:
            return None

        return Reference(
            reference_type="expected_vs_actual",
            target=diag.file,
            content="\n".join(content_parts),
            source_tool="hint_generator",
        )
