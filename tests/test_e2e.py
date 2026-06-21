import json
import os
import subprocess
import sys

from dsc import asset_synthesizer, deploy, package_inspector, smoke_tracer, source_miner


def run_main_safely(main_func, argv):
    try:
        main_func(argv)
    except SystemExit as e:
        assert e.code == 0 or e.code is None


def test_dsc_pipeline_e2e(tmp_path, monkeypatch):
    # 1. Prepare temporary directories
    dummy_git_repo = tmp_path / "dummy_git_repo"
    dummy_git_repo.mkdir()

    temp_project = tmp_path / "temp_project"
    temp_project.mkdir()

    temp_cache_root = tmp_path / "temp_cache"
    temp_cache_root.mkdir()

    # Monkeypatch the KNOWLEDGE_CACHE constant across the DSC modules that use it,
    # and set EKP_KNOWLEDGE_CACHE to ensure consistency for subprocesses/config.
    monkeypatch.setenv("EKP_KNOWLEDGE_CACHE", str(temp_cache_root))
    monkeypatch.setattr(package_inspector, "KNOWLEDGE_CACHE", temp_cache_root)
    monkeypatch.setattr(source_miner, "KNOWLEDGE_CACHE", temp_cache_root)
    monkeypatch.setattr(deploy, "KNOWLEDGE_CACHE", temp_cache_root)

    # 2. Set up dummy package source and a local git repository
    dummypkg_src = dummy_git_repo / "dummypkg"
    dummypkg_src.mkdir()
    (dummypkg_src / "__init__.py").write_text(
        "class Simulator:\n"
        "    def __init__(self, steps=10):\n"
        "        self.steps = steps\n"
        "    def run(self):\n"
        "        return f'simulated {self.steps} steps'\n",
        encoding="utf-8",
    )

    tests_dir = dummy_git_repo / "tests"
    tests_dir.mkdir()
    # High static scoring test file
    (tests_dir / "test_dummy.py").write_text(
        "# A dummy test file to hit max static score in smoke_tracer\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "\n"
        "from dummypkg import Simulator\n"
        "import dummypkg\n"
        "from dummypkg import Simulator as Sim2\n"
        "\n"
        "def test_dummy():\n"
        "    sim1 = Simulator(1)\n"
        "    sim2 = Sim2(2)\n"
        "    sim3 = dummypkg.Simulator(3)\n"
        "    assert sim1.run() == 'simulated 1 steps'\n"
        "    assert sim2.run() == 'simulated 2 steps'\n"
        "    assert sim3.run() == 'simulated 3 steps'\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    # main block\n"
        "    pass\n",
        encoding="utf-8",
    )

    examples_dir = dummy_git_repo / "examples"
    examples_dir.mkdir()
    # High static scoring example file
    (examples_dir / "demo_run.py").write_text(
        "# A dummy simulation run to hit max static score in smoke_tracer\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "# Adding lines to reach the 20-line sweet spot\n"
        "\n"
        "from dummypkg import Simulator\n"
        "import dummypkg\n"
        "from dummypkg import Simulator as Sim2\n"
        "\n"
        "sim1 = Simulator(1)\n"
        "sim2 = Sim2(2)\n"
        "sim3 = dummypkg.Simulator(3)\n"
        "\n"
        "print(sim1.run())\n"
        "print(sim2.run())\n"
        "print(sim3.run())\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    # main block\n"
        "    pass\n",
        encoding="utf-8",
    )

    # Initialize Git repository and commit files
    subprocess.run(["git", "init"], cwd=dummy_git_repo, check=True)
    subprocess.run(["git", "config", "user.name", "E2E Tester"], cwd=dummy_git_repo, check=True)
    subprocess.run(["git", "config", "user.email", "e2e@tester.com"], cwd=dummy_git_repo, check=True)
    subprocess.run(["git", "add", "."], cwd=dummy_git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=dummy_git_repo, check=True)

    # 3. Build a dummy venv/site-packages layout inside temp_project
    site_packages = temp_project / ".venv" / "lib" / "python3.10" / "site-packages"
    site_packages.mkdir(parents=True)

    dist_info = site_packages / "dummypkg-1.0.0.dist-info"
    dist_info.mkdir()

    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\n"
        "Name: dummypkg\n"
        "Version: 1.0.0\n",
        encoding="utf-8",
    )

    direct_url_data = {
        "url": dummy_git_repo.as_uri(),
        "vcs_info": {"vcs": "git", "commit_id": "dummycommit"},
    }
    (dist_info / "direct_url.json").write_text(json.dumps(direct_url_data), encoding="utf-8")

    # Symlink current interpreter to the virtual environment's bin folder
    bin_dir = temp_project / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    (bin_dir / "python3").symlink_to(sys.executable)

    # 4. Set Python paths (to let test subprocesses import dummypkg)
    monkeypatch.syspath_prepend(str(dummy_git_repo))
    old_pythonpath = os.environ.get("PYTHONPATH", "")
    new_pythonpath = str(dummy_git_repo)
    if old_pythonpath:
        new_pythonpath = f"{new_pythonpath}:{old_pythonpath}"
    monkeypatch.setenv("PYTHONPATH", new_pythonpath)

    # 5. Run the entire pipeline sequentially
    manifest_path = temp_project / "manifest.json"

    # Stage 1: package_inspector
    run_main_safely(
        package_inspector.main,
        ["--project", str(temp_project), "--target", "dummypkg", "--output", str(manifest_path)],
    )
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["packages"][0]["found"] is True
    assert manifest["packages"][0]["version"] == "1.0.0"

    # Stage 2: source_miner
    run_main_safely(
        source_miner.main,
        ["--manifest", str(manifest_path), "--output", str(temp_cache_root / "dummypkg" / "1.0.0")],
    )

    cache_pkg_dir = temp_cache_root / "dummypkg" / "1.0.0"
    assert (cache_pkg_dir / "verified_tests" / "tests" / "test_dummy.py").exists()
    assert (cache_pkg_dir / "verified_examples" / "examples" / "demo_run.py").exists()

    # Stage 3: smoke_tracer
    run_main_safely(smoke_tracer.main, ["--manifest", str(manifest_path)])
    assert (cache_pkg_dir / "smoke_trace_report.json").exists()

    # Stage 4: asset_synthesizer (offline mode)
    run_main_safely(
        asset_synthesizer.main, ["--manifest", str(manifest_path), "--no-llm"]
    )
    assert (cache_pkg_dir / "integration_graph.md").exists()

    # Stage 5: deploy
    run_main_safely(
        deploy.main, ["--project", str(temp_project), "--packages", "dummypkg=1.0.0"]
    )

    # 6. Verify outputs and assert final deployment state
    assert (temp_project / ".ai-knowledge" / "dummypkg.md").exists()
    assert (temp_project / "verified_examples" / "examples" / "demo_run.py").exists()
    assert (temp_project / "verified_tests" / "tests" / "test_dummy.py").exists()

    schema_path = temp_project / "api_schema.yaml"
    assert schema_path.exists()
    schema_content = schema_path.read_text(encoding="utf-8")
    assert "- dummypkg" in schema_content
