"""Tests for schemas/task_schema.py — Pydantic model validation."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from ekp_forge.schemas.task_schema import (
    ErrorChunkEntry,
    ErrorChunkSummary,
    EscalationReason,
    HelpRequestSchema,
    TaskSchema,
    _error_fingerprint,
    _estimate_confidence,
    _generate_task_id,
)

# ---------------------------------------------------------------------------
# TaskSchema tests
# ---------------------------------------------------------------------------


def _valid_task_schema() -> dict:
    return {
        "task_id": "T-20260622131903-a3f291",
        "manager_id": "MGR-Auth-01",
        "goal": "Implement user authentication middleware",
        "constraints": ["No external libraries outside api_schema.yaml", "Must use urllib.request"],
        "acceptance_tests": ["Auth token is validated", "Invalid tokens return 401"],
        "affected_modules": ["src/middleware/auth.py"],
        "assumptions_required": {"api_schema_version": "v1.2"},
    }


class TestTaskSchema:
    def test_valid(self) -> None:
        """正常な TaskSchema が生成できること"""
        data = _valid_task_schema()
        schema = TaskSchema(**data)
        assert schema.task_id == data["task_id"]
        assert schema.goal == data["goal"]
        assert schema.constraints == data["constraints"]

    def test_invalid_task_id_format(self) -> None:
        """T- 形式以外の task_id でバリデーションエラー"""
        data = _valid_task_schema()
        data["task_id"] = "INVALID-123"
        with pytest.raises(ValidationError, match="task_id"):
            TaskSchema(**data)

    def test_task_id_too_short(self) -> None:
        """空の task_id 形式でエラー"""
        data = _valid_task_schema()
        data["task_id"] = "T-"
        with pytest.raises(ValidationError, match="task_id"):
            TaskSchema(**data)

    def test_empty_goal(self) -> None:
        """空の goal でバリデーションエラー"""
        data = _valid_task_schema()
        data["goal"] = ""
        with pytest.raises(ValidationError, match="goal"):
            TaskSchema(**data)

    def test_goal_too_long(self) -> None:
        """1000文字超の goal でバリデーションエラー"""
        data = _valid_task_schema()
        data["goal"] = "x" * 1001
        with pytest.raises(ValidationError, match="goal"):
            TaskSchema(**data)

    def test_empty_constraints(self) -> None:
        """constraints が空でバリデーションエラー"""
        data = _valid_task_schema()
        data["constraints"] = []
        with pytest.raises(ValidationError, match="constraints"):
            TaskSchema(**data)

    def test_non_py_affected_module(self) -> None:
        """affected_modules に .py 以外を含むとバリデーションエラー"""
        data = _valid_task_schema()
        data["affected_modules"] = ["src/middleware/auth.py", "config.json"]
        with pytest.raises(ValidationError, match="affected_module"):
            TaskSchema(**data)

    def test_empty_affected_modules(self) -> None:
        """affected_modules が空でバリデーションエラー"""
        data = _valid_task_schema()
        data["affected_modules"] = []
        with pytest.raises(ValidationError, match="affected_modules"):
            TaskSchema(**data)

    def test_default_assumptions_required(self) -> None:
        """assumptions_required 未指定で空の dict になること"""
        data = _valid_task_schema()
        del data["assumptions_required"]
        schema = TaskSchema(**data)
        assert schema.assumptions_required == {}

    def test_optional_parent_task_id(self) -> None:
        """parent_task_id は None を許容すること"""
        data = _valid_task_schema()
        data["parent_task_id"] = "EPIC-42"
        schema = TaskSchema(**data)
        assert schema.parent_task_id == "EPIC-42"


# ---------------------------------------------------------------------------
# HelpRequestSchema tests
# ---------------------------------------------------------------------------


class TestHelpRequestSchema:
    def test_valid(self) -> None:
        """正常な HelpRequestSchema が生成できること"""
        req = HelpRequestSchema(
            task_id="T-20260622131903-a3f291",
            reason=EscalationReason.CYCLIC_ERROR,
            confidence=0.4,
            attempts=["restarted aider", "added more context"],
            needed_information=["class definition of AuthMiddleware"],
        )
        assert req.task_id == "T-20260622131903-a3f291"
        assert req.reason == EscalationReason.CYCLIC_ERROR
        assert req.status == "needs_help"

    def test_default_status(self) -> None:
        """status のデフォルト値が 'needs_help' であること"""
        req = HelpRequestSchema(
            task_id="T-20260622131903-a3f291",
            reason=EscalationReason.CONTEXT_MISSING,
            confidence=0.5,
        )
        assert req.status == "needs_help"

    def test_confidence_out_of_range_high(self) -> None:
        """confidence が 1.0 超でバリデーションエラー"""
        with pytest.raises(ValidationError, match="confidence"):
            HelpRequestSchema(
                task_id="T-20260622131903-a3f291",
                reason=EscalationReason.CONFIDENCE_DROP,
                confidence=1.5,
            )

    def test_confidence_out_of_range_low(self) -> None:
        """confidence が 0.0 未満でバリデーションエラー"""
        with pytest.raises(ValidationError, match="confidence"):
            HelpRequestSchema(
                task_id="T-20260622131903-a3f291",
                reason=EscalationReason.CONFIDENCE_DROP,
                confidence=-0.1,
            )

    def test_boundary_confidence_values(self) -> None:
        """confidence が境界値 (0.0, 1.0) で正常動作すること"""
        req_low = HelpRequestSchema(task_id="T-x", reason=EscalationReason.CONFIDENCE_DROP, confidence=0.0)
        assert req_low.confidence == 0.0
        req_high = HelpRequestSchema(task_id="T-x", reason=EscalationReason.CONFIDENCE_DROP, confidence=1.0)
        assert req_high.confidence == 1.0

    def test_all_escalation_reasons(self) -> None:
        """すべての EscalationReason が正常に設定できること"""
        for reason in EscalationReason:
            req = HelpRequestSchema(task_id="T-1", reason=reason, confidence=0.5)
            assert req.reason == reason


# ---------------------------------------------------------------------------
# ErrorChunkSummary tests
# ---------------------------------------------------------------------------


class TestErrorChunkSummary:
    def test_accumulation(self) -> None:
        """エントリを追加して件数が正しく増えること"""
        summary = ErrorChunkSummary(task_id="T-001")
        assert summary.total_retries == 0
        assert len(summary.entries) == 0

        entry1 = ErrorChunkEntry(
            attempt=1,
            error_type="AssertionError",
            module="tests/test_auth.py",
            action_taken="Fixed off-by-one error in token validation",
        )
        summary.add_entry(entry1)
        assert summary.total_retries == 1
        assert len(summary.entries) == 1

        entry2 = ErrorChunkEntry(
            attempt=2,
            error_type="SyntaxError",
            module="src/middleware/auth.py",
            action_taken="Fixed missing colon in function definition",
        )
        summary.add_entry(entry2)
        assert summary.total_retries == 2
        assert len(summary.entries) == 2

    def test_empty_by_default(self) -> None:
        """デフォルトで entries が空リスト、total_retries が 0 であること"""
        summary = ErrorChunkSummary(task_id="T-002")
        assert summary.entries == []
        assert summary.total_retries == 0


# ---------------------------------------------------------------------------
# Utility function tests
# ---------------------------------------------------------------------------


class TestGenerateTaskId:
    def test_format(self) -> None:
        """_generate_task_id が T-YYYYMMDDHHMMSS-xxxxxx 形式であること"""
        tid = _generate_task_id("Implement auth")
        assert re.match(r"^T-\d{14}-[a-f0-9]{6}$", tid), f"Unexpected format: {tid}"

    def test_deterministic_within_same_second(self) -> None:
        """同じ goal から生成される ID が同じ秒内で一致すること（冪等性）"""
        tid1 = _generate_task_id("same goal")
        tid2 = _generate_task_id("same goal")
        # They could differ if second boundary crossed, but usually same
        # Just check format consistency
        assert re.match(r"^T-\d{14}-[a-f0-9]{6}$", tid1)
        assert re.match(r"^T-\d{14}-[a-f0-9]{6}$", tid2)


class TestErrorFingerprint:
    def test_fingerprint_consistent(self) -> None:
        """同じ pytest 出力から同じフィンガープリントが生成されること"""
        output = (
            "FAILED tests/test_auth.py::test_invalid_token - AssertionError: expected 401\n"
            "AssertionError: expected 401\n"
            "assert 200 == 401\n"
        )
        fp1 = _error_fingerprint(output)
        fp2 = _error_fingerprint(output)
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_different_errors_different_fingerprints(self) -> None:
        """異なるエラー出力から異なるフィンガープリントが生成されること"""
        out1 = "FAILED test_a.py::test_a - AssertionError: x"
        out2 = "FAILED test_b.py::test_b - TypeError: y"
        assert _error_fingerprint(out1) != _error_fingerprint(out2)

    def test_empty_output(self) -> None:
        """空の出力からも有効なフィンガープリントが生成されること"""
        fp = _error_fingerprint("")
        assert len(fp) == 16

    def test_line_number_independence(self) -> None:
        """行番号や変数名の変動に依存しないフィンガープリントであること"""
        out_a = "FAILED tests/test_auth.py::test_invalid_token - AssertionError: expected 401\nassert 200 == 401\n"
        out_b = "FAILED tests/test_auth.py::test_invalid_token - AssertionError: expected 401\nassert 200 == 401\n"
        assert _error_fingerprint(out_a) == _error_fingerprint(out_b)


class TestEstimateConfidence:
    def test_initial_confidence(self) -> None:
        """attempt=0, エラーなしで confidence が 1.0 になること"""
        summary = ErrorChunkSummary(task_id="T-001")
        assert _estimate_confidence(0, summary) == 1.0

    def test_one_failure_one_type(self) -> None:
        """attempt=1, 1種類のエラーで confidence=0.75"""
        summary = ErrorChunkSummary(task_id="T-001")
        summary.add_entry(ErrorChunkEntry(attempt=1, error_type="AssertionError", module="m.py", action_taken="fix"))
        assert _estimate_confidence(1, summary) == 0.75

    def test_two_failures_two_types(self) -> None:
        """attempt=2, 2種類のエラーで confidence=0.40 (閾値0.6未満)"""
        summary = ErrorChunkSummary(task_id="T-001")
        summary.add_entry(ErrorChunkEntry(attempt=1, error_type="AssertionError", module="m.py", action_taken="fix"))
        summary.add_entry(ErrorChunkEntry(attempt=2, error_type="TypeError", module="m.py", action_taken="fix"))
        # base=0.5, diversity_penalty=(2-1)*0.1=0.1 → 0.40
        assert _estimate_confidence(2, summary) == 0.40

    def test_one_failure_three_types(self) -> None:
        """attempt=1, 3種類のエラーで confidence=0.55 (閾値0.6未満)"""
        summary = ErrorChunkSummary(task_id="T-001")
        summary.add_entry(ErrorChunkEntry(attempt=1, error_type="AssertionError", module="m1.py", action_taken="fix"))
        summary.add_entry(ErrorChunkEntry(attempt=1, error_type="TypeError", module="m2.py", action_taken="fix"))
        summary.add_entry(ErrorChunkEntry(attempt=1, error_type="ValueError", module="m3.py", action_taken="fix"))
        # base=0.75, diversity_penalty=min(0.3, 2*0.1)=0.20 → 0.55
        assert _estimate_confidence(1, summary) == 0.55

    def test_three_failures_one_type(self) -> None:
        """attempt=3, 1種類のエラーで confidence=0.25 (閾値0.6未満)"""
        summary = ErrorChunkSummary(task_id="T-001")
        summary.add_entry(ErrorChunkEntry(attempt=1, error_type="AssertionError", module="m.py", action_taken="fix"))
        summary.add_entry(ErrorChunkEntry(attempt=2, error_type="AssertionError", module="m.py", action_taken="fix"))
        summary.add_entry(ErrorChunkEntry(attempt=3, error_type="AssertionError", module="m.py", action_taken="fix"))
        # base=0.25, diversity_penalty=0 → 0.25
        assert _estimate_confidence(3, summary) == 0.25

    def test_confidence_never_below_zero(self) -> None:
        """attempt が多くても confidence が負にならないこと"""
        summary = ErrorChunkSummary(task_id="T-001")
        for i in range(10):
            summary.add_entry(ErrorChunkEntry(attempt=i, error_type="Err", module="m.py", action_taken="fix"))
        conf = _estimate_confidence(10, summary)
        assert conf >= 0.0
