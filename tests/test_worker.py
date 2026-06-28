"""Tests for worker.py — Worker Agent verification loop and escalation policy."""

from __future__ import annotations

from typing import Generator
from unittest.mock import patch

import pytest

from ekp_forge.schemas.contract import (
    Diagnostic,
    DiagnosticCategory,
    DiagnosticSeverity,
)
from ekp_forge.schemas.task_schema import (
    EscalationReason,
    TaskSchema,
)
from ekp_forge.worker import WorkerAgent


# Helper to create a test diagnostic
def _make_diag(category: DiagnosticCategory = DiagnosticCategory.TEST_FAILURE, msg: str = "failure") -> Diagnostic:
    return Diagnostic(
        tool="pytest",
        severity=DiagnosticSeverity.ERROR,
        file="tests/test_auth.py",
        code="AssertionError",
        message=msg,
        category=category,
    )

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def task_schema() -> TaskSchema:
    return TaskSchema(
        task_id="T-20260622131903-a3f291",
        manager_id="MGR-Auth-01",
        goal="Implement user authentication middleware",
        constraints=["No external libraries outside api_schema.yaml"],
        acceptance_tests=["Auth token is validated"],
        affected_modules=["src/middleware/auth.py"],
    )


@pytest.fixture
def worker() -> WorkerAgent:
    return WorkerAgent(
        model="ollama/qwen2.5-coder:7b",
        max_retries=3,
        escalation_confidence_threshold=0.6,
    )


@pytest.fixture(autouse=True)
def mock_ruff_mypy() -> Generator[None, None, None]:
    with (
        patch("ekp_forge.worker.setup_ruff_mypy"),
        patch("ekp_forge.worker.run_verification_pipeline", return_value=[]),
        patch("ekp_forge.sandbox.scoped_lint._changed_files", return_value=[]),
    ):
        yield


# ---------------------------------------------------------------------------
# Verification Loop — Success path
# ---------------------------------------------------------------------------


