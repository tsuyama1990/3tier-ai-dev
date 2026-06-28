"""Worker introspection tool — safely executes dir()/help() in sandbox.

Phase 3, Priority 2.

When the Worker encounters an ``AttributeError`` or ``ModuleNotFoundError``
during the fix loop, this tool attempts to resolve the issue by inspecting
the actual module or object in a segregated read-only subprocess.

Design:
- All execution happens via ``importlib.import_module()`` in a **segregated
  read-only subprocess** (same sandbox workspace, separate process).
- The tool NEVER modifies global state or writes files.
- Results are structured as ``IntrospectionResult`` for prompt injection.
- Hard timeout of 10s per call; cumulative budget of 30s tracked by Worker.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum characters in the formatted prompt output
_MAX_PROMPT_CHARS = 3000

# Maximum characters per docstring excerpt
_MAX_DOC_CHARS = 500

# Maximum number of attributes/methods to include
_MAX_ATTRIBUTES = 50

# Subprocess timeout in seconds
_INTROSPECTION_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class IntrospectionResult:
    """Structured result of an introspection operation.

    Attributes:
        module_name:    The module/object name that was inspected.
        attributes:     List of attribute names from ``dir()``.
        callables:      List of callable method/function names.
        classes:        List of class names found.
        signature:      Function/class signature if applicable.
        doc_summary:    First N chars of docstring.
        error:          Error message if introspection failed (empty = success).
    """

    module_name: str = ""
    attributes: list[str] = field(default_factory=list)
    callables: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    signature: str = ""
    doc_summary: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Introspection Tool
# ---------------------------------------------------------------------------


class IntrospectionTool:
    """Sandbox-safe introspection tool for WorkerAgent.

    Usage (within sandbox only)::

        tool = IntrospectionTool()
        result = tool.inspect_module("json")
        result = tool.inspect_object(some_object, "my_var")

    All execution happens in a read-only subprocess. The main process
    never imports the target module directly.
    """

    def __init__(self, workspace: Path | None = None) -> None:
        """Initialise the introspection tool.

        Args:
            workspace: The sandbox workspace path. If ``None``, uses ``Path.cwd()``.
        """
        self._workspace = workspace or Path.cwd()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def inspect_module(self, module_name: str) -> IntrospectionResult:
        """Import a module and return its structure via subprocess.

        Steps:
        1. Generate a small Python script that performs the import + dir().
        2. Run it in a subprocess with 10s timeout.
        3. Parse the JSON output into an ``IntrospectionResult``.

        Args:
            module_name: The fully-qualified module name (e.g. ``"json"``).

        Returns:
            ``IntrospectionResult`` with the module's structure.
        """
        script = self._build_inspect_module_script(module_name)
        return self._run_script(script, module_name)

    def inspect_object(self, obj_repr: str, name: str = "") -> IntrospectionResult:
        """Inspect an object by evaluating its representation in subprocess.

        .. caution::
           This method evaluates a Python expression to obtain the object.
           Only use with trusted, known-safe expressions (e.g. ``sys.stdout``).

        Args:
            obj_repr: A Python expression that evaluates to the object.
            name:     A human-readable name for the object (for the result).

        Returns:
            ``IntrospectionResult`` with the object's structure.
        """
        script = self._build_inspect_object_script(obj_repr, name)
        return self._run_script(script, name or obj_repr)

    def resolve_attribute_error(
        self,
        module_name: str,
        attribute_name: str,
    ) -> IntrospectionResult:
        """Specifically resolve an ``AttributeError``.

        Example: ``"module 'X' has no attribute 'Y'"``
        → Imports X, runs dir(X), checks if Y exists under different name.

        Args:
            module_name:   The module name from the error message.
            attribute_name: The missing attribute name.

        Returns:
            ``IntrospectionResult`` with full module structure + suggestions.
        """
        result = self.inspect_module(module_name)
        if result.error:
            return result

        # Check if the attribute actually exists (false positive?)
        if attribute_name in result.attributes:
            result.error = ""  # Clear error — attribute exists
            return result

        # Suggest similar attributes (fuzzy match)
        suggestions = self._fuzzy_match(attribute_name, result.attributes)
        if suggestions:
            result.error = (
                f"Attribute '{attribute_name}' not found in '{module_name}'. "
                f"Did you mean: {', '.join(suggestions[:5])}?"
            )
        else:
            result.error = (
                f"Attribute '{attribute_name}' not found in '{module_name}'. "
                f"Available attributes: {', '.join(result.attributes[:_MAX_ATTRIBUTES])}"
            )

        return result

    # ------------------------------------------------------------------
    # Prompt Formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_for_prompt(result: IntrospectionResult) -> str:
        """Format introspection result as concise context for Worker prompt.

        Output is capped at ``_MAX_PROMPT_CHARS`` (3000) to prevent context bloom.

        Args:
            result: The ``IntrospectionResult`` to format.

        Returns:
            A formatted string for injection into the Worker's prompt.
        """
        if result.error and not result.attributes:
            return f"[Introspection Error] {result.error}"

        lines: list[str] = []
        lines.append(f"[Introspection: {result.module_name}]")

        if result.doc_summary:
            lines.append(f"Doc: {result.doc_summary}")

        if result.signature:
            lines.append(f"Signature: {result.signature}")

        if result.callables:
            callables_str = ", ".join(result.callables[:_MAX_ATTRIBUTES])
            lines.append(f"Callables: {callables_str}")

        if result.classes:
            classes_str = ", ".join(result.classes[:_MAX_ATTRIBUTES])
            lines.append(f"Classes: {classes_str}")

        if result.attributes:
            attrs_str = ", ".join(result.attributes[:_MAX_ATTRIBUTES])
            lines.append(f"Attributes: {attrs_str}")

        if result.error:
            lines.append(f"Note: {result.error}")

        output = "\n".join(lines)
        if len(output) > _MAX_PROMPT_CHARS:
            output = output[:_MAX_PROMPT_CHARS] + "\n... [truncated]"

        return output

    # ------------------------------------------------------------------
    # Script Generation
    # ------------------------------------------------------------------

    @staticmethod
    def _build_inspect_module_script(module_name: str) -> str:
        """Generate a Python script that inspects a module.

        The script performs:
        1. importlib.import_module(module_name)
        2. dir(module)
        3. Classify members into callables, classes, plain attributes
        4. Extract docstring and signature
        5. Output JSON to stdout
        """
        return f"""
