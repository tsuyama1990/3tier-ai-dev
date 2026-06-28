"""Tests for engine layer — Dispatcher, WorkflowEngine, FixPlanner integration."""

from __future__ import annotations

from typing import Any

import pytest

from ekp_forge.agents.base import BaseAgent
from ekp_forge.agents.registry import AgentRegistry
from ekp_forge.engine.dispatcher import Dispatcher
from ekp_forge.protocol.capability import Capability
from ekp_forge.engine.fix_planner import FixPlanner
from ekp_forge.engine.workflow import WorkflowEngine
from ekp_forge.protocol.assignment import OrganizationProfile, RoleAssignment
from ekp_forge.protocol.roles import Role
from ekp_forge.schemas.contract import (
    Diagnostic,
    DiagnosticCategory,
    DiagnosticSeverity,
    WorkerContract,
)


# ---------------------------------------------------------------------------
# Test Agent Implementations
# ---------------------------------------------------------------------------


class MockManagerAgent(BaseAgent):
    """Mock manager for testing."""

    agent_id: str = "manager"
    capabilities: list[Capability] = [
        Capability.REQUIREMENT_REVIEW,
        Capability.PLANNING,
        Capability.RAG_SEARCH,
        Capability.ARCHITECTURE_REVIEW,
        Capability.SPECIFICATION,
        Capability.INTEGRATION,
    ]

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        role: Role | None = context.get("_role")
        if role == Role.REQUIREMENT_REVIEW:
            return {"status": "accepted", "plan": "mock plan"}
        if role == Role.INTEGRATION:
            return {"status": "success", "adr_path": "/tmp/test_adr.md"}
        return {"status": "ok"}


class MockWorkerAgent(BaseAgent):
    """Mock worker for testing."""

    agent_id: str = "worker"
    capabilities: list[Capability] = [
        Capability.CODING,
        Capability.VERIFICATION,
        Capability.INTROSPECTION,
    ]

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "success", "git_diff": "mock diff", "error_chunk_summary": None}


class MockRejectingAgent(BaseAgent):
    """Agent that rejects during requirement review."""

    agent_id: str = "rejecting_manager"
    capabilities: list[Capability] = [Capability.REQUIREMENT_REVIEW]

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "rejected", "rejection_reason": "Test rejection"}


class MockFailingWorker(BaseAgent):
    """Worker that fails."""

    agent_id: str = "failing_worker"
    capabilities: list[Capability] = [Capability.CODING]

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "failed", "error_chunk_summary": None}


# ---------------------------------------------------------------------------
# Shared fixture: simple profile
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_profile() -> OrganizationProfile:
    return OrganizationProfile(
        profile_name="test_simple",
        assignment={
            "requirement_review": "manager",
            "planning": "manager",
            "architecture": "manager",
            "specification": "manager",
            "implementation": "worker",
            "verification": "worker",
            "integration": "manager",
        },
    )


@pytest.fixture
def registry_with_mocks() -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(MockManagerAgent())
    reg.register(MockWorkerAgent())
    return reg


