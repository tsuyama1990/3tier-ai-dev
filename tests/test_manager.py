"""Tests for manager.py — Manager Agent triage, validation, ADR generation, help request."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from manager import ManagerAgent
from schemas.task_schema import (
    ErrorChunkEntry,
    ErrorChunkSummary,
    EscalationReason,
    HelpRequestSchema,
    TaskSchema,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manager() -> ManagerAgent:
    return ManagerAgent(manager_id="MGR-Test-01")


@pytest.fixture
def valid_task() -> TaskSchema:
    return TaskSchema(
        task_id="T-20260622131903-a3f291",
        manager_id="MGR-Test-01",
        goal="Implement user authentication middleware",
        constraints=["No external libraries outside api_schema.yaml"],
        acceptance_tests=["Auth token is validated"],
        affected_modules=["src/middleware/auth.py"],
        assumptions_required={"api_schema_version": "v1.2"},
    )


# ---------------------------------------------------------------------------
# Triage — ACCEPT
# ---------------------------------------------------------------------------


class TestTriageAccept:
    def test_triage_accept(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """問題のない TaskSchema で ACCEPT が返ること"""
        with (
            patch.object(manager, "_check_assumptions", return_value=None),
            patch.object(manager, "_generate_implementation_plan", return_value="plan content"),
        ):
            status, result = manager.triage(valid_task)
            assert status == "ACCEPT"
            assert result == "plan content"

    def test_implementation_plan_contains_goal(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """Implementation Plan に goal が含まれていること"""
        with patch.object(manager, "_check_assumptions", return_value=None):
            status, plan = manager.triage(valid_task)
            assert status == "ACCEPT"
            assert valid_task.goal in plan
            assert valid_task.task_id in plan
            for mod in valid_task.affected_modules:
                assert mod in plan
            for c in valid_task.constraints:
                assert c in plan
            for t in valid_task.acceptance_tests:
                assert t in plan


# ---------------------------------------------------------------------------
# Triage — REJECT
# ---------------------------------------------------------------------------


class TestTriageReject:
    def test_triage_reject_assumption_violated(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """assumptions_required と api_schema.yaml の矛盾で REJECT が返ること"""
        # Save original api_schema.yaml to restore later
        schema_path = Path("api_schema.yaml")
        original_content = schema_path.read_text(encoding="utf-8") if schema_path.exists() else None
        try:
            # Write a restricted api_schema.yaml
            schema_path.write_text(yaml.dump({"allowed_imports": ["builtins"]}))
            # Add constraint that explicitly references importing a disallowed library
            valid_task.constraints = ["Do not import requests library"]
            status, reason = manager.triage(valid_task)
            assert status == "REJECT"
            assert "Assumption violated" in reason
        finally:
            # Restore original content
            if original_content is not None:
                schema_path.write_text(original_content, encoding="utf-8")
            elif schema_path.exists():
                schema_path.unlink()

    def test_triage_reject_invalid_schema(self) -> None:
        """Pydantic バリデーション失敗でエラーが発生すること"""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            TaskSchema(
                task_id="bad-id",
                manager_id="MGR",
                goal="x",
                constraints=[],
                acceptance_tests=[],
                affected_modules=[],
            )

    def test_triage_reject_adr_conflict(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """既存 ADR と assumptions が矛盾する場合 REJECT が返ること"""
        decisions_dir = Path("decisions")
        decisions_dir.mkdir(parents=True, exist_ok=True)
        adr_file = decisions_dir / "20260622_120000_T-001.md"
        adr_file.write_text(
            """# ADR: T-001 — Test

## 2. Assumptions (Machine Readable)
```json
{"api_schema_version": "v2.0"}
```

## 3. Decision
Test decision.
"""
        )
        try:
            status, reason = manager.triage(valid_task)
            assert status == "REJECT"
            assert "Assumption violated" in reason
            assert "v2.0" in reason
        finally:
            adr_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Validate Outcome — Approve
# ---------------------------------------------------------------------------


class TestValidateOutcomeApprove:
    def test_validate_outcome_approve(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """ハードコードのない clean な diff で True が返ること"""
        clean_diff = """diff --git a/src/middleware/auth.py b/src/middleware/auth.py
new file mode 100644
+def validate_token(token: str) -> bool:
+    return len(token) > 0
"""
        error_chunk = ErrorChunkSummary(task_id=valid_task.task_id)

        with (
            patch.object(manager, "_static_validation", return_value=""),
            patch.object(manager, "_llm_validation", return_value=""),
        ):
            approved, feedback = manager.validate_outcome(valid_task, clean_diff, error_chunk)
            assert approved is True
            assert feedback == ""


# ---------------------------------------------------------------------------
# Validate Outcome — Reject (Static)
# ---------------------------------------------------------------------------


class TestValidateOutcomeRejectStatic:
    def test_validate_outcome_reject_hardcode(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """assert True 単独のテストを含む diff で False が返ること"""
        bad_diff = """diff --git a/tests/test_auth.py b/tests/test_auth.py
+def test_auth():
+    assert True
"""
        error_chunk = ErrorChunkSummary(task_id=valid_task.task_id)
        approved, feedback = manager.validate_outcome(valid_task, bad_diff, error_chunk)
        assert approved is False
        assert "assert True" in feedback

    def test_validate_outcome_reject_constraint_violation(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """allowed_imports 外のライブラリを含む diff で False が返ること"""
        # Save original api_schema.yaml
        schema_path = Path("api_schema.yaml")
        original_content = schema_path.read_text(encoding="utf-8") if schema_path.exists() else None
        try:
            schema_path.write_text(yaml.dump({"allowed_imports": ["builtins", "typing"]}))
            violating_diff = """diff --git a/src/middleware/auth.py b/src/middleware/auth.py
