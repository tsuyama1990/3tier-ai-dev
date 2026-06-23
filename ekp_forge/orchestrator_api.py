import os
import subprocess
from pathlib import Path

from ekp_forge import orchestrator


def run_3tier_dev(
    prompt: str,
    target_pkg: str,
    target_files: list[str],
    timeout: int = 600,
    model: str | None = None,
    skip_self_healing: bool = False,
) -> dict:
    """
    Safely invokes 3tier AI dev / Aider internally, bypassing sys.stdin
    and enforcing timeout, lock mechanism, and self-healing loop.
    Returns results in a structured dictionary.
    """
    try:
        orchestrator.setup_ruff_mypy()
    except Exception as e:
        orchestrator.log(f"Failed to run setup_ruff_mypy: {e!s}")

    lock_file = Path(".ekp.lock")
    if os.path.exists(".ekp.lock"):
        return {"success": False, "status": "locked"}

    # P1: Check if knowledge file exists for target_pkg
    knowledge_file = Path(f".ai-knowledge/{target_pkg}.md")
    if not knowledge_file.exists():
        return {"success": False, "status": "knowledge_missing"}

    # Pre-create target files to make sure they exist
    for f in target_files:
        fpath = Path(f)
        if not fpath.exists():
            try:
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.touch()
            except Exception as e:
                orchestrator.log(f"Failed to pre-create target file {f}: {e!s}")

    try:
        # Acquire lock
        lock_file.write_text(f"locked by run_3tier_dev process {os.getpid()}")

        from ekp_forge.sandbox.cloner import clone_into
        from ekp_forge.sandbox.integrator import integrate_changes
        from ekp_forge.sandbox.workspace import SandboxWorkspace

        with SandboxWorkspace() as ws_path:
            clone_ok, clone_err = clone_into(ws_path)
            if not clone_ok:
                return {"success": False, "status": "error", "message": f"Sandbox clone failed: {clone_err}"}

            original_cwd = os.getcwd()
            os.chdir(ws_path / "repo")
            try:
                # Prepare extra Aider arguments from local directories
                extra_args = []
                if os.path.exists(".ai-knowledge"):
                    for f in sorted(os.listdir(".ai-knowledge")):
                        extra_fpath = os.path.join(".ai-knowledge", f)
                        if os.path.isfile(extra_fpath) and f.endswith(".md"):
                            extra_args.extend(["--read", extra_fpath])
                if os.path.exists("verified_examples"):
                    for f in sorted(os.listdir("verified_examples")):
                        extra_fpath = os.path.join("verified_examples", f)
                        if os.path.isfile(extra_fpath) and f.endswith(".py"):
                            extra_args.extend(["--read", extra_fpath])

                temp_msg_file = ".aider.msg.temp"
                try:
                    with open(temp_msg_file, "w", encoding="utf-8") as msg_file:
                        msg_file.write(prompt)
                except Exception as e:
                    orchestrator.log(f"Failed to create temp message file: {e!s}")
                    return {
                        "success": False,
                        "status": "error",
                        "message": f"Failed to create temp message file: {e!s}",
                    }

                aider_cmd = [orchestrator.REAL_AIDER, "--yes", "--no-git", "--edit-format", "diff"]
                if model:
                    aider_cmd.extend(["--model", model])
                aider_cmd.extend([*extra_args, "--message-file", temp_msg_file, *target_files])

                orchestrator.log(f"Starting initial Aider pass with command: {' '.join(aider_cmd)}")

                env = os.environ.copy()
                env["AIDER_MAP_TOKENS"] = "0"
                try:
                    res = subprocess.run(
                        aider_cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, env=env
                    )
                except subprocess.TimeoutExpired:
                    orchestrator.log("Aider initial pass timed out")
                    return {"success": False, "status": "timeout"}
                finally:
                    if os.path.exists(temp_msg_file):
                        try:
                            os.remove(temp_msg_file)
                        except Exception:
                            pass

                if res.returncode != 0:
                    orchestrator.log(f"Initial Aider pass failed with code: {res.returncode}")
                    # P1: Git rollback on failure (sandbox only)
                    orchestrator.log("Performing git rollback due to failure...")
                    try:
                        subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, check=False)
                        subprocess.run(["git", "clean", "-fdx", "--exclude=.venv"], capture_output=True, check=False)
                        orchestrator.log("Git rollback completed.")
                    except Exception as e:
                        orchestrator.log(f"Git rollback failed: {e!s}")
                    return {
                        "success": False,
                        "status": "error",
                        "message": f"Initial Aider pass failed with code: {res.returncode}",
                        "stdout": res.stdout,
                        "stderr": res.stderr,
                    }

                if skip_self_healing:
                    orchestrator.run_cleanup()
                    orchestrator.cleanup_files()
                    import_success, import_err = orchestrator.validate_imports()

                    test_log_parts = []
                    if not import_success:
                        success = False
                        test_log_parts.append(f"--- Import Validation Failures ---\n{import_err}")
                    else:
                        from ekp_forge.sandbox.scoped_lint import _changed_files

                        changed_file_paths = _changed_files()
                        changed_files = [str(f) for f in changed_file_paths] if changed_file_paths else None

                        pytest_success, pytest_log = orchestrator.run_tests()
                        ruff_success, ruff_log = orchestrator.run_ruff(changed_files)
                        mypy_success, mypy_log = orchestrator.run_mypy(changed_files)

                        success = pytest_success and ruff_success and mypy_success

                        if not pytest_success:
                            test_log_parts.append(f"--- Pytest Failures ---\n{pytest_log}")
                        if not ruff_success:
                            test_log_parts.append(f"--- Ruff Lint Failures ---\n{ruff_log}")
                        if not mypy_success:
                            test_log_parts.append(f"--- Mypy Type Failures ---\n{mypy_log}")

                    test_log = "\n\n".join(test_log_parts)

                    if success:
                        orchestrator.log("Validation passed with skip_self_healing=True.")
                        os.chdir(original_cwd)
                        integrate_ok, integrate_err = integrate_changes(Path(original_cwd), sandbox_path=ws_path)
                        if not integrate_ok:
                            return {
                                "success": False,
                                "status": "error",
                                "message": f"Integration failed: {integrate_err}",
                            }
                        return {
                            "success": True,
                            "files_changed": target_files,
                            "status": "success",
                            "stdout": res.stdout,
                            "stderr": res.stderr,
                        }
                    orchestrator.log(f"Validation failed with skip_self_healing=True. Details: {test_log}")
                    orchestrator.log("Performing git rollback due to failure...")
                    try:
                        subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, check=False)
                        subprocess.run(["git", "clean", "-fdx", "--exclude=.venv"], capture_output=True, check=False)
                        orchestrator.log("Git rollback completed.")
                    except Exception as e:
                        orchestrator.log(f"Git rollback failed: {e!s}")
                    return {
                        "success": False,
                        "status": "failed",
                        "message": f"Validation failed: {test_log}",
                        "stdout": res.stdout,
                        "stderr": res.stderr,
                    }

                # Self-healing loop
                max_retries = 3
                retries = 0

                while retries < max_retries:
                    orchestrator.run_cleanup()
                    orchestrator.cleanup_files()
                    import_success, import_err = orchestrator.validate_imports()

                    test_log_parts = []
                    if not import_success:
                        success = False
                        test_log_parts.append(f"--- Import Validation Failures ---\n{import_err}")
                    else:
                        from ekp_forge.sandbox.scoped_lint import _changed_files

                        changed_file_paths = _changed_files()
                        changed_files = [str(f) for f in changed_file_paths] if changed_file_paths else None

                        pytest_success, pytest_log = orchestrator.run_tests()
                        ruff_success, ruff_log = orchestrator.run_ruff(changed_files)
                        mypy_success, mypy_log = orchestrator.run_mypy(changed_files)

                        success = pytest_success and ruff_success and mypy_success

                        if not pytest_success:
                            test_log_parts.append(f"--- Pytest Failures ---\n{pytest_log}")
                        if not ruff_success:
                            test_log_parts.append(f"--- Ruff Lint Failures ---\n{ruff_log}")
                        if not mypy_success:
                            test_log_parts.append(f"--- Mypy Type Failures ---\n{mypy_log}")

                    test_log = "\n\n".join(test_log_parts)

                    if success:
                        orchestrator.log("All tests and quality checks passed. Success!")
                        os.chdir(original_cwd)
                        integrate_ok, integrate_err = integrate_changes(Path(original_cwd), sandbox_path=ws_path)
                        if not integrate_ok:
                            return {
                                "success": False,
                                "status": "error",
                                "message": f"Integration failed: {integrate_err}",
                            }
                        return {
                            "success": True,
                            "files_changed": target_files,
                            "status": "success",
                            "stdout": res.stdout,
                            "stderr": res.stderr,
                        }

                    retries += 1
                    orchestrator.log(f"Verification failure detected. Retrying correction ({retries}/{max_retries})...")

                    repair_instructions = (
                        f"The tests failed. Please fix the implementation to make the tests pass.\n"
                        f"Note: If a constraint or constructor signature documented in `.ai-knowledge/` conflicts with the empirical runtime traceback (e.g. causes a TypeError or AttributeError), the runtime behavior takes absolute precedence. Real-world execution is the ground truth. Correct the code to match the actual Python behavior even if it violates the documented rule in `.ai-knowledge/`.\n\n"
                        f"--- Test Output ---\n{test_log}"
                    )

                    try:
                        with open(temp_msg_file, "w", encoding="utf-8") as msg_file:
                            msg_file.write(repair_instructions)
                    except Exception as e:
                        orchestrator.log(f"Failed to create temp message file for repair: {e!s}")
                        return {
                            "success": False,
                            "status": "error",
                            "message": f"Failed to create temp message file for repair: {e!s}",
                        }

                    orchestrator.log("Running Aider for self-healing...")
                    env = os.environ.copy()
                    env["AIDER_MAP_TOKENS"] = "0"
                    try:
                        res = subprocess.run(
                            aider_cmd,
                            capture_output=True,
                            text=True,
                            stdin=subprocess.DEVNULL,
                            timeout=timeout,
                            env=env,
                        )
                    except subprocess.TimeoutExpired:
                        orchestrator.log("Aider repair pass timed out")
                        return {"success": False, "status": "timeout"}
                    finally:
                        if os.path.exists(temp_msg_file):
                            try:
                                os.remove(temp_msg_file)
                            except Exception:
                                pass

                    if res.returncode != 0:
                        orchestrator.log(f"Aider repair pass failed with code: {res.returncode}")
                        return {
                            "success": False,
                            "status": "error",
                            "message": f"Aider repair pass failed with code: {res.returncode}",
                            "stdout": res.stdout,
                            "stderr": res.stderr,
                        }

                orchestrator.log("Self-healing failed after max retries.")

                # P1: Git rollback on failure (sandbox only)
                orchestrator.log("Performing git rollback due to failure...")
                try:
                    subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, check=False)
                    subprocess.run(["git", "clean", "-fdx", "--exclude=.venv"], capture_output=True, check=False)
                    orchestrator.log("Git rollback completed.")
                except Exception as e:
                    orchestrator.log(f"Git rollback failed: {e!s}")

                return {
                    "success": False,
                    "status": "failed",
                    "message": "Self-healing failed after max retries",
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                }
            finally:
                os.chdir(original_cwd)

    finally:
        # Clean up the lock file in all exit paths
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception as e:
                orchestrator.log(f"Failed to remove lock file: {e!s}")
