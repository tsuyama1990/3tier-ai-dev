#!/usr/bin/env python3
"""
Comprehensive tests for DSC Stage 4: Asset Synthesizer.

Tests cover:
  - load_report()         with/without existing report
  - build_api_index()     normal, empty fallback, deduplication
  - generate_integration_graph()  module grouping, summary stats, format
  - generate_workflow_graph()     Mermaid generation, edge limiting, snippets
  - synthesize()          full pipeline, dry-run
  - CLI                   manifest mode, explicit mode, missing args
"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure the dsc package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dsc.asset_synthesizer import (
    ApiEntry,
    _module_display,
    build_api_index,
    build_parser,
    generate_integration_graph,
    generate_workflow_graph,
    load_report,
    main,
    synthesize,
)

# ── Sample mock data ──────────────────────────────────────────────────────────

SAMPLE_REPORT = {
    "target": "ase",
    "cache_dir": "",
    "version": "3.28.0",
    "files": [
        {
            "rel_path": "verified_examples/examples/00-n2cu.py",
            "category": "example",
            "final_score": 1.0,
            "api_surface": [
                "ase.Atoms",
                "ase.calculators.emt.EMT",
            ],
        },
        {
            "rel_path": "verified_examples/examples/01-atoms.py",
            "category": "example",
            "final_score": 0.95,
            "api_surface": [
                "ase.Atoms",
                "ase.calculators.emt.EMT",
                "ase.io.write",
            ],
        },
        {
            "rel_path": "verified_examples/examples/02-io.py",
            "category": "example",
            "final_score": 0.9,
            "api_surface": [
                "ase.io.read",
                "ase.io.write",
            ],
        },
        {
            "rel_path": "verified_examples/examples/03-build.py",
            "category": "example",
            "final_score": 0.85,
            "api_surface": [
                "ase.build.molecule",
                "ase.build.fcc",
            ],
        },
        {
            "rel_path": "verified_examples/examples/04-opt.py",
            "category": "example",
            "final_score": 0.75,
            "api_surface": [
                "ase.Atoms",
                "ase.optimize.BFGS",
                "ase.calculators.emt.EMT",
            ],
        },
        {
            "rel_path": "verified_tests/test_something.py",
            "category": "test",
            "final_score": 1.0,
            "api_surface": [
                "ase.Atoms",
                "ase.io.read",
            ],
        },
    ],
    "api_surface": [
        "ase.Atoms",
        "ase.build.fcc",
        "ase.build.molecule",
        "ase.calculators.emt.EMT",
        "ase.io.read",
        "ase.io.write",
        "ase.optimize.BFGS",
    ],
}

SAMPLE_CODE_00 = """from ase import Atoms
from ase.calculators.emt import EMT

def test_n2cu():
    atoms = Atoms('N2Cu')
    atoms.calc = EMT()
    energy = atoms.get_potential_energy()
    assert isinstance(energy, float)
"""

SAMPLE_CODE_01 = """from ase import Atoms
from ase.calculators.emt import EMT
from ase.io import write

def test_atoms():
    atoms = Atoms('H2O')
    atoms.calc = EMT()
    write('test.traj', atoms)