+import requests
+def validate():
+    pass
"""
            error_chunk = ErrorChunkSummary(task_id=valid_task.task_id)
            approved, feedback = manager.validate_outcome(valid_task, violating_diff, error_chunk)
            assert approved is False
            assert "unauthorized import" in feedback.lower() or "constraint violation" in feedback.lower()
        finally:
            if original_content is not None:
                schema_path.write_text(original_content, encoding="utf-8")
            elif schema_path.exists():
                schema_path.unlink()

    def test_validate_outcome_detects_test_mode(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """TEST_MODE 条件分岐を含む diff で False が返ること"""
        bad_diff = """diff --git a/src/middleware/auth.py b/src/middleware/auth.py
+if TEST_MODE:
+    return True
"""
        error_chunk = ErrorChunkSummary(task_id=valid_task.task_id)
        approved, feedback = manager.validate_outcome(valid_task, bad_diff, error_chunk)
        assert approved is False
        assert "TEST_MODE" in feedback


# ---------------------------------------------------------------------------
# ADR Generation
# ---------------------------------------------------------------------------


class TestGenerateADR:
    def test_generate_adr_creates_file(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """ADR ファイルが decisions/ に生成されること"""
        error_chunk = ErrorChunkSummary(task_id=valid_task.task_id)
        path = manager.generate_adr(valid_task, error_chunk)
        assert path is not None
        assert Path(path).exists()
        assert "decisions" in path
        assert valid_task.task_id in path
        # Cleanup
        Path(path).unlink(missing_ok=True)

    def test_generate_adr_contains_assumptions(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """ADR に ## 2. Assumptions セクションが含まれること"""
        error_chunk = ErrorChunkSummary(task_id=valid_task.task_id)
        path = manager.generate_adr(valid_task, error_chunk)
        content = Path(path).read_text(encoding="utf-8")
        assert "## 2. Assumptions" in content
        assert "## 3. Decision" in content
        assert "## 4. Reflection" in content
        assert valid_task.goal in content
        # Cleanup
        Path(path).unlink(missing_ok=True)

    def test_generate_adr_with_error_chunk(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """ErrorChunkSummary の内容が ADR に反映されること"""
        error_chunk = ErrorChunkSummary(task_id=valid_task.task_id)
        error_chunk.add_entry(
            ErrorChunkEntry(attempt=1, error_type="AssertionError", module="test.py", action_taken="fixed")
        )
        error_chunk.add_entry(
            ErrorChunkEntry(attempt=2, error_type="TypeError", module="src/main.py", action_taken="fixed signature")
        )
        path = manager.generate_adr(valid_task, error_chunk, reflection_notes="Compromise: used str instead of int")
        content = Path(path).read_text(encoding="utf-8")
        assert "AssertionError" in content
        assert "TypeError" in content
        assert "Compromise" in content
        assert "fixed signature" in content
        # Cleanup
        Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Help Request Handling
# ---------------------------------------------------------------------------


class TestHandleHelpRequest:
    def test_handle_help_request_provide_context(self, manager: ManagerAgent) -> None:
        """missing_context で PROVIDE_CONTEXT が返ること"""
        help_req = HelpRequestSchema(
            task_id="T-001",
            reason=EscalationReason.CONTEXT_MISSING,
            confidence=0.5,
            needed_information=["AuthMiddleware"],
        )
        with patch.object(manager, "_search_additional_context", return_value="found context"):
            action, payload = manager.handle_help_request(help_req)
            assert action == "PROVIDE_CONTEXT"
            assert payload == "found context"

    def test_handle_help_request_reject_cyclic(self, manager: ManagerAgent) -> None:
        """cyclic_error で REJECT が返ること"""
        help_req = HelpRequestSchema(
            task_id="T-001",
            reason=EscalationReason.CYCLIC_ERROR,
            confidence=0.3,
        )
        action, payload = manager.handle_help_request(help_req)
        assert action == "REJECT"
        assert "Cyclic error" in payload

    def test_handle_help_request_reject_confidence_drop(self, manager: ManagerAgent) -> None:
        """confidence_drop で REJECT が返ること"""
        help_req = HelpRequestSchema(
            task_id="T-001",
            reason=EscalationReason.CONFIDENCE_DROP,
            confidence=0.4,
        )
        action, payload = manager.handle_help_request(help_req)
        assert action == "REJECT"
        assert "confidence" in payload.lower()

    def test_handle_help_request_context_not_found(self, manager: ManagerAgent) -> None:
        """missing_context だがコンテキストが見つからない場合 REJECT が返ること"""
        help_req = HelpRequestSchema(
            task_id="T-001",
            reason=EscalationReason.CONTEXT_MISSING,
            confidence=0.5,
            needed_information=["NonExistentClass"],
        )
        with patch.object(manager, "_search_additional_context", return_value=""):
            action, payload = manager.handle_help_request(help_req)
            assert action == "REJECT"


# ---------------------------------------------------------------------------
# Static Validation (unit-level)
# ---------------------------------------------------------------------------


class TestStaticValidation:
    def test_detects_assert_true(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """assert True を検出すること"""
        diff = "+    assert True"
        result = manager._static_validation(valid_task, diff)
        assert "assert True" in result

    def test_detects_test_mode(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """TEST_MODE を検出すること"""
        diff = "+if TEST_MODE:"
        result = manager._static_validation(valid_task, diff)
        assert "TEST_MODE" in result

    def test_clean_diff_no_findings(self, manager: ManagerAgent, valid_task: TaskSchema) -> None:
        """クリーンな diff で空文字が返ること"""
        diff = "+def foo(): pass"
        result = manager._static_validation(valid_task, diff)
        assert result == ""