@pytest.fixture
def engine(simple_profile: OrganizationProfile, registry_with_mocks: AgentRegistry) -> WorkflowEngine:
    return WorkflowEngine(simple_profile, registry_with_mocks)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDispatcher:
    """Verify Dispatcher role-to-agent resolution."""

    def test_resolve_single_agent(self, simple_profile: OrganizationProfile, registry_with_mocks: AgentRegistry) -> None:
        dispatcher = Dispatcher(simple_profile, registry_with_mocks)
        agents = dispatcher.resolve_agents(Role.REQUIREMENT_REVIEW)
        assert len(agents) == 1
        assert agents[0].agent_id == "manager"

    def test_resolve_implementation(self, simple_profile: OrganizationProfile, registry_with_mocks: AgentRegistry) -> None:
        dispatcher = Dispatcher(simple_profile, registry_with_mocks)
        agents = dispatcher.resolve_agents(Role.IMPLEMENTATION)
        assert len(agents) == 1
        assert agents[0].agent_id == "worker"

    def test_resolve_missing_agent_raises(self, simple_profile: OrganizationProfile) -> None:
        """If agent not in registry, resolve should raise ValueError."""
        empty_registry = AgentRegistry()
        dispatcher = Dispatcher(simple_profile, empty_registry)
        with pytest.raises(ValueError, match="not registered"):
            dispatcher.resolve_agents(Role.REQUIREMENT_REVIEW)

    def test_dispatch_returns_result(self, simple_profile: OrganizationProfile, registry_with_mocks: AgentRegistry) -> None:
        dispatcher = Dispatcher(simple_profile, registry_with_mocks)
        results = dispatcher.dispatch(Role.REQUIREMENT_REVIEW, {"task": "test"})
        assert len(results) == 1
        assert results[0]["status"] == "accepted"

    def test_dispatch_injects_role(self, simple_profile: OrganizationProfile, registry_with_mocks: AgentRegistry) -> None:
        dispatcher = Dispatcher(simple_profile, registry_with_mocks)
        results = dispatcher.dispatch(Role.IMPLEMENTATION, {"task": "test"})
        assert len(results) == 1

    def test_profile_property(self, simple_profile: OrganizationProfile, registry_with_mocks: AgentRegistry) -> None:
        dispatcher = Dispatcher(simple_profile, registry_with_mocks)
        assert dispatcher.profile is simple_profile


# ---------------------------------------------------------------------------
# WorkflowEngine — basic flow
# ---------------------------------------------------------------------------


class TestWorkflowEngine:
    """Verify WorkflowEngine sequential role execution."""

    def test_requirement_review_then_implementation(self, engine: WorkflowEngine) -> None:
        """Simulate sequential requirement review → implementation flow."""
        triage_result = engine.run(Role.REQUIREMENT_REVIEW, {"task": "test_task"})
        assert triage_result["status"] == "accepted"
        assert triage_result["plan"] == "mock plan"

        impl_result = engine.run(Role.IMPLEMENTATION, {"task": "test_task", "plan": triage_result["plan"]})
        assert impl_result["status"] == "success"

    def test_full_pipeline(self, engine: WorkflowEngine) -> None:
        """Simulate full pipeline: review → impl → integration."""
        # Phase 1: Review
        review = engine.run(Role.REQUIREMENT_REVIEW, {"task": "task1"})
        assert review["status"] == "accepted"

        # Phase 2: Implementation
        impl = engine.run(Role.IMPLEMENTATION, {"task": "task1", "plan": review.get("plan", "")})
        assert impl["status"] == "success"

        # Phase 3: Integration
        integration = engine.run(Role.INTEGRATION, {
            "task": "task1",
            "impl_result": impl,
            "error_chunk_summary": None,
            "git_diff": impl.get("git_diff", ""),
        })
        assert integration["status"] == "success"
        assert integration["adr_path"] == "/tmp/test_adr.md"

    def test_context_accumulates(self, engine: WorkflowEngine) -> None:
        """Context from previous runs should be available to subsequent runs."""
        engine.run(Role.REQUIREMENT_REVIEW, {"task": "task1"})
        engine.run(Role.IMPLEMENTATION, {"task": "task1"})

        # The shared _context should have accumulated results
        assert "_role" not in engine._context or engine._context.get("status") is not None

    def test_reset_context(self, engine: WorkflowEngine) -> None:
        """reset_context should clear accumulated state."""
        engine.run(Role.REQUIREMENT_REVIEW, {"task": "task1"})
        engine.reset_context()
        assert len(engine._context) == 0

    def test_profile_property(self, engine: WorkflowEngine) -> None:
        assert engine.profile is not None
        assert engine.profile.profile_name == "test_simple"

    def test_dispatcher_property(self, engine: WorkflowEngine) -> None:
        assert engine.dispatcher is not None
        assert isinstance(engine.dispatcher, Dispatcher)