"""


# ── Helper ────────────────────────────────────────────────────────────────────


def _make_cache_dir(files_data: list[tuple[str, str]]) -> Path:
    """Create a temporary cache directory with smoke_trace_report.json and example files."""
    tmpdir = Path(tempfile.mkdtemp(prefix="dsc_test_"))

    for rel_path, content in files_data:
        fpath = tmpdir / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")

    return tmpdir


def _write_report(cache_dir: Path, report: dict) -> Path:
    """Write smoke_trace_report.json to cache_dir and return the path."""
    report["cache_dir"] = str(cache_dir)
    rp = cache_dir / "smoke_trace_report.json"
    rp.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return rp


# ── Test Suite ────────────────────────────────────────────────────────────────


class TestLoadReport(unittest.TestCase):
    """Tests for load_report()."""

    def test_load_existing_report(self):
        """load_report should return the parsed JSON when the file exists."""
        tmpdir = Path(tempfile.mkdtemp(prefix="dsc_test_"))
        _write_report(tmpdir, copy.deepcopy(SAMPLE_REPORT))

        result = load_report(tmpdir)
        self.assertEqual(result["target"], "ase")
        self.assertEqual(len(result["files"]), 6)

    def test_load_missing_report_fallback(self):
        """load_report should scan cache_dir when smoke_trace_report.json is missing."""
        py_files = [
            ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
            ("verified_examples/examples/01-atoms.py", SAMPLE_CODE_01),
        ]
        tmpdir = _make_cache_dir(py_files)

        result = load_report(tmpdir)
        self.assertEqual(len(result["files"]), 2)
        # No api_surface in fallback mode
        self.assertEqual(result["api_surface"], [])

    def test_load_empty_cache_dir(self):
        """load_report on an empty cache_dir should return an empty file list."""
        tmpdir = Path(tempfile.mkdtemp(prefix="dsc_test_"))
        result = load_report(tmpdir)
        self.assertEqual(result["files"], [])

    def test_load_report_with_version(self):
        """load_report should preserve version info from the report."""
        report = dict(SAMPLE_REPORT)
        tmpdir = Path(tempfile.mkdtemp(prefix="dsc_test_"))
        _write_report(tmpdir, report)

        result = load_report(tmpdir)
        self.assertEqual(result.get("version"), "3.28.0")


class TestBuildApiIndex(unittest.TestCase):
    """Tests for build_api_index()."""

    def test_build_index_normal(self):
        """build_api_index should create entries for each unique FQN."""
        report = dict(SAMPLE_REPORT)
        index = build_api_index(report)

        self.assertIn("ase.Atoms", index)
        self.assertIn("ase.io.read", index)
        self.assertEqual(len(index), 7)

        atoms = index["ase.Atoms"]
        self.assertEqual(atoms.module, "ase")
        self.assertEqual(atoms.name, "Atoms")
        self.assertIn("verified_examples/examples/00-n2cu.py", atoms.used_in)
        self.assertIn("verified_examples/examples/01-atoms.py", atoms.used_in)
        self.assertEqual(atoms.max_trust_score, 1.0)

    def test_build_index_empty_api_surface_fallback(self):
        """Files with empty api_surface should fall back to report api_surface."""
        report = copy.deepcopy(SAMPLE_REPORT)
        files = report["files"]
        assert isinstance(files, list)
        for f in files:
            assert isinstance(f, dict)
            f["api_surface"] = []
        report["api_surface"] = ["ase.Atoms", "ase.io.read"]

        index = build_api_index(report)
        self.assertIn("ase.Atoms", index)
        self.assertIn("ase.io.read", index)

    def test_build_index_empty_report(self):
        """An empty file list should produce an empty index."""
        index = build_api_index({"files": [], "api_surface": []})
        self.assertEqual(len(index), 0)

    def test_build_index_trust_score_tracking(self):
        """max_trust_score should reflect the highest score across files."""
        report = {
            "files": [
                {"rel_path": "a.py", "final_score": 0.5, "api_surface": ["ase.Atoms"]},
                {"rel_path": "b.py", "final_score": 0.9, "api_surface": ["ase.Atoms"]},
            ],
            "api_surface": ["ase.Atoms"],
        }
        index = build_api_index(report)
        self.assertEqual(index["ase.Atoms"].max_trust_score, 0.9)

    def test_build_index_used_in_dedup(self):
        """The same path should not appear twice in used_in."""
        report = {
            "files": [
                {
                    "rel_path": "a.py",
                    "final_score": 0.5,
                    "api_surface": ["ase.Atoms", "ase.Atoms"],
                },
            ],
            "api_surface": ["ase.Atoms"],
        }
        index = build_api_index(report)
        self.assertEqual(index["ase.Atoms"].used_in, ["a.py"])


class TestModuleDisplay(unittest.TestCase):
    """Tests for _module_display()."""

    def test_core_module(self):
        """Package-level module should display as '{pkg} (core)'."""
        self.assertEqual(_module_display("ase", ""), "ase (core)")
        self.assertEqual(_module_display("ase", "ase"), "ase (core)")

    def test_submodule(self):
        """Submodules should display as-is."""
        self.assertEqual(_module_display("ase", "ase.io"), "ase.io")
        self.assertEqual(
            _module_display("ase", "ase.calculators.emt"), "ase.calculators.emt"
        )


class TestGenerateIntegrationGraph(unittest.TestCase):
    """Tests for generate_integration_graph()."""

    def setUp(self):
        self.report = copy.deepcopy(SAMPLE_REPORT)
        self.index = build_api_index(self.report)

    def test_includes_expected_sections(self):
        """The generated markdown should contain key sections."""
        output = generate_integration_graph(
            self.index, self.report, Path("/tmp"), "ase"
        )

        self.assertIn("# ASE 3.28.0 \u2014 Integration Graph", output)
        self.assertIn("Generated by DSC Asset Synthesizer", output)
        self.assertIn("## API Surface by Module", output)
        self.assertIn("## Summary Statistics", output)

    def test_module_grouping(self):
        """APIs should be grouped into the correct module sections."""
        output = generate_integration_graph(
            self.index, self.report, Path("/tmp"), "ase"
        )

        # Core module
        self.assertIn("### ase (core)", output)
        # Submodules
        self.assertIn("### ase.io", output)
        self.assertIn("### ase.calculators.emt", output)
        self.assertIn("### ase.build", output)
        self.assertIn("### ase.optimize", output)

    def test_api_table_rows(self):
        """Each API should appear in a table row with its FQN."""
        output = generate_integration_graph(
            self.index, self.report, Path("/tmp"), "ase"
        )

        self.assertIn("`ase.Atoms`", output)
        self.assertIn("`ase.io.read`", output)
        self.assertIn("`ase.io.write`", output)
        self.assertIn("`ase.calculators.emt.EMT`", output)

    def test_trust_score_column(self):
        """Trust Score column should show the numeric values."""
        output = generate_integration_graph(
            self.index, self.report, Path("/tmp"), "ase"
        )
        self.assertIn("Trust Score", output)
        self.assertIn("1.0", output)
        self.assertIn("0.95", output)

    def test_summary_statistics(self):
        """Summary Statistics should report correct counts."""
        output = generate_integration_graph(
            self.index, self.report, Path("/tmp"), "ase"
        )

        self.assertIn("Total verified files: 6", output)
        self.assertIn("Unique APIs observed: 7", output)
        self.assertIn("ase (core)", output)
        self.assertIn("ase.build", output)

    def test_core_module_first(self):
        """The core module section should appear before submodules."""
        output = generate_integration_graph(
            self.index, self.report, Path("/tmp"), "ase"
        )
        core_idx = output.index("### ase (core)")
        io_idx = output.index("### ase.io")
        self.assertLess(core_idx, io_idx)

    def test_empty_api_index(self):
        """An empty API index should produce a minimal document."""
        output = generate_integration_graph({}, self.report, Path("/tmp"), "ase")
        self.assertIn("Integration Graph", output)
        self.assertIn("Unique APIs observed: 0", output)


class TestGenerateWorkflowGraph(unittest.TestCase):
    """Tests for generate_workflow_graph()."""

    def setUp(self):
        self.report = copy.deepcopy(SAMPLE_REPORT)
        self.index = build_api_index(self.report)

    def test_includes_mermaid_block(self):
        """Generated output should contain a Mermaid graph TD block."""
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
                ("verified_examples/examples/01-atoms.py", SAMPLE_CODE_01),
                (
                    "verified_examples/examples/02-io.py",
                    "import ase.io\nase.io.read('f')",
                ),
                (
                    "verified_examples/examples/03-build.py",
                    "from ase.build import molecule",
                ),
                (
                    "verified_examples/examples/04-opt.py",
                    "from ase.optimize import BFGS",
                ),
            ]
        )
        _write_report(tmpdir, self.report)

        output = generate_workflow_graph(self.index, self.report, tmpdir, "ase")

        self.assertIn("```mermaid", output)
        self.assertIn("graph TD", output)
        self.assertIn("```", output)

    def test_mermaid_nodes_and_edges(self):
        """Mermaid nodes and edges should be generated from api_surface."""
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
                ("verified_examples/examples/01-atoms.py", SAMPLE_CODE_01),
            ]
        )
        _write_report(tmpdir, self.report)

        output = generate_workflow_graph(self.index, self.report, tmpdir, "ase")

        # Nodes — check FQN presence regardless of insertion-order node ID
        self.assertIn('["ase.Atoms"]', output)
        self.assertIn('["ase.calculators.emt.EMT"]', output)

        # Edges
        self.assertIn("-->", output)

    def test_top_5_files_by_trust_score(self):
        """Only top 5 verified_examples files should be included."""
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
                ("verified_examples/examples/01-atoms.py", SAMPLE_CODE_01),
                (
                    "verified_examples/examples/02-io.py",
                    "import ase.io\nase.io.read('f')",
                ),
                (
                    "verified_examples/examples/03-build.py",
                    "from ase.build import molecule",
                ),
                (
                    "verified_examples/examples/04-opt.py",
                    "from ase.optimize import BFGS",
                ),
            ]
        )
        _write_report(tmpdir, self.report)

        output = generate_workflow_graph(self.index, self.report, tmpdir, "ase")
        self.assertIn("Stage 1", output)
        self.assertIn("Stage 5", output)

    def test_edge_limit_15(self):
        """Edges should be limited to 15 maximum."""
        # Create a file with many APIs to test edge limiting
        many_apis = [f"ase.module.func{i}" for i in range(20)]
        report_many = {
            "target": "ase",
            "version": "3.28.0",
            "files": [
                {
                    "rel_path": "verified_examples/examples/many.py",
                    "category": "example",
                    "final_score": 1.0,
                    "api_surface": many_apis,
                },
            ],
            "api_surface": many_apis,
        }
        index = build_api_index(report_many)
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/many.py", "# many APIs"),
            ]
        )
        _write_report(tmpdir, report_many)

        output = generate_workflow_graph(index, report_many, tmpdir, "ase")
        # Count edges
        edge_lines = [line for line in output.splitlines() if "-->" in line]
        self.assertLessEqual(len(edge_lines), 15)

    def test_code_snippets(self):
        """Code snippets from example files should be included."""
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
            ]
        )
        _write_report(tmpdir, self.report)

        output = generate_workflow_graph(self.index, self.report, tmpdir, "ase")

        self.assertIn("```python", output)
        self.assertIn("from ase import Atoms", output)
        self.assertIn("**Source**: verified_examples/examples/00-n2cu.py", output)

    def test_missing_file_graceful(self):
        """generate_workflow_graph should handle missing files without crashing."""
        tmpdir = Path(tempfile.mkdtemp(prefix="dsc_test_"))
        _write_report(tmpdir, self.report)
        # Don't create the actual example file

        output = generate_workflow_graph(self.index, self.report, tmpdir, "ase")
        self.assertIn("could not read file", output)

    def test_no_verified_examples(self):
        """No verified_examples files should produce a minimal document."""
        report_no_examples = {
            "target": "ase",
            "version": "3.28.0",
            "files": [
                {
                    "rel_path": "verified_tests/test_a.py",
                    "final_score": 1.0,
                    "api_surface": ["ase.Atoms"],
                },
            ],
            "api_surface": ["ase.Atoms"],
        }
        index = build_api_index(report_no_examples)
        tmpdir = Path(tempfile.mkdtemp(prefix="dsc_test_"))
        _write_report(tmpdir, report_no_examples)

        output = generate_workflow_graph(index, report_no_examples, tmpdir, "ase")
        self.assertIn("Workflow Graph", output)
        # No stages since no examples
        self.assertNotIn("Stage 1", output)


class TestSynthesize(unittest.TestCase):
    """Tests for synthesize()."""

    def test_synthesize_full_pipeline(self):
        """synthesize should generate both markdown files."""
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
                ("verified_examples/examples/01-atoms.py", SAMPLE_CODE_01),
            ]
        )
        _write_report(tmpdir, copy.deepcopy(SAMPLE_REPORT))

        result = synthesize(tmpdir, "ase", "3.28.0", dry_run=False)

        self.assertTrue(result["integration_graph_written"])
        self.assertTrue(result["workflow_graph_written"])
        self.assertEqual(result["files_found"], 6)
        self.assertEqual(result["unique_apis"], 7)

        # Verify files exist
        self.assertTrue((tmpdir / "integration_graph.md").exists())
        self.assertTrue((tmpdir / "workflow_graph.md").exists())

    def test_synthesize_dry_run(self):
        """dry_run should not write files to disk."""
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
            ]
        )
        _write_report(tmpdir, copy.deepcopy(SAMPLE_REPORT))

        result = synthesize(tmpdir, "ase", "3.28.0", dry_run=True)

        self.assertFalse(result["integration_graph_written"])
        self.assertFalse(result["workflow_graph_written"])

        # Files should NOT exist
        self.assertFalse((tmpdir / "integration_graph.md").exists())
        self.assertFalse((tmpdir / "workflow_graph.md").exists())

    def test_synthesize_empty_cache(self):
        """synthesize with an empty cache should return a zero report."""
        tmpdir = Path(tempfile.mkdtemp(prefix="dsc_test_"))
        _write_report(
            tmpdir,
            {"target": "ase", "version": "3.28.0", "files": [], "api_surface": []},
        )

        result = synthesize(tmpdir, "ase", "3.28.0", dry_run=False)

        self.assertFalse(result["integration_graph_written"])
        self.assertFalse(result["workflow_graph_written"])
        self.assertEqual(result["files_found"], 0)

    def test_synthesize_integration_graph_content(self):
        """The written integration_graph.md should contain expected content."""
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
            ]
        )
        _write_report(tmpdir, copy.deepcopy(SAMPLE_REPORT))

        synthesize(tmpdir, "ase", "3.28.0", dry_run=False)

        content = (tmpdir / "integration_graph.md").read_text()
        self.assertIn("## API Surface by Module", content)
        self.assertIn("ase.Atoms", content)

    def test_synthesize_workflow_graph_content(self):
        """The written workflow_graph.md should contain expected content."""
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
                ("verified_examples/examples/01-atoms.py", SAMPLE_CODE_01),
            ]
        )
        _write_report(tmpdir, copy.deepcopy(SAMPLE_REPORT))

        synthesize(tmpdir, "ase", "3.28.0", dry_run=False)

        content = (tmpdir / "workflow_graph.md").read_text()
        self.assertIn("```mermaid", content)
        self.assertIn("```python", content)

    def test_synthesize_validation(self):
        """Generated files should pass the validation checks from the spec."""
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
                ("verified_examples/examples/01-atoms.py", SAMPLE_CODE_01),
                (
                    "verified_examples/examples/02-io.py",
                    "import ase.io\nase.io.read('f')",
                ),
            ]
        )
        _write_report(tmpdir, copy.deepcopy(SAMPLE_REPORT))

        synthesize(tmpdir, "ase", "3.28.0", dry_run=False)

        ig = (tmpdir / "integration_graph.md").read_text()
        assert "## API Surface by Module" in ig
        assert "ase.Atoms" in ig
        assert "ase.io" in ig

        wg = (tmpdir / "workflow_graph.md").read_text()
        assert "```mermaid" in wg
        assert "graph TD" in wg


class TestCLI(unittest.TestCase):
    """Tests for the CLI interface."""

    def test_parser_manifest_mode(self):
        """Parser should accept --manifest."""
        args = build_parser().parse_args(["--manifest", "/tmp/test.json"])
        self.assertEqual(args.manifest, "/tmp/test.json")
        self.assertIsNone(args.cache_dir)

    def test_parser_cache_dir_mode(self):
        """Parser should accept --cache-dir with --target and --version."""
        args = build_parser().parse_args(
            [
                "--cache-dir",
                "/tmp/cache",
                "--target",
                "ase",
                "--version",
                "3.28.0",
            ]
        )
        self.assertEqual(args.cache_dir, "/tmp/cache")
        self.assertEqual(args.target, "ase")
        self.assertEqual(args.version, "3.28.0")

    def test_parser_dry_run(self):
        """Parser should accept --dry-run."""
        args = build_parser().parse_args(
            [
                "--cache-dir",
                "/tmp/cache",
                "--target",
                "ase",
                "--version",
                "3.28.0",
                "--dry-run",
            ]
        )
        self.assertTrue(args.dry_run)

    def test_parser_compact(self):
        """Parser should accept --compact."""
        args = build_parser().parse_args(
            [
                "--cache-dir",
                "/tmp/cache",
                "--target",
                "ase",
                "--version",
                "3.28.0",
                "--compact",
            ]
        )
        self.assertTrue(args.compact)

    def test_main_manifest_mode(self):
        """main() should work with manifest mode."""
        tmpdir = Path(tempfile.mkdtemp(prefix="dsc_test_"))
        cache_dir = tmpdir / "ase" / "3.28.0"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Create an example file
        (cache_dir / "verified_examples").mkdir(parents=True, exist_ok=True)
        (cache_dir / "verified_examples" / "test.py").write_text(
            "import ase\n", encoding="utf-8"
        )
        # Write report
        report_data = copy.deepcopy(SAMPLE_REPORT)
        report_data["cache_dir"] = str(cache_dir)
        (cache_dir / "smoke_trace_report.json").write_text(
            json.dumps(report_data),
            encoding="utf-8",
        )

        # Create manifest
        manifest = {
            "project": str(tmpdir),
            "packages": [
                {
                    "name": "ase",
                    "version": "3.28.0",
                    "cache_path": str(cache_dir),
                },
            ],
        }
        manifest_path = tmpdir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        # Run main() — it calls sys.exit(0) on success
        with tempfile.TemporaryFile("w+") as buf:
            old_stdout = sys.stdout
            try:
                sys.stdout = buf
                with self.assertRaises(SystemExit) as cm:
                    main(["--manifest", str(manifest_path)])
                self.assertEqual(cm.exception.code, 0)
                buf.seek(0)
                output = buf.read()
            finally:
                sys.stdout = old_stdout

        self.assertIn("integration_graph_written", output)
        self.assertIn("workflow_graph_written", output)

    def test_main_missing_cache_dir(self):
        """main() should exit with error when cache_dir doesn't exist."""
        with self.assertRaises(SystemExit):
            main(
                [
                    "--cache-dir",
                    "/nonexistent/path",
                    "--target",
                    "ase",
                    "--version",
                    "3.28.0",
                ]
            )

    def test_main_missing_target_version(self):
        """main() should exit with error when --target or --version is missing with --cache-dir."""
        with self.assertRaises(SystemExit):
            main(["--cache-dir", "/tmp/cache"])