import importlib, inspect, json, sys

try:
    mod = importlib.import_module({json.dumps(module_name)})
except ModuleNotFoundError as e:
    print(json.dumps({{"error": str(e), "module_name": {json.dumps(module_name)}}}))
    sys.exit(0)
except Exception as e:
    print(json.dumps({{"error": f"Import failed: {{e}}", "module_name": {json.dumps(module_name)}}}))
    sys.exit(0)

all_attrs = dir(mod)
callables = []
classes = []
plain_attrs = []

for name in all_attrs:
    try:
        obj = getattr(mod, name)
        if inspect.isclass(obj):
            classes.append(name)
        elif callable(obj):
            callables.append(name)
        else:
            plain_attrs.append(name)
    except Exception:
        plain_attrs.append(name)

# Docstring
doc = getattr(mod, '__doc__', '') or ''
doc_summary = doc[:{_MAX_DOC_CHARS}].strip() if doc else ''

# Try to get version
version = getattr(mod, '__version__', '') or ''

result = {{
    "module_name": {json.dumps(module_name)},
    "attributes": all_attrs[:{_MAX_ATTRIBUTES * 2}],
    "callables": callables[:{_MAX_ATTRIBUTES}],
    "classes": classes[:{_MAX_ATTRIBUTES}],
    "signature": f"version={{version}}" if version else "",
    "doc_summary": doc_summary,
    "error": "",
}}
print(json.dumps(result))
"""

    @staticmethod
    def _build_inspect_object_script(obj_repr: str, name: str) -> str:
        """Generate a Python script that inspects an object expression."""
        return f"""
import inspect, json, sys

try:
    obj = eval({json.dumps(obj_repr)})
except Exception as e:
    print(json.dumps({{"error": str(e), "module_name": {json.dumps(name)}}}))
    sys.exit(0)

all_attrs = dir(obj)
callables = []
classes = []
plain_attrs = []

