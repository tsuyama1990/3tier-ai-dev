#!/usr/bin/env python3
"""
Unit tests for dsc/deploy.py

All tests use dry-run mode or temporary directories so that no real
project files are modified during the test run.
"""

import sys
from pathlib import Path

import pytest

# ── Ensure dsc/ is importable ─────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from dsc.deploy import (
    _parse_package_spec,
    generate_api_schema,
    merge_workflow_graphs,
    deploy,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_cache(tmp_path):
    """
    Create a temporary knowledge cache directory with a fake mesa=9.9.9 entry.
    Patches KNOWLEDGE_CACHE to point to it.
    """
    cache_root = tmp_path / "knowledge-cache"
    mesa_dir = cache_root / "mesa" / "9.9.9"
    mesa_dir.mkdir(parents=True)

    (mesa_dir / "integration_graph.md").write_text("# Mesa 9.9.9 Integration Graph\n")
    (mesa_dir / "workflow_graph.md").write_text(
        "# Mesa 9.9.9 Workflow Graph\n\n## Typical Usage Flow\n\n```mermaid\ngraph TD\n    A --> B\n```\n"
    )

    examples_dir = mesa_dir / "verified_examples"
    examples_dir.mkdir()
    (examples_dir / "minimal_sim.py").write_text("import mesa\nprint('ok')\n")

    tests_dir = mesa_dir / "verified_tests"
    tests_dir.mkdir()
    (tests_dir / "smoke_mesa.py").write_text(
        "import mesa\ndef test_smoke(): assert True\n"
    )

    # Monkeypatch the module-level constant
    import dsc.deploy as deploy_mod
    original = deploy_mod.KNOWLEDGE_CACHE
    deploy_mod.KNOWLEDGE_CACHE = cache_root
    yield cache_root, mesa_dir
    deploy_mod.KNOWLEDGE_CACHE = original


@pytest.fixture
def tmp_project(tmp_path):
    """Return a temporary project directory."""
    project = tmp_path / "myproject"
    project.mkdir()
    return project


# ── _parse_package_spec ───────────────────────────────────────────────────────


class TestParsePackageSpec:
    def test_single_equals(self):
        assert _parse_package_spec("mesa=3.5.1") == ("mesa", "3.5.1")

    def test_double_equals(self):
        assert _parse_package_spec("mesa==3.5.1") == ("mesa", "3.5.1")

    def test_invalid_no_version(self):
        with pytest.raises(ValueError, match="Invalid package spec"):
            _parse_package_spec("mesa")

    def test_invalid_empty_name(self):
        with pytest.raises(ValueError):
            _parse_package_spec("=3.5.1")

    def test_complex_version(self):
        name, ver = _parse_package_spec("ase=3.28.0.post1")
        assert name == "ase"
        assert ver == "3.28.0.post1"


# ── _auto_detect_version ──────────────────────────────────────────────────────


class TestAutoDetectVersion:
    def test_detects_version(self, tmp_cache):
        cache_root, _ = tmp_cache
        import dsc.deploy as deploy_mod
        # KNOWLEDGE_CACHE is already patched to cache_root
        ver = deploy_mod._auto_detect_version("mesa")
        assert ver == "9.9.9"

    def test_returns_none_for_missing_pkg(self, tmp_cache):
        import dsc.deploy as deploy_mod
        ver = deploy_mod._auto_detect_version("nonexistent_pkg_xyz")
        assert ver is None


# ── generate_api_schema ───────────────────────────────────────────────────────


