#!/usr/bin/env python3
import sys
import os
import subprocess
import shutil
import ast
import yaml

REAL_AIDER = "/home/tomo/.local/bin/aider"
LOG_FILE = "/home/tomo/project/000_devenv/3tier_ai_devs/orchestrator.log"

def log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")
    sys.stderr.write(f"[Orchestrator] {msg}\n")
    sys.stderr.flush()

def run_tests():
    log("Running pytest...")
    pytest_bin = "./.venv/bin/pytest"
    if not os.path.exists(pytest_bin):
        pytest_bin = shutil.which("pytest")
        
    if not pytest_bin:
        log("No pytest binary found. Skipping test run.")
        return True, "No pytest binary found."
        
    # Remove old JSON report if it exists
    report_file = ".report.json"
    if os.path.exists(report_file):
        try:
            os.remove(report_file)
        except Exception:
            pass

    cmd = [pytest_bin, "--json-report", f"--json-report-file={report_file}"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    log(f"Pytest exit code: {res.returncode}")
    
    # Return code 0 (all pass) or 5 (no tests found) are successes
    if res.returncode in (0, 5):
        return True, res.stdout
    else:
        # Extract structured traceback from JSON report
        json_traceback = ""
        if os.path.exists(report_file):
            try:
                import json
                with open(report_file) as f:
                    data = json.load(f)
                failures = []
                for test in data.get("tests", []):
                    if test.get("outcome") == "failed":
                        nodeid = test.get("nodeid")
                        call_stage = test.get("call", {})
                        crash = call_stage.get("crash", {})
                        tb_entries = call_stage.get("traceback", [])
                        
                        tb_lines = []
                        for entry in tb_entries:
                            path = entry.get("path")
                            lineno = entry.get("lineno")
                            message = entry.get("message")
                            tb_lines.append(f"  File \"{path}\", line {lineno}\n    {message}")
                        
                        crash_msg = crash.get("message", "Unknown error")
                        failures.append(f"Failed Test: {nodeid}\n" + "\n".join(tb_lines) + f"\nError: {crash_msg}")
                if failures:
                    json_traceback = "\n=== Structured Failure Traceback (from JSON report) ===\n" + "\n\n".join(failures) + "\n=======================================================\n"
            except Exception as e:
                log(f"Failed to parse JSON report: {str(e)}")
        
        return False, res.stdout + "\n" + res.stderr + "\n" + json_traceback

def cleanup_files():
    # Walk all .py files in current directory (excluding .venv)
    for root, dirs, files in os.walk("."):
        parts = root.split(os.sep)
        if any((part.startswith(".") and part not in (".", "..")) or part == "node_modules" or part == ".venv" for part in parts if part):
            continue
            
        for file in files:
            if file.endswith(".py"):
                fpath = os.path.join(root, file)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                    
                    # Strip trailing backticks or markdown code blocks
                    lines = content.splitlines()
                    changed = False
                    while lines and (lines[-1].strip() == "```" or lines[-1].strip() == "```python" or not lines[-1].strip()):
                        lines.pop()
                        changed = True
                        
                    if changed:
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write("\n".join(lines) + "\n")
                        log(f"Cleaned up trailing backticks from {fpath}")
                except Exception as e:
                    log(f"Failed to cleanup {fpath}: {str(e)}")

# Builtins that allow arbitrary code execution and must always be blocked
_DANGEROUS_BUILTINS = frozenset({"eval", "exec", "compile"})

def _discover_local_modules():
    """Auto-discover local Python packages/modules in the current directory.

    Scans top-level directories (e.g. src/, tests/) for .py files and
    constructs dotted module names so that api_schema.yaml does not need
    to enumerate every internal file manually.

    Returns a set of allowed module name strings (e.g. {'src', 'src.pipeline'}).
    """
    local = set()
    skip_dirs = {".venv", "node_modules", "__pycache__"}
    try:
        for entry in os.scandir("."):
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name in skip_dirs:
                continue
            # Include directory itself as a top-level package name
            pkg_name = entry.name
            has_py = False
            try:
                for sub in os.scandir(entry.path):
                    if sub.is_file() and sub.name.endswith(".py"):
                        has_py = True
                        modname = sub.name[:-3]
                        local.add(f"{pkg_name}.{modname}")
            except PermissionError:
                continue
            if has_py:
                local.add(pkg_name)
    except Exception as e:
        log(f"Warning: local module discovery failed: {str(e)}")
    return local


def _is_allowed(mod, allowed):
    """Return True if `mod` matches any entry in `allowed` (exact or prefix)."""
    for pattern in allowed:
        if mod == pattern or mod.startswith(pattern + "."):
            return True
    return False


def validate_imports():
    schema_file = "api_schema.yaml"
    if not os.path.exists(schema_file):
        return True, "No schema file found. Skipping validation."

    try:
        with open(schema_file) as f:
            schema = yaml.safe_load(f)
    except Exception as e:
        log(f"Failed to parse api_schema.yaml: {str(e)}")
        return False, f"Failed to parse api_schema.yaml: {str(e)}"

    # Merge schema whitelist with auto-discovered local modules
    schema_allowed = schema.get("allowed_imports", [])
    local_modules = _discover_local_modules()
    allowed = list(set(schema_allowed) | local_modules)
    log(f"Allowed imports (schema): {sorted(schema_allowed)}")
    log(f"Allowed imports (local, auto-discovered): {sorted(local_modules)}")

    # Walk all .py files in current directory (excluding hidden dirs and .venv)
    for root, dirs, files in os.walk("."):
        parts = root.split(os.sep)
        if any(
            (part.startswith(".") and part not in (".", ".."))
            or part in ("node_modules", ".venv", "__pycache__")
            for part in parts if part
        ):
            continue

        for file in files:
            if not file.endswith(".py"):
                continue
            fpath = os.path.join(root, file)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    source = f.read()
                tree = ast.parse(source, filename=fpath)
            except SyntaxError as e:
                err_msg = f"COMPILER ERROR: Syntax error in '{fpath}': {str(e)}"
                log(err_msg)
                return False, err_msg
            except Exception as e:
                err_msg = f"COMPILER ERROR: Failed to parse '{fpath}': {str(e)}"
                log(err_msg)
                return False, err_msg

            for node in ast.walk(tree):
                # ── 1. Static imports (ast.Import / ast.ImportFrom) ─────────────
                imported_modules = []
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported_modules.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported_modules.append(node.module)

                for mod in imported_modules:
                    if not _is_allowed(mod, allowed):
                        err_msg = (
                            f"COMPILER ERROR: Unauthorized import of '{mod}' "
                            f"detected in '{fpath}'. "
                            f"Allowed modules: {sorted(schema_allowed)}."
                        )
                        log(err_msg)
                        return False, err_msg

                # ── 2. Dynamic import calls ─────────────────────────────────────
                if isinstance(node, ast.Call):
                    func = node.func

                    # Pattern: __import__('module')
                    if isinstance(func, ast.Name) and func.id == "__import__":
                        if node.args and isinstance(node.args[0], ast.Constant):
                            mod = str(node.args[0].value)
                            if not _is_allowed(mod, allowed):
                                err_msg = (
                                    f"COMPILER ERROR: Dynamic __import__('{mod}') "
                                    f"is unauthorized in '{fpath}'. "
                                    f"Allowed modules: {sorted(schema_allowed)}."
                                )
                                log(err_msg)
                                return False, err_msg
                        else:
                            # Non-literal argument — module name is computed at
                            # runtime and cannot be statically verified.
                            err_msg = (
                                f"COMPILER ERROR: Non-literal __import__() call "
                                f"detected in '{fpath}'. "
                                f"Dynamic module names are not permitted."
                            )
                            log(err_msg)
                            return False, err_msg

                    # Pattern: importlib.import_module('module')
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == "import_module"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "importlib"
                    ):
                        if node.args and isinstance(node.args[0], ast.Constant):
                            mod = str(node.args[0].value)
                            if not _is_allowed(mod, allowed):
                                err_msg = (
                                    f"COMPILER ERROR: Dynamic importlib.import_module('{mod}') "
                                    f"is unauthorized in '{fpath}'. "
                                    f"Allowed modules: {sorted(schema_allowed)}."
                                )
                                log(err_msg)
                                return False, err_msg
                        else:
                            err_msg = (
                                f"COMPILER ERROR: Non-literal importlib.import_module() call "
                                f"detected in '{fpath}'. "
                                f"Dynamic module names are not permitted."
                            )
                            log(err_msg)
                            return False, err_msg

                    # ── 3. Dangerous built-in functions ─────────────────────────
                    # eval(), exec(), compile() enable arbitrary code execution
                    # and are unconditionally forbidden regardless of imports.
                    if isinstance(func, ast.Name) and func.id in _DANGEROUS_BUILTINS:
                        err_msg = (
                            f"COMPILER ERROR: Dangerous builtin '{func.id}()' "
                            f"detected in '{fpath}'. "
                            f"eval/exec/compile are unconditionally forbidden."
                        )
                        log(err_msg)
                        return False, err_msg

    return True, "All imports validated."