class TestVerificationLoopSuccess:
    def test_success_path(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """Aider + pytest 成功時に status='success' が返ること"""
        with (
            patch.object(worker, "_run_aider", return_value=(True, "aider ok")),
            patch.object(worker, "_validate_imports", return_value=(True, "ok")),
            patch.object(worker, "_run_pytest", return_value=(True, "all passed")),
            patch.object(
                worker, "_get_git_diff", return_value="diff --git a/src/middleware/auth.py b/src/middleware/auth.py"
            ),
        ):
            result = worker.execute_verification_loop(task_schema, "plan")
            assert result["status"] == "success"
            assert result["retries"] == 1
            assert result["help_request"] is None
            assert "diff" in result["git_diff"]

    def test_success_after_retry(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """2回目の試行で成功した場合も status='success' が返ること"""
        call_count = [0]

        def _verification_side_effect(**kwargs: object) -> list[Diagnostic]:
            call_count[0] += 1
            if call_count[0] == 1:
                return [_make_diag(msg="FAILED tests/test_auth.py::test_auth - AssertionError")]
            return []

        with (
            patch.object(worker, "_run_aider", return_value=(True, "aider ok")),
            patch.object(worker, "_validate_imports", return_value=(True, "ok")),
            patch("ekp_forge.worker.run_verification_pipeline", side_effect=_verification_side_effect),
            patch.object(worker, "_get_git_diff", return_value="diff --git a/file.py b/file.py"),
        ):
            result = worker.execute_verification_loop(task_schema, "plan")
            assert result["status"] == "success"
            assert result["retries"] == 2


# ---------------------------------------------------------------------------
# Verification Loop — Failure path
# ---------------------------------------------------------------------------


class TestVerificationLoopFailure:
    def test_max_retries_exceeded(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """pytest が常に失敗する場合、max_retries 後に status='failed' が返ること"""
        # Lower threshold to avoid confidence drop escalation before max_retries
        worker.escalation_confidence_threshold = 0.0
        # Use different error messages each time to avoid cyclic error detection
        error_messages = [
            "FAILED test_1.py - AssertionError: x",
            "FAILED test_2.py - TypeError: y",
            "FAILED test_3.py - ValueError: z",
        ]
        call_idx = [0]

        def _verification_side_effect(**kwargs: object) -> list[Diagnostic]:
            idx = call_idx[0]
            call_idx[0] += 1
            return [_make_diag(msg=error_messages[idx])]

        with (
            patch.object(worker, "_run_aider", return_value=(True, "aider ok")),
            patch.object(worker, "_validate_imports", return_value=(True, "ok")),
            patch("ekp_forge.worker.run_verification_pipeline", side_effect=_verification_side_effect),
            patch.object(worker, "_git_rollback") as mock_rollback,
        ):
            result = worker.execute_verification_loop(task_schema, "plan")
            assert result["status"] == "failed"
            assert result["retries"] == worker.max_retries
            assert result["help_request"] is None
            mock_rollback.assert_called_once()

    def test_aider_failure_breaks_loop(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """Aider が失敗した場合、ループを break して status='failed' が返ること"""
        with (
            patch.object(worker, "_run_aider", return_value=(False, "Aider failed")),
            patch.object(worker, "_git_rollback") as mock_rollback,
        ):
            result = worker.execute_verification_loop(task_schema, "plan")
            assert result["status"] == "failed"
            mock_rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Escalation Policy
# ---------------------------------------------------------------------------


class TestEscalationCyclicError:
    def test_cyclic_error_detection(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """同じエラーが2回連続で status='escalated' / reason='cyclic_error'"""
        diag = _make_diag(msg="FAILED tests/test_auth.py::test_auth - AssertionError: expected 401")

        def _verification_side_effect(**kwargs: object) -> list[Diagnostic]:
            return [diag]

        with (
            patch.object(worker, "_run_aider", return_value=(True, "aider ok")),
            patch.object(worker, "_validate_imports", return_value=(True, "ok")),
            patch("ekp_forge.worker.run_verification_pipeline", side_effect=_verification_side_effect),
            patch.object(worker, "_git_rollback"),
        ):
            result = worker.execute_verification_loop(task_schema, "plan")
            assert result["status"] == "escalated"
            assert result["help_request"] is not None
            assert result["help_request"].reason == EscalationReason.CYCLIC_ERROR


class TestEscalationContextMissing:
    def test_context_missing_attribute_error(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """AttributeError で status='escalated' / reason='missing_context'"""
        # The escalation policy checks for "AttributeError" in the compressed error log
        diag = _make_diag(msg="AttributeError: module 'X' has no attribute 'Y'")

        with (
            patch.object(worker, "_run_aider", return_value=(True, "aider ok")),
            patch.object(worker, "_validate_imports", return_value=(True, "ok")),
            patch("ekp_forge.worker.run_verification_pipeline", return_value=[diag]),
            patch.object(worker, "_git_rollback"),
        ):
            result = worker.execute_verification_loop(task_schema, "plan")
            assert result["status"] == "escalated"
            assert result["help_request"] is not None
            assert result["help_request"].reason == EscalationReason.CONTEXT_MISSING

    def test_context_missing_module_not_found(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """ModuleNotFoundError で status='escalated' / reason='missing_context'"""
        diag = _make_diag(msg="ModuleNotFoundError: No module named 'nonexistent'")

        with (
            patch.object(worker, "_run_aider", return_value=(True, "aider ok")),
            patch.object(worker, "_validate_imports", return_value=(True, "ok")),
            patch("ekp_forge.worker.run_verification_pipeline", return_value=[diag]),
            patch.object(worker, "_git_rollback"),
        ):
            result = worker.execute_verification_loop(task_schema, "plan")
            assert result["status"] == "escalated"
            assert result["help_request"] is not None
            assert result["help_request"].reason == EscalationReason.CONTEXT_MISSING


class TestEscalationConfidenceDrop:
    def test_confidence_drop_after_three_retries(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """attempt=3 で confidence < threshold となり status='escalated'"""
        # Use high threshold and different errors each time to avoid cyclic detection
        worker.escalation_confidence_threshold = 0.7

        error_messages = [
            "FAILED test_1.py - ValueError: a",
            "FAILED test_2.py - TypeError: b",
            "FAILED test_3.py - RuntimeError: c",
        ]
        call_idx = [0]

        def _verification_side_effect(**kwargs: object) -> list[Diagnostic]:
            idx = call_idx[0]
            call_idx[0] += 1
            return [_make_diag(msg=error_messages[idx])]

        with (
            patch.object(worker, "_run_aider", return_value=(True, "aider ok")),
            patch.object(worker, "_validate_imports", return_value=(True, "ok")),
            patch("ekp_forge.worker.run_verification_pipeline", side_effect=_verification_side_effect),
            patch.object(worker, "_git_rollback"),
        ):
            result = worker.execute_verification_loop(task_schema, "plan")
            assert result["status"] == "escalated"
            assert result["help_request"] is not None
            assert result["help_request"].reason == EscalationReason.CONFIDENCE_DROP
            assert result["help_request"].confidence < worker.escalation_confidence_threshold


# ---------------------------------------------------------------------------
# Error Chunk Accumulation
# ---------------------------------------------------------------------------


class TestErrorChunkAccumulation:
    def test_error_chunk_accumulates(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """各試行でエラーが ErrorChunkSummary に蓄積されること"""
        diag = _make_diag(msg="FAILED test - AssertionError")

        with (
            patch.object(worker, "_run_aider", return_value=(True, "aider ok")),
            patch.object(worker, "_validate_imports", return_value=(True, "ok")),
            patch("ekp_forge.worker.run_verification_pipeline", return_value=[diag]),
            patch.object(worker, "_git_rollback"),
        ):
            result = worker.execute_verification_loop(task_schema, "plan")
            assert result["status"] == "failed" or result["status"] == "escalated"
            summary = result["error_chunk_summary"]
            assert summary.total_retries > 0
            assert len(summary.entries) > 0
            # Each entry should have required fields
            for entry in summary.entries:
                assert entry.attempt >= 1
                assert entry.error_type
                assert entry.module
                assert entry.action_taken


# ---------------------------------------------------------------------------
# Git Rollback
# ---------------------------------------------------------------------------


class TestGitRollback:
    def test_git_reset_on_failure(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """最終失敗後に git reset --hard HEAD が呼ばれること"""
        with (
            patch.object(worker, "_run_aider", return_value=(False, "Aider failed")),
            patch.object(worker, "_git_rollback") as mock_rollback,
        ):
            worker.execute_verification_loop(task_schema, "plan")
            mock_rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Reflection Log
# ---------------------------------------------------------------------------


class TestReflectionLog:
    def test_reflection_log_updated_on_completion(self, worker: WorkerAgent, task_schema: TaskSchema) -> None:
        """成功・失敗問わず Reflection Log が更新されること"""
        with (
            patch.object(worker, "_run_aider", return_value=(True, "aider ok")),
            patch.object(worker, "_validate_imports", return_value=(True, "ok")),
            patch.object(worker, "_run_pytest", return_value=(True, "all passed")),
            patch.object(worker, "_get_git_diff", return_value="diff"),
            patch.object(worker, "_update_reflection_log") as mock_reflect,
        ):
            result = worker.execute_verification_loop(task_schema, "plan")
            assert result["status"] == "success"
            mock_reflect.assert_called_once()


# ---------------------------------------------------------------------------
# Helper method tests
# ---------------------------------------------------------------------------


class TestClassifyError:
    def test_classify_assertion_error(self) -> None:
        output = "FAILED test_a.py::test_b - AssertionError: x"
        assert WorkerAgent._classify_error(output) == "AssertionError"

    def test_classify_failed_line(self) -> None:
        output = "FAILED test_a.py::test_b - TypeError: y"
        assert WorkerAgent._classify_error(output) == "TypeError"

    def test_classify_fallback(self) -> None:
        output = "Some random output without error markers"
        assert WorkerAgent._classify_error(output) == "UnknownError"


class TestErrorModule:
    def test_extract_from_affected_modules(self) -> None:
        output = "FAILED src/middleware/auth.py::test_auth - Error"
        modules = ["src/middleware/auth.py"]
        assert WorkerAgent._error_module(output, modules) == "src/middleware/auth.py"

    def test_extract_from_failed_line(self) -> None:
        output = "FAILED tests/test_auth.py::test_auth - Error"
        modules = ["src/other.py"]
        assert "tests/test_auth.py" in WorkerAgent._error_module(output, modules)

    def test_fallback_unknown(self) -> None:
        """対応するモジュールが見つからない場合 unknown が返ること"""
        output = "no module info here"
        assert WorkerAgent._error_module(output, []) == "unknown"


class TestCompressErrorLog:
    def test_compress_error_log_short(self) -> None:
        """短いエラーログはそのまま返されること"""
        agent = WorkerAgent()
        text = "Short error log"
        assert agent._compress_error_log(text) == text

    def test_compress_error_log_filtering(self) -> None:
        """長いログから FAILED や Traceback などの重要行が抽出されること"""
        agent = WorkerAgent()
        lines = [
            "verbose setup output line 1",
            "verbose setup output line 2",
            "Traceback (most recent call last):",
            '  File "test.py", line 10, in test_foo',
            "    assert 1 == 2",
            "AssertionError: assert 1 == 2",
            "verbose teardown output line 1",
            "verbose teardown output line 2",
        ] * 20  # Make it long enough (>1500 chars)
        text = "\n".join(lines)
        compressed = agent._compress_error_log(text)
        assert len(compressed) < len(text)
        assert "Traceback" in compressed
        assert "AssertionError" in compressed
        assert "verbose setup output line 1" not in compressed


# ---------------------------------------------------------------------------
# Phase 4.5: Mode dispatch tests
# ---------------------------------------------------------------------------


from ekp_forge.protocol.roles import Role


class TestExecutionModeDispatch:
    """Tests for research/production mode dispatch in WorkerAgent.execute()."""

    def test_default_mode_uses_production_path(self) -> None:
        """execution_mode が指定されていない場合、production パスが使われること"""
        agent = WorkerAgent()
        task_schema_id = _make_task().task_id  # create once for reuse
        with patch.object(agent, "_run_with_worktree") as mock_prod:
            mock_prod.return_value = {"status": "success", "retries": 0}
            # No execution_mode → defaults to "production"
            result = agent.execute({
                "_role": Role.IMPLEMENTATION,
                "task": _make_task(),
                "plan": "test plan",
            })
            assert result["status"] == "success"
            mock_prod.assert_called_once()

    def test_research_mode_calls_research_method(self) -> None:
        """research モードでは _execute_research_mode が呼ばれること"""
        agent = WorkerAgent()
        with patch.object(agent, "_execute_research_mode") as mock_research:
            mock_research.return_value = {"status": "success", "retries": 0}
            result = agent.execute({
                "_role": Role.IMPLEMENTATION,
                "task": _make_task(),
                "plan": "test plan",
                "execution_mode": "research",
            })
            assert result["status"] == "success"
            mock_research.assert_called_once()

    def test_production_mode_calls_worktree_method(self) -> None:
        """production モードでは _run_with_worktree が呼ばれること"""
        agent = WorkerAgent()
        with patch.object(agent, "_run_with_worktree") as mock_prod:
            mock_prod.return_value = {"status": "success", "retries": 0}
            result = agent.execute({
                "_role": Role.IMPLEMENTATION,
                "task": _make_task(),
                "plan": "test plan",
                "execution_mode": "production",
            })
            assert result["status"] == "success"
            mock_prod.assert_called_once()


def _make_task() -> TaskSchema:
    """Create a minimal valid TaskSchema for testing."""
    from ekp_forge.schemas.task_schema import _generate_task_id

    return TaskSchema(
        task_id=_generate_task_id("test"),
        manager_id="MGR-Test",
        goal="test execution mode dispatch",
        constraints=["test constraint"],
        acceptance_tests=["test acceptance"],
        affected_modules=["test.py"],
    )