class TestGenerateApiSchema:
    def test_basic_generation(self):
        schema = generate_api_schema([("mesa", "3.5.1")])
        assert "allowed_imports:" in schema
        assert "- mesa" in schema
        assert "- pytest" in schema  # test infrastructure
        assert "- os" in schema  # stdlib

    def test_multiple_packages(self):
        schema = generate_api_schema([("ase", "3.28.0"), ("pacemaker", "0.8.4")])
        assert "- ase" in schema
        assert "- pacemaker" in schema

    def test_append_to_existing(self, tmp_path):
        schema_path = tmp_path / "api_schema.yaml"
        # Write an existing schema with a manual addition
        schema_path.write_text(
            "allowed_imports:\n  - mesa\n  - mylocal\n"
        )
        # Deploy ase into a project that already has mesa
        updated = generate_api_schema(
            [("ase", "3.28.0")],
            existing_path=schema_path,
            force=False,
        )
        assert "- ase" in updated
        # Manual addition 'mylocal' should be preserved
        assert "mylocal" in updated

    def test_force_regenerates(self, tmp_path):
        schema_path = tmp_path / "api_schema.yaml"
        schema_path.write_text("allowed_imports:\n  - old_pkg\n")
        updated = generate_api_schema(
            [("mesa", "3.5.1")],
            existing_path=schema_path,
            force=True,
        )
        # Force=True: fresh generation, old_pkg is gone
        assert "old_pkg" not in updated
        assert "- mesa" in updated

    def test_no_duplicate_when_pkg_already_present(self, tmp_path):
        schema_path = tmp_path / "api_schema.yaml"
        schema_path.write_text("allowed_imports:\n  - mesa\n")
        schema = generate_api_schema(
            [("mesa", "3.5.1")],
            existing_path=schema_path,
            force=False,
        )
        # Should not duplicate 'mesa'
        count = schema.count("- mesa")
        assert count == 1


# ── merge_workflow_graphs ─────────────────────────────────────────────────────


class TestMergeWorkflowGraphs:
    def test_single_package_passthrough(self):
        content = "# Mesa 3.5.1 Workflow\n\nsome content"
        result = merge_workflow_graphs([("mesa", "3.5.1", content)])
        assert result == content

    def test_two_packages_merged(self):
        c1 = "# Mesa 3.5.1 Workflow\n\n## Typical Usage\n\ncontent1"
        c2 = "# ASE 3.28.0 Workflow\n\n## Typical Usage\n\ncontent2"
        result = merge_workflow_graphs(
            [("mesa", "3.5.1", c1), ("ase", "3.28.0", c2)]
        )
        assert "Multi-Package Workflow Graph" in result
        assert "mesa 3.5.1" in result
        assert "ase 3.28.0" in result
        assert "content1" in result
        assert "content2" in result

    def test_empty_contents(self):
        result = merge_workflow_graphs([])
        assert result == ""


# ── deploy (integration) ──────────────────────────────────────────────────────


