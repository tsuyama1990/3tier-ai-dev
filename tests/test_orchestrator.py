import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import importlib.util
import os
import unittest

HAS_YAML = importlib.util.find_spec("yaml") is not None

if HAS_YAML:
    from orchestrator import run_cleanup, run_tests, validate_imports
else:
    run_cleanup = None  # type: ignore[assignment]
    run_tests = None  # type: ignore[assignment]
    validate_imports = None  # type: ignore[assignment]


@unittest.skipUnless(HAS_YAML, "Requires PyYAML library")
class TestOrchestrator(unittest.TestCase):
    def test_run_cleanup(self):
        # Create a dummy index.lock
        lock_dir = ".git"
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, "index.lock")
        with open(lock_path, "w") as f:
            f.write("lock")

        self.assertTrue(os.path.exists(lock_path))
        run_cleanup()
        self.assertFalse(os.path.exists(lock_path))

    def test_run_tests(self):
        # Prevent infinite recursion when run_tests() runs pytest
        if os.environ.get("IN_ORCHESTRATOR_TEST"):
            self.skipTest("Skipping to prevent infinite recursion under orchestrator test run")

        os.environ["IN_ORCHESTRATOR_TEST"] = "1"
        try:
            success, test_log = run_tests()
            self.assertTrue(success)
            self.assertIn("test_calc.py", test_log)
        finally:
            del os.environ["IN_ORCHESTRATOR_TEST"]

    def test_validate_imports_violations(self):
        import shutil
        import tempfile

        # Create temporary directory to act as the project root
        temp_dir = tempfile.mkdtemp()
        orig_cwd = os.getcwd()
        os.chdir(temp_dir)
        try:
            # 1. Create allowed api_schema.yaml
            schema_content = """
allowed_imports:
  - math
  - sys
"""
            with open("api_schema.yaml", "w") as f:
                f.write(schema_content)

            # 2. Create a clean file with allowed imports
            with open("good_module.py", "w") as f:
                f.write("import math\nimport sys\n")

            success, msg = validate_imports()
            self.assertTrue(success, f"Expected success but got: {msg}")

            # 3. Create a violating file with unauthorized import
            with open("bad_import.py", "w") as f:
                f.write("import os\n")

            success, msg = validate_imports()
            self.assertFalse(success)
            self.assertIn("Unauthorized import of 'os'", msg)

            # Remove the bad import file
            os.remove("bad_import.py")

            # 4. Create a violating file using forbidden eval()
            with open("bad_eval.py", "w") as f:
                f.write("eval('1 + 1')\n")

            success, msg = validate_imports()
            self.assertFalse(success)
            self.assertIn("Dangerous builtin 'eval()'", msg)

        finally:
            os.chdir(orig_cwd)
            shutil.rmtree(temp_dir)


if __name__ == "__main__":
    unittest.main()