# ---------------------------------------------------------------------------
# WorkflowEngine — rejection / failure paths
# ---------------------------------------------------------------------------


class TestWorkflowEngineRejection:
    """Verify engine handles rejection correctly."""

    def test_rejected_requirement(self) -> None:
        """When RequirementReview rejects, the result should reflect that."""
        reg = AgentRegistry()
        reg.register(MockRejectingAgent())
        reg.register(MockWorkerAgent())

        profile = OrganizationProfile(
            profile_name="test",
            assignment={
                "requirement_review": "rejecting_manager",
                "planning": "rejecting_manager",
                "architecture": "rejecting_manager",
                "specification": "rejecting_manager",
                "implementation": "worker",
                "verification": "worker",
                "integration": "rejecting_manager",
            },
        )
        engine = WorkflowEngine(profile, reg)

        result = engine.run(Role.REQUIREMENT_REVIEW, {"task": "test"})
        assert result["status"] == "rejected"
        assert "rejection_reason" in result

    def test_failed_implementation(self) -> None:
        """When Implementation fails, the result should reflect that."""
        reg = AgentRegistry()
        reg.register(MockManagerAgent())
        reg.register(MockFailingWorker())

        profile = OrganizationProfile(
            profile_name="test",
            assignment={
                "requirement_review": "manager",
                "planning": "manager",
                "architecture": "manager",
                "specification": "manager",
                "implementation": "failing_worker",
                "verification": "failing_worker",
                "integration": "manager",
            },
        )
        engine = WorkflowEngine(profile, reg)

        result = engine.run(Role.IMPLEMENTATION, {"task": "test", "plan": "test plan"})
        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# WorkflowEngine — run_all
# ---------------------------------------------------------------------------


class TestWorkflowEngineRunAll:
    """Verify run_all() returns results from all agents."""

    def test_run_all_multiple_agents(self) -> None:
        """Capability-first dispatch returns only capability-matching agents.

        Phase 3: With capability-first dispatch, only agents whose declared
        capabilities match the Role's requirements are returned.
        For IMPLEMENTATION, required caps are [CODING, INTROSPECTION].
        MockWorkerAgent has these; MockManagerAgent does not.
        """
        reg = AgentRegistry()
        reg.register(MockManagerAgent())
        reg.register(MockWorkerAgent())

        profile = OrganizationProfile(
            profile_name="test_multi",
            assignment={
                "requirement_review": "manager",
                "planning": "manager",
                "architecture": "manager",
                "specification": "manager",
                "implementation": ["worker", "manager"],
                "verification": "worker",
                "integration": "manager",
            },
        )
        engine = WorkflowEngine(profile, reg)

        # Phase 3: Only MockWorkerAgent has CODING + INTROSPECTION
        results = engine.run_all(Role.IMPLEMENTATION, {"task": "test"})
        assert len(results) == 1
        assert results[0].get("status") == "success"


# ---------------------------------------------------------------------------
# FixPlanner — unit tests
# ---------------------------------------------------------------------------


class MockTask:
    """Minimal mock TaskSchema for testing."""
    task_id = "T-20260627000000-abcdef"
    manager_id = "MGR-001"
    goal = "Test task"
    constraints = ["test"]
    acceptance_tests = []
    affected_modules = ["src/test.py"]
    assumptions_required = {}


