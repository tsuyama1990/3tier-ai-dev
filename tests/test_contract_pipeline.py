"""TDD tests for the Contract-Driven Pipeline.

Tests cover:
1. SPECIFICATION phase: Manager generates WorkerContract from task + plan
2. Worker slicing & execution: FunctionSlicer extracts/injects code
3. Semantic contract verification: Manager validates code against contract
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ekp_forge.manager import ManagerAgent
from ekp_forge.protocol.roles import Role
from ekp_forge.sandbox.slicer import FunctionSlicer
from ekp_forge.schemas.contract import WorkerContract


# ---------------------------------------------------------------------------
# Test Case 1: Specification Generation
# ---------------------------------------------------------------------------


def _make_specification_context() -> dict:
    """Create a minimal context for SPECIFICATION role testing."""
    from ekp_forge.schemas.task_schema import TaskSchema

    task = TaskSchema(
        task_id="T-20260628000000-test01",
        manager_id="MGR-Test-01",
        goal="Create a Calculator class with an add method.",
        constraints=["Use type hints", "File must be at test_output/calculator.py"],
        acceptance_tests=["Calculator().add(1, 2) == 3"],
        affected_modules=["test_output/calculator.py"],
        assumptions_required={},
        force_accept=True,
    )
    plan = (
        "# Implementation Plan\n"
        "## Goal\n"
        "Create Calculator class with add(a: int, b: int) -> int\n"
        "## Steps\n"
        "1. Create test_output/calculator.py with Calculator class\n"
        "2. Implement add method\n"
    )
    return {
        "_role": Role.SPECIFICATION,
        "task": task,
        "plan": plan,
    }


def test_specification_generation() -> None:
    """ManagerAgent generates a valid WorkerContract from task + plan."""
    manager = ManagerAgent(manager_id="MGR-Test-01")
    context = _make_specification_context()

    # Mock DeepSeek call to return a structured contract JSON
    mock_contract_json = json.dumps({
        "contract_id": "C-20260628000000-a1b2c3",
        "objective": "Create Calculator class with add method.",
        "target_files": ["test_output/calculator.py"],
        "editable_symbols": ["Calculator.add"],
        "forbidden_symbols": [],
        "acceptance_tests": ["Calculator().add(1, 2) == 3"],
        "implementation_steps": [
            "Create test_output/calculator.py with Calculator class",
            "Implement add method returning sum of two integers",
        ],
        "local_design_freedom": "within_file",
    })

    with patch.object(manager, "_call_deepseek", return_value=mock_contract_json):
        result = manager.execute(context)

    assert result["status"] == "accepted"
    contract = result.get("worker_contract")
    assert isinstance(contract, WorkerContract), f"Expected WorkerContract, got {type(contract)}"
    assert "test_output/calculator.py" in contract.target_files
    assert "Calculator.add" in contract.editable_symbols
    assert contract.objective == "Create Calculator class with add method."
    assert contract.local_design_freedom == "within_file"


def test_specification_generation_creates_skeleton() -> None:
    """SPECIFICATION phase writes skeleton code to target file."""
    manager = ManagerAgent(manager_id="MGR-Test-01")
    context = _make_specification_context()
    tmp_dir = Path("/tmp/test_contract_skeleton")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target_file = tmp_dir / "calculator.py"

    # Override affected_modules to point to tmp dir
    task = context["task"]
    task.affected_modules = [str(target_file)]

    mock_contract_json = json.dumps({
        "contract_id": "C-20260628000000-b2c3d4",
        "objective": "Create Calculator class with add method.",
        "target_files": [str(target_file)],
        "editable_symbols": ["Calculator.add"],
        "forbidden_symbols": [],
        "acceptance_tests": ["Calculator().add(1, 2) == 3"],
        "implementation_steps": [],
        "local_design_freedom": "within_file",
        "skeleton_code": (
            "class Calculator:\n"
            "    def add(self, a: int, b: int) -> int:\n"
            "        pass\n"
        ),
    })

    with patch.object(manager, "_call_deepseek", return_value=mock_contract_json):
        result = manager.execute(context)

    assert result["status"] == "accepted"
    assert target_file.exists(), f"Skeleton file was not created at {target_file}"
    content = target_file.read_text()
    assert "class Calculator:" in content
    assert "def add(self, a: int, b: int) -> int:" in content
    assert "pass" in content

    # Cleanup
    target_file.unlink(missing_ok=True)
    tmp_dir.rmdir()


# ---------------------------------------------------------------------------
# Test Case 2: Worker Slicing & Execution
# ---------------------------------------------------------------------------


def test_worker_slicing_extraction() -> None:
    """FunctionSlicer extracts a single method from skeleton code."""
    skeleton = (
        "class Calculator:\n"
        "    def add(self, a: int, b: int) -> int:\n"
        "        pass\n"
        "\n"
        "    def subtract(self, a: int, b: int) -> int:\n"
        "        pass\n"
    )
    slicer = FunctionSlicer()
    extracted = slicer.extract_function(skeleton, "Calculator.add")
    assert extracted is not None
    assert "def add(self, a: int, b: int) -> int:" in extracted
    assert "pass" in extracted
    assert "subtract" not in extracted


def test_worker_slicing_injection() -> None:
    """FunctionSlicer injects implemented function body back into skeleton."""
    skeleton = (
        "class Calculator:\n"
        "    def add(self, a: int, b: int) -> int:\n"
        "        pass\n"
    )
    implemented = (
        "    def add(self, a: int, b: int) -> int:\n"
        "        return a + b\n"
    )
    slicer = FunctionSlicer()
    merged = slicer.inject_fix(skeleton, "Calculator.add", implemented)
    assert merged is not None
    assert "return a + b" in merged
    assert "pass" not in merged
    assert "class Calculator:" in merged


def test_worker_slicing_roundtrip(tmp_path: Path) -> None:
    """Full roundtrip: extract → implement → inject preserves file integrity."""
    file_path = tmp_path / "calculator.py"
    skeleton = (
        "import typing\n"
        "\n"
        "\n"
        "class Calculator:\n"
        "    def add(self, a: int, b: int) -> int:\n"
        "        pass\n"
        "\n"
        "    def subtract(self, a: int, b: int) -> int:\n"
        "        return 0  # stub\n"
    )
    file_path.write_text(skeleton)

    slicer = FunctionSlicer()
    extracted = slicer.extract_function_from_file(str(file_path), "Calculator.add")
    assert extracted is not None

    # Worker implements the function body
    implemented = extracted.replace("pass", "return a + b")

    # Inject back
    ok = slicer.inject_fix_to_file(str(file_path), "Calculator.add", implemented)
    assert ok

    result = file_path.read_text()
    assert "return a + b" in result
    assert "class Calculator:" in result
    assert "def subtract" in result
    assert "return 0  # stub" in result

    # Verify syntax is valid
    import ast
    ast.parse(result)


# ---------------------------------------------------------------------------
# Test Case 3: Semantic Contract Verification
# ---------------------------------------------------------------------------


def _make_calculator_contract() -> WorkerContract:
    return WorkerContract(
        contract_id="C-20260628000000-a1b2c3",
        objective="Calculator with add method returning int.",
        target_files=["test_output/calculator.py"],
        editable_symbols=["Calculator.add"],
        acceptance_tests=["Calculator().add(1, 2) == 3"],
        implementation_steps=["Implement add method"],
        local_design_freedom="none",
    )


def test_contract_verification_compliant() -> None:
    """Manager approves code that matches the contract."""
    manager = ManagerAgent(manager_id="MGR-Test-01")
    contract = _make_calculator_contract()
    compliant_code = (
        "class Calculator:\n"
        "    def add(self, a: int, b: int) -> int:\n"
        "        return a + b\n"
    )

    mock_response = json.dumps({
        "compliant": True,
        "reasoning": "All contract requirements are met.",
        "issues": [],
    })

    with patch.object(manager, "_call_deepseek", return_value=mock_response):
        ok, feedback = manager.validate_contract_compliance(contract, compliant_code)

    assert ok is True
    assert feedback == ""


def test_contract_verification_non_compliant() -> None:
    """Manager rejects code that violates the contract."""
    manager = ManagerAgent(manager_id="MGR-Test-01")
    contract = _make_calculator_contract()
    non_compliant_code = (
        "class Calculator:\n"
        "    def add(self, a: str, b: str) -> str:\n"
        "        return a + b\n"
    )

    mock_response = json.dumps({
        "compliant": False,
        "reasoning": "Signature mismatch: expected (int, int) -> int, got (str, str) -> str",
        "issues": [
            "Method add() parameter 'a' should be int, got str",
            "Method add() parameter 'b' should be int, got str",
            "Return type should be int, got str",
        ],
    })

    with patch.object(manager, "_call_deepseek", return_value=mock_response):
        ok, feedback = manager.validate_contract_compliance(contract, non_compliant_code)

    assert ok is False
    assert "Signature mismatch" in feedback


def test_contract_verification_missing_method() -> None:
    """Manager rejects code missing required methods."""
    manager = ManagerAgent(manager_id="MGR-Test-01")
    contract = _make_calculator_contract()
    missing_code = (
        "class Calculator:\n"
        "    pass\n"
    )

    mock_response = json.dumps({
        "compliant": False,
        "reasoning": "Required method 'add' is missing from class Calculator.",
        "issues": ["Missing method: Calculator.add"],
    })

    with patch.object(manager, "_call_deepseek", return_value=mock_response):
        ok, feedback = manager.validate_contract_compliance(contract, missing_code)

    assert ok is False
    assert "missing" in feedback.lower() or "Missing" in feedback