def run_cleanup():
    if os.path.exists(".git/index.lock"):
        log("Removing stale .git/index.lock")
        try:
            os.remove(".git/index.lock")
        except Exception as e:
            log(f"Failed to remove .git/index.lock: {str(e)}")

def main():
    # Load OPENROUTER_API_KEY from .zshrc if not in environment
    if "OPENROUTER_API_KEY" not in os.environ:
        zsh_rc = os.path.expanduser("~/.zshrc")
        if os.path.exists(zsh_rc):
            try:
                val = subprocess.check_output(
                    ["zsh", "-c", f"source {zsh_rc} && echo $OPENROUTER_API_KEY"],
                    text=True
                ).strip()
                if val:
                    os.environ["OPENROUTER_API_KEY"] = val
            except Exception:
                pass

    # If version check, forward directly
    if "--version" in sys.argv:
        res = subprocess.run([REAL_AIDER, "--version"], capture_output=True, text=True)
        sys.stdout.write(res.stdout)
        sys.stderr.write(res.stderr)
        sys.exit(res.returncode)
        
    # Read stdin
    instructions = sys.stdin.read()
    
    # Initialize log
    with open(LOG_FILE, "w") as f:
        f.write("=== Orchestrator Session Started ===\n")
    log(f"Working Directory: {os.getcwd()}")
    log(f"Arguments: {sys.argv[1:]}")
    log(f"Instructions Length: {len(instructions)}")
    
    run_cleanup()
    
    # Automatically add files from .ai-knowledge/ and verified_examples/ as read-only references
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

    # Initial Aider pass
    temp_msg_file = ".aider.msg.temp"
    try:
        with open(temp_msg_file, "w") as f:
            f.write(instructions)
    except Exception as e:
        log(f"Failed to create temp message file: {str(e)}")
        sys.exit(1)

    aider_cmd = [REAL_AIDER] + extra_args + ["--message-file", temp_msg_file] + sys.argv[1:]
    log(f"Starting initial Aider pass with command: {' '.join(aider_cmd)}")
    res = subprocess.run(aider_cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    
    # Clean up temp message file
    if os.path.exists(temp_msg_file):
        try:
            os.remove(temp_msg_file)
        except Exception:
            pass
            
    # Capture Aider stdout/stderr
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    
    if res.returncode != 0:
        log(f"Initial Aider pass failed with code: {res.returncode}")
        sys.exit(res.returncode)
        
    # Self-healing loop
    max_retries = 3
    retries = 0
    
    while retries < max_retries:
        run_cleanup()
        cleanup_files()
        import_success, import_err = validate_imports()
        if not import_success:
            success = False
            test_log = import_err
        else:
            success, test_log = run_tests()
            
        if success:
            log("All tests passed (or no tests found). Success!")
            sys.exit(0)
            
        retries += 1
        log(f"Test failure detected. Retrying correction ({retries}/{max_retries})...")
        
        repair_instructions = (
            f"The tests failed. Please fix the implementation to make the tests pass.\n"
            f"Note: If a constraint or constructor signature documented in `.ai-knowledge/` conflicts with the empirical runtime traceback (e.g. causes a TypeError or AttributeError), the runtime behavior takes absolute precedence. Real-world execution is the ground truth. Correct the code to match the actual Python behavior even if it violates the documented rule in `.ai-knowledge/`.\n\n"
            f"--- Test Output ---\n{test_log}"
        )
        
        try:
            with open(temp_msg_file, "w") as f:
                f.write(repair_instructions)
        except Exception as e:
            log(f"Failed to create temp message file for repair: {str(e)}")
            sys.exit(1)
            
        log("Running Aider for self-healing...")
        res = subprocess.run(aider_cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        
        # Clean up temp message file
        if os.path.exists(temp_msg_file):
            try:
                os.remove(temp_msg_file)
            except Exception:
                pass

        # Print Aider output
        sys.stdout.write(res.stdout)
        sys.stderr.write(res.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        
        if res.returncode != 0:
            log(f"Aider repair pass failed with code: {res.returncode}")
            sys.exit(res.returncode)
            
    log("Self-healing failed after max retries.")
    sys.exit(1)

if __name__ == "__main__":
    main()