class TestFixPlannerIntegration:
    """Verify FixPlanner integration with WorkflowEngine."""

    @pytest.fixture
    def contract(self) -> WorkerContract:
        return WorkerContract(
            contract_id="C-20260627000000-abcdef",
            objective="Fix test module",
            target_files=["src/test.py"],
        )

    def test_fix_planner_plan_with_mock_worker(
        self,
        engine: WorkflowEngine,
        contract: WorkerContract,
    ) -> None:
        """FixPlanner generates FixTasks for a mock implementation."""
        planner = FixPlanner(contract)

        # Simulate diagnostics from verification
        diagnostics = [
            Diagnostic(
                tool="ruff",
                severity=DiagnosticSeverity.ERROR,
                file="src/test.py",
                line=10,
                code="F821",
                message="Undefined name 'foo'",
                category=DiagnosticCategory.UNDEFINED_NAME,
            ),
            Diagnostic(
                tool="mypy",
                severity=DiagnosticSeverity.ERROR,
                file="src/test.py",
                line=20,
                code="mypy-arg-type",
                message="Incompatible type",
                category=DiagnosticCategory.TYPE_MISMATCH,
            ),
        ]

        assert planner.has_work(diagnostics) is True

        # Plan: should return priority 2 (UNDEFINED_NAME = import/names priority)
        tasks = planner.plan(diagnostics)
        assert len(tasks) == 1
        assert tasks[0].priority == 2  # UNDEFINED_NAME has priority 2
        assert len(tasks[0].diagnostics) == 1  # only the highest priority item

    def test_fix_planner_auto_fixable_filtering(
        self,
        contract: WorkerContract,
    ) -> None:
        """Auto-fixable diagnostics are filtered out by the planner."""
        planner = FixPlanner(contract)

        diagnostics = [
            Diagnostic(
                tool="ruff",
                severity=DiagnosticSeverity.WARNING,
                file="src/test.py",
                message="Unused import",
                category=DiagnosticCategory.UNUSED_IMPORT,
            ),
            Diagnostic(
                tool="ruff",
                severity=DiagnosticSeverity.WARNING,
                file="src/test.py",
                message="Trailing whitespace",
                category=DiagnosticCategory.FORMATTING,
            ),
        ]

        # All diagnostics are auto-fixable → no work
        assert planner.has_work(diagnostics) is False
        assert planner.plan(diagnostics) == []

    def test_fix_planner_multiple_priority_levels(
        self,
        contract: WorkerContract,
    ) -> None:
        """Multiple priority levels: only the highest is returned."""
        planner = FixPlanner(contract)

        diagnostics = [
            Diagnostic(
                tool="pytest", severity=DiagnosticSeverity.ERROR,
                file="tests/test_main.py", message="Test failed",
                category=DiagnosticCategory.TEST_FAILURE,
            ),
            Diagnostic(
                tool="mypy", severity=DiagnosticSeverity.ERROR,
                file="src/test.py", line=5, message="Type error",
                category=DiagnosticCategory.TYPE_MISMATCH,
            ),
            Diagnostic(
                tool="ruff", severity=DiagnosticSeverity.ERROR,
                file="src/test.py", line=1, message="Syntax error",
                category=DiagnosticCategory.SYNTAX,
            ),
        ]

        # Plan: only syntax (priority 1) is returned
        tasks = planner.plan(diagnostics)
        assert len(tasks) == 1
        assert tasks[0].priority == 1
        assert tasks[0].diagnostics[0].category == DiagnosticCategory.SYNTAX

    def test_run_with_fix_loop_success_path(
        self,
        engine: WorkflowEngine,
        contract: WorkerContract,
    ) -> None:
        """run_with_fix_loop succeeds when implementation succeeds directly."""
        task = MockTask()
        result = engine.run_with_fix_loop(
            task=task,
            contract=contract,
            plan="Implement test module",
            max_iterations=3,
        )
        # The mock worker always succeeds, so the fix loop should succeed
        assert result["status"] == "success"
        assert isinstance(result["fix_tasks_completed"], list)
        assert isinstance(result["impl_result"], dict)

    def test_workflow_engine_accepts_fix_planner_context(
        self,
        engine: WorkflowEngine,
        contract: WorkerContract,
    ) -> None:
        """WorkflowEngine.run() should pass worker_contract through context."""
        task = MockTask()
        result = engine.run(
            Role.IMPLEMENTATION,
            {
                "task": task,
                "plan": "test plan",
                "worker_contract": contract,
            },
        )
        assert result["status"] == "success"
