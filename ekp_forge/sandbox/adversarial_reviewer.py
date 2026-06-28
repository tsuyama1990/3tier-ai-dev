"""AdversarialReviewer — independent edge-case robustness audit.

The Adversarial Reviewer is an independent gate that evaluates code robustness
under edge-case inputs **after** the Worker implementation passes verification.
It runs **between** Worker success and Integrator merge.

Key design properties:
- **Non-blocking**: Failures produce warnings only — they do NOT block pipeline success.
- **Isolated execution**: Each edge-case assertion runs in a separate subprocess
  with a timeout (prevents infinite loops from crashing the pipeline).
- **Deterministic edge-case generation**: Uses AST to find function signatures,
  then generates type-appropriate edge cases (``None``, empty, boundary values).
- **No LLM**: All edge-case generation is rule-based; no LLM calls.

Usage::

    from ekp_forge.sandbox.adversarial_reviewer import AdversarialReviewer

    reviewer = AdversarialReviewer()
    ok, report = reviewer.review(task, code)
    if not ok:
        print(f"Edge-case warnings:\\n{report}")
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ekp_forge.agents.base import BaseAgent, ExecutionTier
from ekp_forge.protocol.capability import Capability

# ---------------------------------------------------------------------------
# Edge-case inputs per Python type
# ---------------------------------------------------------------------------

#: Mapping of Python type names to lists of edge-case input values.
#: Each entry is a tuple of (value_repr, description).
EDGE_CASE_MAP: dict[str, list[tuple[str, str]]] = {
    "int": [
        ("None", "None instead of int"),
        ("0", "zero value"),
        ("-1", "negative value"),
        ("10**6", "large positive value"),
        ("-10**6", "large negative value"),
    ],
    "float": [
        ("None", "None instead of float"),
        ("0.0", "zero value"),
        ("-1.0", "negative value"),
        ("1e308", "very large float"),
        ("-1e308", "very large negative float"),
        ("float('nan')", "NaN input"),
        ("float('inf')", "infinity input"),
    ],
    "str": [
        ("None", "None instead of str"),
        ("''", "empty string"),
        ("'x' * 10**5", "very long string"),
        ("'\\x00'", "null byte in string"),
    ],
    "list": [
        ("None", "None instead of list"),
        ("[]", "empty list"),
        ("[None]", "list with None"),
        ("list(range(10**5))", "very large list"),
    ],
    "dict": [
        ("None", "None instead of dict"),
        ("{}", "empty dict"),
        ("{None: None}", "dict with None key/value"),
    ],
    "tuple": [
        ("None", "None instead of tuple"),
        ("()", "empty tuple"),
    ],
    "set": [
        ("None", "None instead of set"),
        ("set()", "empty set"),
        ("{None}", "set with None"),
    ],
    "bool": [
        ("None", "None instead of bool"),
        ("True", "boolean True"),
        ("False", "boolean False"),
    ],
    "bytes": [
        ("None", "None instead of bytes"),
        ("b''", "empty bytes"),
        ("b'\\x00'", "null byte in bytes"),
    ],
}

#: Types that are commonly used as aliases for Optional[X]
OPTIONAL_PATTERNS: set[str] = {"Optional", "optional"}


class AdversarialReviewer(BaseAgent):
    """Independent gate that reviews code against edge cases.

    The reviewer:
    1. Extracts function signatures from code using AST.
    2. Generates edge-case test inputs based on parameter type hints.
    3. Runs each edge-case assertion in an isolated subprocess with timeout.
    4. Returns warnings for any crashes, but does **not** fail the pipeline.

    Inherits from ``BaseAgent`` for protocol compatibility.
    """

    agent_id: str = "adversarial_reviewer"
    capabilities: list[Capability] = [
        Capability.ADVERSARIAL_REVIEW,
        Capability.VERIFICATION,
    ]
    execution_tier: ExecutionTier = "local"

    #: Maximum edge cases to test per function (prevents excessive runtime).
    MAX_CASES_PER_FUNCTION: int = 5

    #: Timeout in seconds for each edge-case subprocess.
    CASE_TIMEOUT: int = 5

    def __init__(self, model: str = "ollama/qwen2.5-coder:7b") -> None:
        """Initialize the AdversarialReviewer.

        Args:
            model: Model identifier (reserved for future LLM-based review;
                   currently unused — all generation is deterministic).
        """
        self.model = model

    # ------------------------------------------------------------------
    # BaseAgent Protocol
    # ------------------------------------------------------------------

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        """BaseAgent protocol — dispatch based on context keys.

        Expects context keys:
        - ``task`` (required): TaskSchema with ``affected_modules``.
        - ``impl_result`` (optional): dict holding implementation output.
        - ``code`` (optional): pre-extracted code string (overrides task lookup).

        Returns:
            Dict with keys:
            - ``status``: ``"success"`` (always — adversarial review never blocks).
            - ``adversarial_warnings``: list of warning strings.
            - ``robust``: ``True`` if no warnings, ``False`` otherwise.
        """
        task = context.get("task")
        impl_result = context.get("impl_result", {})
        code: str = context.get("code", "")

        if not code.strip():
            code = self._extract_code(task)

        if not code.strip():
            return {
                "status": "success",
                "adversarial_warnings": [],
                "robust": True,
                "reason": "No code to review",
            }

        ok, report = self.review(task, code)

        warnings: list[str] = []
        if not ok:
            warnings.append(report)

        return {
            "status": "success",
            "adversarial_warnings": warnings,
            "robust": ok,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def review(self, task: Any, code: str) -> tuple[bool, str]:
        """Generate adversarial edge-case tests and run them.

        Steps:
        1. Parse the code with AST to find function signatures.
        2. For each function, generate type-appropriate edge-case arguments.
        3. Run each edge-case call in an isolated subprocess.
        4. Collect crashes as warnings.

        Args:
            task: The TaskSchema (may be ``None`` for standalone usage).
            code: The Python source code to review.

        Returns:
            ``(True, "")`` if all edge cases pass (or no functions found).
            ``(False, warning_report)`` if any edge cases crash.
        """
        edge_cases = self._generate_edge_cases(code)

        if not edge_cases:
            return True, ""

        failures: list[str] = []
        for func_name, case_args in edge_cases[: self.MAX_CASES_PER_FUNCTION * 10]:
            crash, output = self._run_edge_case(code, func_name, case_args)
            if crash:
                arg_str = ", ".join(str(a) for a in case_args)
                failures.append(f"Function '{func_name}({arg_str})' crashed: {output[:200].strip()}")

        if failures:
            report = "Adversarial edge-case warnings (non-blocking):\n"
            report += "\n".join(f"  - {f}" for f in failures)
            return False, report

        return True, ""

    # ------------------------------------------------------------------
    # Edge-Case Generation
    # ------------------------------------------------------------------

    def _extract_code(self, task: Any) -> str:
        """Read code from task's affected_modules.

        Args:
            task: TaskSchema with ``affected_modules`` list.

        Returns:
            Concatenated source code from all affected modules.
        """
        if task is None:
            return ""

        affected_modules: list[str] = getattr(task, "affected_modules", [])
        parts: list[str] = []
        for mod in affected_modules:
            p = Path(mod)
            if p.exists():
                parts.append(f"# --- {mod} ---\n{p.read_text(encoding='utf-8')}")
        return "\n\n".join(parts)

    def _generate_edge_cases(self, code: str) -> list[tuple[str, list[str]]]:
        """Generate edge-case argument lists for each function in code.

        Uses AST to find function definitions and their parameter type hints,
        then looks up appropriate edge-case values from ``EDGE_CASE_MAP``.

        Args:
            code: Python source code to analyze.

        Returns:
            List of ``(function_name, list_of_argument_reprs)`` tuples.
        """
        cases: list[tuple[str, list[str]]] = []

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return cases

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                func_cases = self._edge_cases_for_function(node)
                cases.extend(func_cases)

        return cases

    def _edge_cases_for_function(self, node: ast.FunctionDef) -> list[tuple[str, list[str]]]:
        """Generate edge-case argument combinations for a single function.

        Args:
            node: The ``ast.FunctionDef`` node to analyze.

        Returns:
            List of ``(function_name, argument_reprs)`` tuples.
        """
        # Collect parameter names and their type annotations
        params: list[tuple[str, str | None]] = []
        for arg in node.args.args:
            if arg.arg in ("self", "cls"):
                continue  # Skip self/cls for methods
            type_name = self._resolve_type_name(arg.annotation)
            params.append((arg.arg, type_name))

        if not params:
            return []

        # Generate combinations: for each param, pick first applicable edge case
        func_name = node.name
        combos: list[tuple[str, list[str]]] = []

        for param_name, type_name in params:
            edge_values = self._get_edge_values(type_name)
            if not edge_values:
                continue

            # Create one case per edge value for this parameter
            # (all other params get their default/normal values)
            for edge_repr, _desc in edge_values[: self.MAX_CASES_PER_FUNCTION]:
                args: list[str] = []
                for pn, pt in params:
                    if pn == param_name:
                        args.append(edge_repr)
                    else:
                        # Use a "normal" value for other parameters
                        args.append(self._normal_value(pt))
                combos.append((func_name, args))

        return combos[: self.MAX_CASES_PER_FUNCTION]

    @staticmethod
    def _resolve_type_name(annotation: ast.AST | None) -> str | None:
        """Resolve an AST annotation node to a Python type name string.

        Handles:
        - Simple names: ``int``, ``str``
        - Subscripts: ``list[int]`` → ``list``, ``Optional[int]`` → ``int``
        - Constant values: ``None``
        - Union types: ``int | str`` → ``int`` (takes first)

        Args:
            annotation: An AST node representing a type annotation, or ``None``.

        Returns:
            A Python type name string, or ``None`` if unresolvable.
        """
        if annotation is None:
            return None

        # Direct name: e.g., ``int``, ``str``
        if isinstance(annotation, ast.Name):
            return annotation.id

        # Subscript: e.g., ``list[int]``, ``Optional[str]``
        if isinstance(annotation, ast.Subscript):
            value = annotation.value
            if isinstance(value, ast.Name):
                base_name = value.id
                # Unwrap Optional[X] → X
                if base_name in OPTIONAL_PATTERNS:
                    slice_node = annotation.slice
                    if isinstance(slice_node, ast.Name):
                        return slice_node.id
                    if isinstance(slice_node, ast.Subscript):
                        if isinstance(slice_node.value, ast.Name):
                            return slice_node.value.id
                    return None  # Could not unwrap Optional
                return base_name
            return None

        # Constant: e.g., ``None``
        if isinstance(annotation, ast.Constant):
            val = annotation.value
            if val is None:
                return "NoneType"
            return type(val).__name__

        # BinOp (Union): e.g., ``int | str`` - take the first type
        if isinstance(annotation, ast.BinOp):
            if isinstance(annotation.left, ast.Name):
                return annotation.left.id
            return None

        return None

    @staticmethod
    def _get_edge_values(type_name: str | None) -> list[tuple[str, str]]:
        """Look up edge-case values for a type name.

        For untyped parameters (``type_name is None``), returns a default
        set covering ``None``, ``0``, and ``""`` to catch common edge cases
        even without type annotations.

        Args:
            type_name: A Python type name (e.g., ``"int"``, ``"str"``),
                       or ``None`` for untyped parameters.

        Returns:
            List of ``(value_repr, description)`` tuples, or empty list.
        """
        if type_name is None:
            # Default edge cases for untyped parameters
            return [
                ("None", "None for untyped parameter"),
                ("0", "zero for untyped parameter"),
                ("''", "empty string for untyped parameter"),
                ("-1", "negative for untyped parameter"),
            ]

        # Direct lookup
        values = EDGE_CASE_MAP.get(type_name)
        if values is not None:
            return values

        # Handle common aliases
        alias_map: dict[str, str] = {
            "integer": "int",
            "string": "str",
            "boolean": "bool",
            "dictionary": "dict",
            "array": "list",
            "NoneType": "None",
        }
        resolved = alias_map.get(type_name)
        if resolved:
            return EDGE_CASE_MAP.get(resolved, [])

        # Unknown type — just try None
        return [("None", "None for unknown type")]

    @staticmethod
    def _normal_value(type_name: str | None) -> str:
        """Return a 'normal' (non-edge) value for a given type.

        Args:
            type_name: A Python type name.

        Returns:
            A string representation of a normal value.
        """
        normal_map: dict[str, str] = {
            "int": "42",
            "float": "3.14",
            "str": "'hello'",
            "list": "[1, 2, 3]",
            "dict": "{'a': 1}",
            "tuple": "(1, 2)",
            "set": "{1, 2, 3}",
            "bool": "True",
            "bytes": "b'data'",
            "NoneType": "None",
        }
        if type_name is None:
            return "None"
        return normal_map.get(type_name, "None")

    # ------------------------------------------------------------------
    # Isolated Execution
    # ------------------------------------------------------------------

    def _run_edge_case(self, code: str, func_name: str, args: list[str]) -> tuple[bool, str]:
        """Run a single edge-case call in an isolated subprocess.

        Wraps the code plus the edge-case call in a temporary Python script,
        executes it via ``subprocess.run`` with a timeout, and captures any
        exception output.

        Args:
            code:      The full Python source code containing the function.
            func_name: The name of the function to test.
            args:      List of argument string representations.

        Returns:
            ``(True, exception_text)`` if the call crashes.
            ``(False, "")`` if the call completes without error.
        """
        # Build the test script manually (not via dedent) to avoid
        # indentation conflicts between the wrapper and the code under test.
        arg_str = ", ".join(args)
        lines: list[str] = [
            "import sys, traceback",
            "",
            "# --- Code under test ---",
            code,
            "",
            "# --- Edge-case call ---",
            "try:",
            f"    result = {func_name}({arg_str})",
            "except Exception:",
            "    traceback.print_exc()",
            "    sys.exit(1)",
            "",
            "# If we get here, the call succeeded",
            "sys.exit(0)",
            "",
        ]
        wrapper = "\n".join(lines)

        # Write to a temp file and execute
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix="adversarial_",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(wrapper)
            temp_path = f.name

        try:
            proc = subprocess.run(
                [sys.executable, temp_path],
                capture_output=True,
                text=True,
                timeout=self.CASE_TIMEOUT,
            )

            if proc.returncode != 0:
                # Crash detected
                error_output = proc.stderr.strip() or proc.stdout.strip()
                return True, error_output
            return False, ""

        except subprocess.TimeoutExpired:
            return True, f"Timed out after {self.CASE_TIMEOUT}s (possible infinite loop)"
        except OSError as e:
            return True, f"OS error running subprocess: {e}"
        finally:
            # Clean up temp file
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