for attr_name in all_attrs:
    try:
        attr = getattr(obj, attr_name)
        if inspect.isclass(attr):
            classes.append(attr_name)
        elif callable(attr):
            callables.append(attr_name)
        else:
            plain_attrs.append(attr_name)
    except Exception:
        plain_attrs.append(attr_name)

doc = getattr(obj, '__doc__', '') or ''
doc_summary = doc[:{_MAX_DOC_CHARS}].strip() if doc else ''

# Try to get type signature
sig = ''
try:
    if callable(obj):
        sig = str(inspect.signature(obj))
except Exception:
    pass

result = {{
    "module_name": {json.dumps(name)},
    "attributes": all_attrs[:{_MAX_ATTRIBUTES * 2}],
    "callables": callables[:{_MAX_ATTRIBUTES}],
    "classes": classes[:{_MAX_ATTRIBUTES}],
    "signature": sig,
    "doc_summary": doc_summary,
    "error": "",
}}
print(json.dumps(result))
"""

    # ------------------------------------------------------------------
    # Subprocess Execution
    # ------------------------------------------------------------------

    def _run_script(self, script: str, module_name: str) -> IntrospectionResult:
        """Run an introspection script in a segregated subprocess.

        Args:
            script:      The Python script to execute.
            module_name: The module name (for error reporting).

        Returns:
            ``IntrospectionResult`` parsed from subprocess stdout.
        """
        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=_INTROSPECTION_TIMEOUT,
                cwd=str(self._workspace),
            )
            stdout = result.stdout.strip()
            if not stdout:
                return IntrospectionResult(
                    module_name=module_name,
                    error=f"Subprocess returned empty output (stderr: {result.stderr[:200]})",
                )
            return self._parse_result(stdout, module_name)

        except subprocess.TimeoutExpired:
            return IntrospectionResult(
                module_name=module_name,
                error=f"Introspection timed out after {_INTROSPECTION_TIMEOUT}s",
            )
        except Exception as e:
            return IntrospectionResult(
                module_name=module_name,
                error=f"Subprocess error: {e}",
            )

    @staticmethod
    def _parse_result(stdout: str, module_name: str) -> IntrospectionResult:
        """Parse JSON output from subprocess into ``IntrospectionResult``.

        Args:
            stdout:      The JSON string from subprocess stdout.
            module_name: Fallback module name if JSON parsing fails.

        Returns:
            ``IntrospectionResult`` with parsed data.
        """
        try:
            data: dict[str, Any] = json.loads(stdout)
            return IntrospectionResult(
                module_name=data.get("module_name", module_name),
                attributes=data.get("attributes", []),
                callables=data.get("callables", []),
                classes=data.get("classes", []),
                signature=data.get("signature", ""),
                doc_summary=data.get("doc_summary", ""),
                error=data.get("error", ""),
            )
        except (json.JSONDecodeError, TypeError) as e:
            return IntrospectionResult(
                module_name=module_name,
                error=f"Failed to parse introspection result: {e}",
            )

    # ------------------------------------------------------------------
    # Fuzzy Matching
    # ------------------------------------------------------------------

    @staticmethod
    def _fuzzy_match(name: str, candidates: list[str]) -> list[str]:
        """Simple fuzzy matching: find similar names.

        Uses two heuristics:
        1. Case-insensitive substring match.
        2. Levenshtein-like prefix/edit distance (simple).

        Args:
            name:       The target name to match.
            candidates: List of candidate names.

        Returns:
            Sorted list of similar names (best match first).
        """
        name_lower = name.lower()
        scored: list[tuple[int, str]] = []

        for candidate in candidates:
            c_lower = candidate.lower()
            score = 0

            # Exact match (case-insensitive)
            if c_lower == name_lower:
                score = 100
            # Starts with the same prefix
            elif c_lower.startswith(name_lower):
                score = 80
            # Substring match
            elif name_lower in c_lower:
                score = 60
            # Character overlap (simple)
            else:
                common = sum(1 for c in name_lower if c in c_lower)
                if common > max(len(name_lower) * 0.5, 2):
                    score = common * 10

            if score > 0:
                scored.append((score, candidate))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:5]]
