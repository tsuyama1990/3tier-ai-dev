#!/usr/bin/env python3
"""
Tests for Smoke Tracer extract_snippet() fix (Plan A).

These tests verify the monolithic node snippet fix that prevents
import-only snippets from being written as verified API demos.
"""

import ast
import sys
import unittest
from pathlib import Path

# Ensure the dsc package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dsc.smoke_tracer import extract_snippet


class TestExtractSnippetMonolithicNodeFix(unittest.TestCase):
    """Tests for the monolithic node fallback fix in extract_snippet()."""

    def test_giant_function_no_main(self):
        """A file with a giant function (no __main__) should include the function body."""
        # 1 import line + 1 empty line + 90 lines of function body = 92 lines
        # With max_lines=80, the function exceeds budget
        src = "from ase import Atoms\n" + "def giant_func():\n" + ("    x = 1\n" * 90)
        snippet = extract_snippet(src, "ase", max_lines=80)

        # Should parse successfully
        self.assertTrue(ast.parse(snippet), "Snippet should be syntactically valid")

        # Should have non-import content (the function body)
        non_import = [
            line
            for line in snippet.splitlines()
            if line.strip() and not line.lstrip().startswith(("import", "from"))
        ]
        self.assertTrue(
            non_import, f"Should have non-import content, got: {snippet[:100]!r}"
        )

    def test_normal_file(self):
        """A normal file with short statements should work as before."""
        src = "from ase import Atoms\natoms = Atoms('Cu')\n"
        snippet = extract_snippet(src, "ase", max_lines=80)

        self.assertIn("Atoms", snippet)
        self.assertTrue(ast.parse(snippet), "Snippet should be syntactically valid")

    def test_main_block(self):
        """A file with __main__ block should extract the block correctly."""
        src = "from ase import Atoms\nif __name__=='__main__':\n    a=Atoms('H')\n    print(a)\n"
        snippet = extract_snippet(src, "ase", max_lines=80)

        self.assertIn("Atoms", snippet)
        self.assertTrue(ast.parse(snippet), "Snippet should be syntactically valid")

    def test_import_only_fallback(self):
        """A file with only imports and no other content should fall back to raw head."""
        # This is a pathological case: only imports, nothing else
        src = (
            "from ase import Atoms\nfrom ase.io import read\nfrom ase.io import write\n"
        )
        snippet = extract_snippet(src, "ase", max_lines=80)

        # Should fall back to raw head (all lines)
        self.assertIn("from ase import Atoms", snippet)
        self.assertTrue(ast.parse(snippet), "Snippet should be syntactically valid")

    def test_giant_class_no_main(self):
        """A file with a giant class (no __main__) should include the class body."""
        # Create a class that exceeds the line budget
        src = "from ase import Atoms\n" + "class GiantClass:\n" + ("    x = 1\n" * 90)
        snippet = extract_snippet(src, "ase", max_lines=80)

        # Should parse successfully
        self.assertTrue(ast.parse(snippet), "Snippet should be syntactically valid")

        # Should have non-import content (the class body)
        non_import = [
            line
            for line in snippet.splitlines()
            if line.strip() and not line.lstrip().startswith(("import", "from"))
        ]
        self.assertTrue(
            non_import, f"Should have non-import content, got: {snippet[:100]!r}"
        )

    def test_mixed_imports_and_code(self):
        """A file with imports and code within budget should work correctly."""
        src = "from ase import Atoms\nfrom ase.calculators.emt import EMT\natoms = Atoms('H2O')\n"
        snippet = extract_snippet(src, "ase", max_lines=80)

        self.assertIn("Atoms", snippet)
        self.assertIn("EMT", snippet)
        self.assertTrue(ast.parse(snippet), "Snippet should be syntactically valid")


if __name__ == "__main__":
    unittest.main()