class TestEdgeCases(unittest.TestCase):
    """Edge case tests."""

    def test_api_entry_with_single_part(self):
        """An FQN with a single part should have empty module."""
        entry = ApiEntry(fqn="ase", module="", name="ase")
        self.assertEqual(entry.module, "")
        self.assertEqual(entry.name, "ase")

    def test_api_entry_module_extraction(self):
        """Module should be everything except the last dotted component."""
        entry = ApiEntry(
            fqn="ase.calculators.emt.EMT", module="ase.calculators.emt", name="EMT"
        )
        self.assertEqual(entry.module, "ase.calculators.emt")
        self.assertEqual(entry.name, "EMT")

    def test_integration_graph_with_custom_target(self):
        """Should handle custom target package names."""
        report = {
            "target": "numpy",
            "version": "1.24.0",
            "files": [
                {
                    "rel_path": "verified_examples/examples/arr.py",
                    "final_score": 1.0,
                    "api_surface": ["numpy.array", "numpy.linalg.norm"],
                },
            ],
            "api_surface": ["numpy.array", "numpy.linalg.norm"],
        }
        index = build_api_index(report)
        output = generate_integration_graph(index, report, Path("/tmp"), "numpy")

        self.assertIn("# NUMPY 1.24.0", output)
        self.assertIn("### numpy (core)", output)
        self.assertIn("### numpy.linalg", output)
        self.assertIn("`numpy.array`", output)

    def test_workflow_graph_with_single_api(self):
        """A file with a single API should produce a node with no edges."""
        report = {
            "target": "ase",
            "version": "3.28.0",
            "files": [
                {
                    "rel_path": "verified_examples/examples/single.py",
                    "final_score": 1.0,
                    "api_surface": ["ase.Atoms"],
                },
            ],
            "api_surface": ["ase.Atoms"],
        }
        index = build_api_index(report)
        tmpdir = _make_cache_dir(
            [
                ("verified_examples/examples/single.py", "from ase import Atoms\n"),
            ]
        )
        _write_report(tmpdir, report)

        output = generate_workflow_graph(index, report, tmpdir, "ase")
        self.assertIn('N1["ase.Atoms"]', output)
        self.assertNotIn("-->", output)

    def test_synthesize_with_llm(self):
        """synthesize with use_llm=True should fetch semantic markdown using OpenRouter API."""
        from unittest.mock import MagicMock, patch

        # Prepare mock response
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.read.return_value = json.dumps({
            "choices": [{
                "message": {
                    "content": "# ASE 3.28.0 — Integration Graph\n\n## Core API Constraints\n\n### Atoms\n- Constructor Signature: Atoms()\n"
                }
            }]
        }).encode("utf-8")

        tmpdir = _make_cache_dir([
            ("verified_examples/examples/00-n2cu.py", SAMPLE_CODE_00),
        ])
        _write_report(tmpdir, copy.deepcopy(SAMPLE_REPORT))

        # We mock urllib.request.urlopen
        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            # We also mock _get_api_key to avoid environment variable errors
            with patch("dsc.asset_synthesizer._get_api_key", return_value="fake-key"):
                result = synthesize(
                    tmpdir,
                    "ase",
                    "3.28.0",
                    dry_run=False,
                    use_llm=True,
                    llm_model="deepseek/deepseek-v4-flash",
                )

                # Check that urlopen was indeed called
                mock_urlopen.assert_called_once()

                self.assertTrue(result["integration_graph_written"])
                self.assertTrue(result["workflow_graph_written"])

                # Verify that integration_graph.md contains the LLM output
                ig_content = (tmpdir / "integration_graph.md").read_text()
                self.assertIn("# ASE 3.28.0 — Integration Graph", ig_content)
                self.assertIn("Constructor Signature: Atoms()", ig_content)


if __name__ == "__main__":
    unittest.main()
