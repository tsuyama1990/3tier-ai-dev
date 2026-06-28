"""LibCST-based function-level slicer — extract/inject individual functions.

Phase 4, Priority 2.

The Slicer provides two core operations:

1. ``extract_function(source, symbol_name)`` — extracts a single function's
   source code from a file, preserving all comments and formatting.
2. ``inject_fix(original_source, symbol_name, fixed_source)`` — replaces a
   single function in the original file with the fixed version, leaving all
   other code (including imports, other functions, classes) untouched.

Uses **LibCST** (Concrete Syntax Tree) instead of AST because LibCST
preserves comments, whitespace, and formatting byte-for-byte.

Usage::

    from ekp_forge.sandbox.slicer import FunctionSlicer

    slicer = FunctionSlicer()

    # Extract a function for isolated fixing
    extracted = slicer.extract_function(
        source_code="def foo(): return 1",
        symbol_name="foo",
    )

    # Inject a fixed function back
    merged = slicer.inject_fix(
        original_source="def foo(): return 1\\ndef bar(): return 2",
        symbol_name="foo",
        fixed_source="def foo(): return 42",
    )
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import libcst as cst
from libcst import FunctionDef as CSTFunctionDef
from libcst import ClassDef as CSTClassDef
from libcst import Module as CSTModule
from libcst.metadata import MetadataWrapper, PositionProvider


# ---------------------------------------------------------------------------
# Internal visitors
# ---------------------------------------------------------------------------


class _FunctionExtractor(cst.CSTVisitor):
    """CST visitor that finds a specific function definition and extracts it.

    Supports both top-level functions (``target_name="foo"``) and class
    methods (``target_name="MyClass.method"``).
    """

    def __init__(self, target_name: str) -> None:
        self.target_name = target_name
        self.extracted_node: CSTFunctionDef | None = None
        self._current_class: str | None = None

    def visit_FunctionDef(self, node: CSTFunctionDef) -> bool | None:
        name = node.name.value
        full_name = f"{self._current_class}.{name}" if self._current_class else name
        if full_name == self.target_name:
            self.extracted_node = node
            return False  # Stop traversal
        return True

    def visit_ClassDef(self, node: CSTClassDef) -> bool | None:
        self._current_class = node.name.value
        return True  # Continue into class body

    def leave_ClassDef(self, original_node: CSTClassDef) -> None:
        self._current_class = None


class _FunctionReplacer(cst.CSTTransformer):
    """CST transformer that replaces a specific function definition.

    Supports both top-level functions and class methods. Only the matching
    function's body is replaced; everything else is preserved.
    """

    def __init__(self, target_name: str, new_source: str) -> None:
        self.target_name = target_name
        self.new_source = new_source
        self._current_class: str | None = None
        self.replaced = False
        self._replacement_node: CSTFunctionDef | None = None
        self._parse_replacement()

    def _parse_replacement(self) -> None:
        """Parse the new function source into a CST node.

        Uses ``textwrap.dedent`` to handle indented function sources
        (e.g., methods extracted from a class).
        """
        try:
            dedented = textwrap.dedent(self.new_source)
            new_module = cst.parse_module(dedented)
            for statement in new_module.body:
                if isinstance(statement, CSTFunctionDef):
                    self._replacement_node = statement
                    return
        except Exception:
            pass

    def visit_FunctionDef(self, node: CSTFunctionDef) -> bool | None:
        name = node.name.value
        full_name = f"{self._current_class}.{name}" if self._current_class else name
        if full_name == self.target_name and self._replacement_node is not None:
            self.replaced = True
            return False  # We'll replace this node
        return True

    def leave_FunctionDef(
        self, original_node: CSTFunctionDef, updated_node: CSTFunctionDef
    ) -> CSTFunctionDef | cst.CSTNode:
        if self.replaced and self._replacement_node is not None:
            name = original_node.name.value
            full_name = f"{self._current_class}.{name}" if self._current_class else name
            if full_name == self.target_name:
                return self._replacement_node
        return updated_node

    def visit_ClassDef(self, node: CSTClassDef) -> bool | None:
        self._current_class = node.name.value
        return True

    def leave_ClassDef(
        self, original_node: CSTClassDef, updated_node: CSTClassDef
    ) -> CSTClassDef | cst.CSTNode:
        self._current_class = None
        return updated_node


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class FunctionSlicer:
    """LibCST-based function extractor and injector.

    Provides two core operations for Phase 4 function-level isolation:

    - ``extract_function()``: extracts a single function from source code.
    - ``inject_fix()``: replaces a single function with fixed code.

    Both operations preserve all comments, whitespace, and formatting
    of code outside the target function.
    """

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------

    def extract_function(
        self,
        source_code: str,
        symbol_name: str,
    ) -> str | None:
        """Extract a single function definition from source code.

        Args:
            source_code: The full source code of the file.
            symbol_name: The function name (e.g. ``"foo"``) or
                        ``"ClassName.method_name"`` for methods.

        Returns:
            The source code of the extracted function, or ``None`` if
            the symbol was not found or the source could not be parsed.
        """
        try:
            module = cst.parse_module(source_code)
        except Exception:
            return None

        wrapper = MetadataWrapper(module)
        extractor = _FunctionExtractor(symbol_name)
        wrapper.visit(extractor)

        if extractor.extracted_node is None:
            return None

        # Use LibCST's codegen to get the exact source of the node
        return module.code_for_node(extractor.extracted_node)

    # ------------------------------------------------------------------
    # Inject
    # ------------------------------------------------------------------

    def inject_fix(
        self,
        original_source: str,
        symbol_name: str,
        fixed_source: str,
    ) -> str | None:
        """Replace a specific function in the original source with fixed code.

        Args:
            original_source: The original full source code.
            symbol_name:     The function name to replace.
            fixed_source:    The new source code for the function (must be
                            a valid function definition).

        Returns:
            The merged source code with only the target function replaced,
            or ``None`` if the symbol was not found.
        """
        try:
            module = cst.parse_module(original_source)
        except Exception:
            return None

        transformer = _FunctionReplacer(symbol_name, fixed_source)
        modified_module = module.visit(transformer)

        if not transformer.replaced:
            return None

        return modified_module.code

    # ------------------------------------------------------------------
    # File-level convenience methods
    # ------------------------------------------------------------------

    def extract_function_from_file(
        self,
        file_path: str,
        symbol_name: str,
    ) -> str | None:
        """Extract a function from a file on disk.

        Args:
            file_path:  Path to the Python source file.
            symbol_name: The function name to extract.

        Returns:
            The extracted function source code, or ``None``.
        """
        path = Path(file_path)
        if not path.exists():
            return None

        source = path.read_text(encoding="utf-8")
        return self.extract_function(source, symbol_name)

    def inject_fix_to_file(
        self,
        file_path: str,
        symbol_name: str,
        fixed_source: str,
    ) -> bool:
        """Replace a function in a file on disk with fixed code.

        Args:
            file_path:    Path to the Python source file.
            symbol_name:  The function name to replace.
            fixed_source: The new source code for the function.

        Returns:
            ``True`` if the file was modified, ``False`` if the symbol
            was not found or the file could not be read.
        """
        path = Path(file_path)
        if not path.exists():
            return False

        original = path.read_text(encoding="utf-8")
        merged = self.inject_fix(original, symbol_name, fixed_source)

        if merged is None:
            return False

        path.write_text(merged, encoding="utf-8")
        return True
