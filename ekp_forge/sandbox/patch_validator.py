"""Patch Validator — static scope-violation detection for Worker fixes.

Phase 4, Priority 3.

The Patch Validator checks that a Worker's fix does not modify anything
outside the ``FixTaskV2``-specified scope. If it does, the patch is
**REJECTED** and the Worker must retry with the rejection feedback.

Validation checks:
1. No new top-level functions/classes added.
2. No top-level functions/classes removed (except the target symbol).
3. No import statements modified.
4. For class methods, parent class structure is preserved.

Usage::

    from ekp_forge.sandbox.patch_validator import PatchValidator

    validator = PatchValidator()
    result = validator.validate(
        original_source="def foo(): return 1",
        fixed_source="def foo(): return 42",
        target_symbol="foo",
    )

    if not result.accepted:
        print(f"REJECTED: {result.reasons}")
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationResult:
    """Result of a patch validation.

    Attributes:
        accepted:  ``True`` if the patch is within scope.
        reasons:   List of rejection reasons (empty if accepted).
        details:   Formatted string of all rejection reasons.
    """

    accepted: bool = True
    reasons: list[str] = field(default_factory=list)
    details: str = ""


class PatchValidator:
    """Validates that a Worker's fix only modifies the target symbol.

    All checks are performed via AST (stdlib), not LibCST, because
    validation only needs structural comparison (definition names and
    imports), not formatting preservation.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(
        self,
        original_source: str,
        fixed_source: str,
        target_symbol: str,
    ) -> ValidationResult:
        """Validate that a fix only modified the target symbol.

        Checks performed:
        1. No new top-level functions/classes added.
        2. No top-level functions/classes removed (except target).
        3. No import statements modified.
        4. For class methods, parent class structure is preserved.

        Args:
            original_source: The original source code (full file).
            fixed_source:    The Worker's fixed source code (full file).
            target_symbol:   The symbol that was allowed to change.

        Returns:
            ``ValidationResult`` with acceptance status and reasons.
        """
        reasons: list[str] = []

        try:
            original_tree = ast.parse(original_source)
            fixed_tree = ast.parse(fixed_source)
        except SyntaxError as e:
            return ValidationResult(
                accepted=False,
                reasons=[f"Syntax error in fixed source: {e}"],
                details=f"Parse error: {e}",
            )

        # Check 1: Top-level definitions unchanged (except target)
        orig_defs = self._extract_top_level_defs(original_tree)
        fixed_defs = self._extract_top_level_defs(fixed_tree)

        self._check_removed_defs(orig_defs, fixed_defs, target_symbol, reasons)
        self._check_added_defs(orig_defs, fixed_defs, reasons)

        # Check 2: Import statements unchanged
        orig_imports = self._extract_imports(original_tree)
        fixed_imports = self._extract_imports(fixed_tree)

        if orig_imports != fixed_imports:
            reasons.append(
                f"Import statements were modified. "
                f"Imports must remain unchanged during a targeted fix."
            )

        # Check 3: For class methods, check class structure
        if "." in target_symbol:
            class_name, method_name = target_symbol.split(".", 1)
            self._validate_class_method(
                original_tree, fixed_tree, class_name, method_name, reasons
            )

        if reasons:
            details = ";\n".join(reasons)
            return ValidationResult(
                accepted=False,
                reasons=reasons,
                details=f"Patch validation rejected:\n{details}",
            )

        return ValidationResult(accepted=True, details="Patch validation passed.")

    # ------------------------------------------------------------------
    # Check helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_removed_defs(
        orig_defs: dict[str, str],
        fixed_defs: dict[str, str],
        target_symbol: str,
        reasons: list[str],
    ) -> None:
        """Check if any definitions were removed (except target)."""
        for name, node_type in orig_defs.items():
            if name not in fixed_defs and name != target_symbol:
                reasons.append(
                    f"Top-level {node_type} '{name}' was removed by the fix. "
                    f"Only '{target_symbol}' was allowed to change."
                )

    @staticmethod
    def _check_added_defs(
        orig_defs: dict[str, str],
        fixed_defs: dict[str, str],
        reasons: list[str],
    ) -> None:
        """Check if any new definitions were added."""
        for name, node_type in fixed_defs.items():
            if name not in orig_defs:
                reasons.append(
                    f"New top-level {node_type} '{name}' was added by the fix. "
                    f"Adding new symbols is not allowed during a targeted fix."
                )

    @staticmethod
    def _validate_class_method(
        original_tree: ast.Module,
        fixed_tree: ast.Module,
        class_name: str,
        method_name: str,
        reasons: list[str],
    ) -> None:
        """Validate that a class method fix didn't break class structure."""
        orig_methods = _extract_class_methods(original_tree, class_name)
        fixed_methods = _extract_class_methods(fixed_tree, class_name)

        if orig_methods is None:
            reasons.append(f"Class '{class_name}' not found in original source.")
            return

        if fixed_methods is None:
            reasons.append(
                f"Class '{class_name}' was removed from the fixed source."
            )
            return

        # Check removed methods
        for method in orig_methods:
            if method not in fixed_methods and method != method_name:
                reasons.append(
                    f"Method '{class_name}.{method}' was removed. "
                    f"Only '{class_name}.{method_name}' was allowed to change."
                )

        # Check added methods
        for method in fixed_methods:
            if method not in orig_methods:
                reasons.append(
                    f"New method '{class_name}.{method}' was added. "
                    f"Adding new methods is not allowed during a targeted fix."
                )

    # ------------------------------------------------------------------
    # AST extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_top_level_defs(tree: ast.Module) -> dict[str, str]:
        """Extract top-level function and class definitions.

        Returns:
            Dict mapping definition name → type description.
        """
        defs: dict[str, str] = {}
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef):
                defs[node.name] = "function"
            elif isinstance(node, ast.AsyncFunctionDef):
                defs[node.name] = "async function"
            elif isinstance(node, ast.ClassDef):
                defs[node.name] = "class"
        return defs

    @staticmethod
    def _extract_imports(tree: ast.Module) -> set[str]:
        """Extract import statements as a canonical set of strings."""
        imports: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = ", ".join(alias.name for alias in node.names)
                imports.add(f"from {module} import {names}")
        return imports


def _extract_class_methods(
    tree: ast.Module, class_name: str
) -> list[str] | None:
    """Extract method names from a class definition.

    Args:
        tree:       The parsed AST module.
        class_name: The name of the class to inspect.

    Returns:
        List of method names, or ``None`` if the class doesn't exist.
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            methods: list[str] = []
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(item.name)
            return methods
    return None