class TestDeploy:
    def test_dry_run_does_not_write(self, tmp_cache, tmp_project):
        deploy(
            project=tmp_project,
            packages=[("mesa", "9.9.9")],
            dry_run=True,
            force=False,
        )
        # In dry-run mode no actual files should be created
        assert not (tmp_project / ".ai-knowledge" / "mesa.md").exists()
        assert not (tmp_project / "verified_examples" / "minimal_sim.py").exists()
        assert not (tmp_project / "verified_tests" / "smoke_mesa.py").exists()
        assert not (tmp_project / "api_schema.yaml").exists()

    def test_deploy_creates_expected_files(self, tmp_cache, tmp_project):
        deploy(
            project=tmp_project,
            packages=[("mesa", "9.9.9")],
            dry_run=False,
            force=False,
        )
        assert (tmp_project / ".ai-knowledge" / "mesa.md").exists()
        assert (tmp_project / ".ai-knowledge" / "workflow_graph.md").exists()
        assert (tmp_project / "verified_examples" / "minimal_sim.py").exists()
        assert (tmp_project / "verified_tests" / "smoke_mesa.py").exists()
        assert (tmp_project / "api_schema.yaml").exists()

    def test_integration_graph_content(self, tmp_cache, tmp_project):
        deploy(project=tmp_project, packages=[("mesa", "9.9.9")])
        content = (tmp_project / ".ai-knowledge" / "mesa.md").read_text()
        assert "Integration Graph" in content

    def test_api_schema_contains_package(self, tmp_cache, tmp_project):
        deploy(project=tmp_project, packages=[("mesa", "9.9.9")])
        schema = (tmp_project / "api_schema.yaml").read_text()
        assert "- mesa" in schema

    def test_skip_existing_without_force(self, tmp_cache, tmp_project):
        # First deploy
        deploy(project=tmp_project, packages=[("mesa", "9.9.9")])
        # Corrupt the file to test it's NOT overwritten
        corrupt = tmp_project / ".ai-knowledge" / "mesa.md"
        corrupt.write_text("CORRUPTED")
        # Second deploy without force
        deploy(project=tmp_project, packages=[("mesa", "9.9.9")], force=False)
        assert corrupt.read_text() == "CORRUPTED"

    def test_force_overwrites_existing(self, tmp_cache, tmp_project):
        # First deploy
        deploy(project=tmp_project, packages=[("mesa", "9.9.9")])
        corrupt = tmp_project / ".ai-knowledge" / "mesa.md"
        corrupt.write_text("CORRUPTED")
        # Second deploy with force
        deploy(project=tmp_project, packages=[("mesa", "9.9.9")], force=True)
        content = corrupt.read_text()
        assert "CORRUPTED" not in content
        assert "Integration Graph" in content

    def test_missing_cache_sets_error(self, tmp_cache, tmp_project):
        result = deploy(
            project=tmp_project,
            packages=[("nonexistent_pkg_xyz", "1.0.0")],
        )
        assert result["packages"][0]["error"] is not None

    def test_result_structure(self, tmp_cache, tmp_project):
        result = deploy(
            project=tmp_project,
            packages=[("mesa", "9.9.9")],
        )
        assert "project" in result
        assert "packages" in result
        assert len(result["packages"]) == 1
        pkg = result["packages"][0]
        assert pkg["name"] == "mesa"
        assert pkg["version"] == "9.9.9"
        assert pkg["error"] is None
        assert pkg["integration_graph"] == "copied"
        assert len(pkg["verified_examples"]) == 1
        assert len(pkg["verified_tests"]) == 1

    def test_multi_package_workflow_merge(self, tmp_path):
        """Two packages → workflow_graph.md should be a merged file."""
        import dsc.deploy as deploy_mod

        cache_root = tmp_path / "knowledge-cache"
        original = deploy_mod.KNOWLEDGE_CACHE
        deploy_mod.KNOWLEDGE_CACHE = cache_root

        try:
            # Package names must be lower-case (deploy normalises to .lower())
            for pkg, ver in [("pkga", "1.0.0"), ("pkgb", "2.0.0")]:
                d = cache_root / pkg / ver
                d.mkdir(parents=True)
                (d / "integration_graph.md").write_text(f"# {pkg} Integration\n")
                (d / "workflow_graph.md").write_text(
                    f"# {pkg} {ver} Workflow\n\n## Flow\n\ncontent_{pkg}\n"
                )

            project = tmp_path / "proj"
            project.mkdir()

            # Call deploy() while the monkeypatch is still active
            deploy(
                project=project,
                packages=[("pkga", "1.0.0"), ("pkgb", "2.0.0")],
            )

            wg = (project / ".ai-knowledge" / "workflow_graph.md").read_text()
            assert "Multi-Package" in wg
            assert "pkga" in wg
            assert "pkgb" in wg
        finally:
            deploy_mod.KNOWLEDGE_CACHE = original

    def test_multi_version_coexistence(self, tmp_path):
        """When multiple versions of the same package exist in the cache, deploy should choose the specified version."""
        import dsc.deploy as deploy_mod

        cache_root = tmp_path / "knowledge-cache"
        original = deploy_mod.KNOWLEDGE_CACHE
        deploy_mod.KNOWLEDGE_CACHE = cache_root

        try:
            # Create two versions for 'mesa'
            for ver in ["3.5.1", "3.6.0"]:
                d = cache_root / "mesa" / ver
                d.mkdir(parents=True)
                (d / "integration_graph.md").write_text(f"# Mesa {ver} Integration Graph\n")
                (d / "workflow_graph.md").write_text(f"# Mesa {ver} Workflow Graph\n")

            project = tmp_path / "proj"
            project.mkdir()

            # 1. Deploy specific version 3.5.1
            deploy(
                project=project,
                packages=[("mesa", "3.5.1")],
                force=True,
            )
            ig_content = (project / ".ai-knowledge" / "mesa.md").read_text()
            assert "Mesa 3.5.1" in ig_content
            assert "Mesa 3.6.0" not in ig_content

            # 2. Deploy specific version 3.6.0
            deploy(
                project=project,
                packages=[("mesa", "3.6.0")],
                force=True,
            )
            ig_content = (project / ".ai-knowledge" / "mesa.md").read_text()
            assert "Mesa 3.6.0" in ig_content
            assert "Mesa 3.5.1" not in ig_content
        finally:
            deploy_mod.KNOWLEDGE_CACHE = original

