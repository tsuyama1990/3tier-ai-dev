import os
import sys
import subprocess
from pathlib import Path

# Add the parent directory of this file to sys.path to safely import orchestrator
sys.path.append(str(Path(__file__).parent.resolve()))
import orchestrator

def run_3tier_dev(prompt, target_pkg, target_files, timeout=600):
    """
    Safely invokes 3tier AI dev / Aider internally, bypassing sys.stdin
    and enforcing timeout, lock mechanism, and self-healing loop.
    Returns results in a structured dictionary.
    """
    lock_file = Path(".ekp.lock")
    if lock_file.exists():
        return {"success": False, "status": "locked"}

    # P1: Check if knowledge file exists for target_pkg
    knowledge_file = Path(f".ai-knowledge/{target_pkg}.md")
    if not knowledge_file.exists():
        return {"success": False, "status": "knowledge_missing"}

    try:
        # Acquire lock
        lock_file.write_text(f"locked by run_3tier_dev process {os.getpid()}")
        
        # Prepare extra Aider arguments from local directories
        extra_args = []
        if os.path.exists(".ai-knowledge"):
            for f in sorted(os.listdir(".ai-knowledge")):
                fpath = os.path.join(".ai-knowledge", f)
                if os.path.isfile(fpath) and f.endswith(".md"):
                    extra_args.extend(["--read", fpath])
        if os.path.exists("verified_examples"):
            for f in sorted(os.listdir("verified_examples")):
                fpath = os.path.join("verified_examples", f)
                if os.path.isfile(fpath) and f.endswith(".py"):
                    extra_args.extend(["--read", fpath])

        temp_msg_file = ".aider.msg.temp"
        try:
            with open(temp_msg_file, "w", encoding="utf-8") as f:
                f.write(prompt)
        except Exception as e:
            orchestrator.log(f"Failed to create temp message file: {str(e)}")
            return {"success": False, "status": "error", "message": f"Failed to create temp message file: {str(e)}"}

        aider_cmd = [orchestrator.REAL_AIDER] + extra_args + ["--message-file", temp_msg_file] + target_files
        orchestrator.log(f"Starting initial Aider pass with command: {' '.join(aider_cmd)}")
        
        try:
            res = subprocess.run(
                aider_cmd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout
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
            return {
                "success": False,
                "status": "error",
                "message": f"Initial Aider pass failed with code: {res.returncode}",
                "stdout": res.stdout,
                "stderr": res.stderr
            }

        # Self-healing loop
        max_retries = 3
        retries = 0
        
        while retries < max_retries:
            orchestrator.run_cleanup()
            orchestrator.cleanup_files()
            import_success, import_err = orchestrator.validate_imports()
            if not import_success:
                success = False
                test_log = import_err
            else:
                success, test_log = orchestrator.run_tests()
                
            if success:
                orchestrator.log("All tests passed (or no tests found). Success!")
                return {
                    "success": True,
                    "files_changed": target_files,
                    "status": "success",
                    "stdout": res.stdout,
                    "stderr": res.stderr
                }
                
            retries += 1
            orchestrator.log(f"Test failure detected. Retrying correction ({retries}/{max_retries})...")
            
            repair_instructions = (
                f"The tests failed. Please fix the implementation to make the tests pass.\n"
                f"Note: If a constraint or constructor signature documented in `.ai-knowledge/` conflicts with the empirical runtime traceback (e.g. causes a TypeError or AttributeError), the runtime behavior takes absolute precedence. Real-world execution is the ground truth. Correct the code to match the actual Python behavior even if it violates the documented rule in `.ai-knowledge/`.\n\n"
                f"--- Test Output ---\n{test_log}"
            )
            
            try:
                with open(temp_msg_file, "w", encoding="utf-8") as f:
                    f.write(repair_instructions)
            except Exception as e:
                orchestrator.log(f"Failed to create temp message file for repair: {str(e)}")
                return {"success": False, "status": "error", "message": f"Failed to create temp message file for repair: {str(e)}"}
                
            orchestrator.log("Running Aider for self-healing...")
            try:
                res = subprocess.run(
                    aider_cmd,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=timeout
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
                    "stderr": res.stderr
                }
                
        orchestrator.log("Self-healing failed after max retries.")
        
        # P1: Git rollback on failure
        orchestrator.log("Performing git rollback due to failure...")
        try:
            subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, check=False)
            subprocess.run(["git", "clean", "-fd"], capture_output=True, check=False)
            orchestrator.log("Git rollback completed.")
        except Exception as e:
            orchestrator.log(f"Git rollback failed: {str(e)}")
        
        return {
            "success": False,
            "status": "failed",
            "message": "Self-healing failed after max retries",
            "stdout": res.stdout,
            "stderr": res.stderr
        }

    finally:
        # Clean up the lock file in all exit paths
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception as e:
                orchestrator.log(f"Failed to remove lock file: {str(e)}")
