"""Tests for agent layer — BaseAgent, AgentRegistry, capability resolution.

Phase 3 adds:
- Capability-based registration and lookup
- execution_tier filtering
- Tier violation detection
"""

from __future__ import annotations

from typing import Any

import pytest

from ekp_forge.agents.base import BaseAgent
from ekp_forge.agents.registry import AgentRegistry
from ekp_forge.protocol.capability import (
    ROLE_REQUIRED_CAPABILITIES,
    ROLE_MINIMUM_TIER,
    Capability,
    CapabilityRegistry,
    TierViolationError,
)
from ekp_forge.protocol.roles import Role


# ---------------------------------------------------------------------------
# Test Agent Implementations
# ---------------------------------------------------------------------------


class DummyAgent(BaseAgent):
    """Minimal agent for testing."""

    agent_id: str = "dummy"
    capabilities: list[Capability] = [Capability.CODING, Capability.INTROSPECTION]
    execution_tier: str = "local"

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "agent_id": self.agent_id, "input_keys": list(context.keys())}


class AnotherAgent(BaseAgent):
    """Another agent for multi-agent tests."""

    agent_id: str = "another"
    capabilities: list[Capability] = [Capability.VERIFICATION]
    execution_tier: str = "local"

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "agent_id": self.agent_id}


class CloudAgent(BaseAgent):
    """Agent with cloud execution_tier for tier resolution tests."""

    agent_id: str = "cloud_agent"
    capabilities: list[Capability] = [
        Capability.PLANNING,
        Capability.RAG_SEARCH,
        Capability.ARCHITECTURE_REVIEW,
    ]
    execution_tier: str = "cloud"

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "agent_id": self.agent_id}


class FailingAgent(BaseAgent):
    """Agent that raises an exception — tests exception transparency."""

    agent_id: str = "failing"
    capabilities: list[Capability] = []
    execution_tier: str = "local"

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        msg = context.get("message", "Intentional failure")
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------


class TestBaseAgent:
    """Verify BaseAgent abstract interface."""

    def test_concrete_agent_can_be_instantiated(self) -> None:
        agent = DummyAgent()
        assert agent.agent_id == "dummy"

    def test_execute_returns_dict(self) -> None:
        agent = DummyAgent()
        result = agent.execute({"task": "test"})
        assert isinstance(result, dict)
        assert result["status"] == "ok"

    def test_abstract_class_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            BaseAgent()  # type: ignore[abstract]

    def test_execute_receives_context(self) -> None:
        agent = DummyAgent()
        result = agent.execute({"foo": "bar", "baz": 42})
        assert "foo" in result["input_keys"]
        assert "baz" in result["input_keys"]

    def test_exception_transparency(self) -> None:
        """Exceptions should propagate transparently through execute()."""
        agent = FailingAgent()
        with pytest.raises(RuntimeError, match="Intentional failure"):
            agent.execute({"message": "Intentional failure"})

    # ------------------------------------------------------------------
    # Phase 3: Capability helpers
    # ------------------------------------------------------------------

    def test_has_capability_true(self) -> None:
        agent = DummyAgent()
        assert agent.has_capability(Capability.CODING) is True
        assert agent.has_capability(Capability.INTROSPECTION) is True

    def test_has_capability_false(self) -> None:
        agent = DummyAgent()
        assert agent.has_capability(Capability.PLANNING) is False
        assert agent.has_capability(Capability.RAG_SEARCH) is False

    def test_has_all_capabilities_true(self) -> None:
        agent = DummyAgent()
        assert agent.has_all_capabilities([Capability.CODING, Capability.INTROSPECTION]) is True

    def test_has_all_capabilities_false(self) -> None:
        agent = DummyAgent()
        assert agent.has_all_capabilities([Capability.CODING, Capability.PLANNING]) is False

    def test_default_execution_tier(self) -> None:
        """Agents default to 'local' execution_tier."""
        agent = DummyAgent()
        assert agent.execution_tier == "local"

    def test_cloud_agent_execution_tier(self) -> None:
        agent = CloudAgent()
        assert agent.execution_tier == "cloud"


# ---------------------------------------------------------------------------
# Capability Enum & Role Mapping
# ---------------------------------------------------------------------------


class TestCapability:
    """Verify Capability enum and Role→Capability mapping."""

    def test_enum_values(self) -> None:
        assert Capability.CODING.value == "coding"
        assert Capability.INTROSPECTION.value == "introspection"
        assert Capability.RAG_SEARCH.value == "rag_search"

    def test_enum_members_are_unique(self) -> None:
        values = [c.value for c in Capability]
        assert len(values) == len(set(values))

    def test_role_required_capabilities_all_roles_mapped(self) -> None:
        """Every Role should be in ROLE_REQUIRED_CAPABILITIES."""
        for role in Role:
            assert role in ROLE_REQUIRED_CAPABILITIES, f"Missing mapping for {role}"

    def test_role_required_capabilities_non_empty(self) -> None:
        """Every Role should require at least one Capability."""
        for role in Role:
            caps = ROLE_REQUIRED_CAPABILITIES[role]
            assert len(caps) >= 1, f"Role {role} has empty capability list"

    def test_implementation_requires_coding_and_introspection(self) -> None:
        caps = ROLE_REQUIRED_CAPABILITIES[Role.IMPLEMENTATION]
        assert Capability.CODING in caps
        assert Capability.INTROSPECTION in caps

    def test_planning_requires_rag_search(self) -> None:
        caps = ROLE_REQUIRED_CAPABILITIES[Role.PLANNING]
        assert Capability.RAG_SEARCH in caps

    def test_role_minimum_tier_architecture_is_cloud(self) -> None:
        assert ROLE_MINIMUM_TIER[Role.ARCHITECTURE] == "cloud"

    def test_role_minimum_tier_implementation_is_local(self) -> None:
        assert ROLE_MINIMUM_TIER[Role.IMPLEMENTATION] == "local"


# ---------------------------------------------------------------------------
# CapabilityRegistry
# ---------------------------------------------------------------------------


class TestCapabilityRegistry:
    """Verify low-level CapabilityRegistry indexing."""

    def test_register_and_find_by_single_capability(self) -> None:
        reg = CapabilityRegistry()
        reg.register("agent_1", [Capability.CODING, Capability.INTROSPECTION])

        result = reg.find_agents_by_capability(Capability.CODING)
        assert "agent_1" in result

    def test_find_agents_for_exact_match(self) -> None:
        reg = CapabilityRegistry()
        reg.register("agent_1", [Capability.CODING, Capability.INTROSPECTION])
        reg.register("agent_2", [Capability.CODING])

        result = reg.find_agents_for([Capability.CODING, Capability.INTROSPECTION])
        assert "agent_1" in result
        assert "agent_2" not in result  # agent_2 lacks INTROSPECTION

    def test_find_agents_for_partial_match_excluded(self) -> None:
        reg = CapabilityRegistry()
        reg.register("agent_1", [Capability.CODING])
        reg.register("agent_2", [Capability.VERIFICATION])

        # No agent has both CODING and VERIFICATION
        result = reg.find_agents_for([Capability.CODING, Capability.VERIFICATION])
        assert len(result) == 0

    def test_has_capability(self) -> None:
        reg = CapabilityRegistry()
        reg.register("agent_1", [Capability.CODING])
        assert reg.has_capability("agent_1", Capability.CODING) is True
        assert reg.has_capability("agent_1", Capability.PLANNING) is False

    def test_has_all_capabilities(self) -> None:
        reg = CapabilityRegistry()
        reg.register("agent_1", [Capability.CODING, Capability.INTROSPECTION])
        assert reg.has_all_capabilities("agent_1", [Capability.CODING, Capability.INTROSPECTION]) is True
        assert reg.has_all_capabilities("agent_1", [Capability.CODING, Capability.PLANNING]) is False

    def test_unregister(self) -> None:
        reg = CapabilityRegistry()
        reg.register("agent_1", [Capability.CODING])
        reg.unregister("agent_1")
        assert "agent_1" not in reg
        assert reg.find_agents_by_capability(Capability.CODING) == []

    def test_get_capabilities_unknown_agent(self) -> None:
        reg = CapabilityRegistry()
        assert reg.get_capabilities("nonexistent") == []

    def test_to_dict(self) -> None:
        reg = CapabilityRegistry()
        reg.register("agent_1", [Capability.CODING, Capability.INTROSPECTION])
        d = reg.to_dict()
        assert "agent_1" in d
        assert "coding" in d["agent_1"]
        assert "introspection" in d["agent_1"]

    def test_empty_registry_len(self) -> None:
        reg = CapabilityRegistry()
        assert len(reg) == 0

    def test_find_agents_for_empty_required(self) -> None:
        """Empty required list returns all agents."""
        reg = CapabilityRegistry()
        reg.register("agent_1", [Capability.CODING])
        reg.register("agent_2", [Capability.VERIFICATION])
        result = reg.find_agents_for([])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# AgentRegistry — Name-based (Phase 1/2 backward compat)
# ---------------------------------------------------------------------------


class TestAgentRegistry:
    """Verify AgentRegistry registration and resolution."""

    def test_register_and_resolve(self) -> None:
        registry = AgentRegistry()
        agent = DummyAgent()
        registry.register(agent)

        resolved = registry.resolve("dummy")
        assert resolved is agent
        assert resolved.agent_id == "dummy"

    def test_resolve_nonexistent(self) -> None:
        registry = AgentRegistry()
        assert registry.resolve("nonexistent") is None

    def test_register_duplicate_raises(self) -> None:
        registry = AgentRegistry()
        registry.register(DummyAgent())
        with pytest.raises(ValueError, match="already registered"):
            registry.register(DummyAgent())

    def test_resolve_all(self) -> None:
        registry = AgentRegistry()
        agent1 = DummyAgent()
        agent2 = AnotherAgent()
        registry.register(agent1)
        registry.register(agent2)

        results = registry.resolve_all(["dummy", "another"])
        assert len(results) == 2
        assert results[0] is agent1
        assert results[1] is agent2

    def test_resolve_all_skips_missing(self) -> None:
        registry = AgentRegistry()
        registry.register(DummyAgent())

        results = registry.resolve_all(["dummy", "nonexistent"])
        assert len(results) == 1
        assert results[0].agent_id == "dummy"

    def test_unregister(self) -> None:
        registry = AgentRegistry()
        registry.register(DummyAgent())
        registry.unregister("dummy")
        assert registry.resolve("dummy") is None

    def test_list_agents(self) -> None:
        registry = AgentRegistry()
        registry.register(DummyAgent())
        registry.register(AnotherAgent())

        agents = registry.list_agents()
        assert "dummy" in agents
        assert "another" in agents
        assert len(agents) == 2

    def test_contains(self) -> None:
        registry = AgentRegistry()
        registry.register(DummyAgent())

        assert "dummy" in registry
        assert "nonexistent" not in registry

    def test_len(self) -> None:
        registry = AgentRegistry()
        assert len(registry) == 0
        registry.register(DummyAgent())
        assert len(registry) == 1
        registry.register(AnotherAgent())
        assert len(registry) == 2

    def test_to_dict(self) -> None:
        registry = AgentRegistry()
        registry.register(DummyAgent())
        registry.register(AnotherAgent())

        d = registry.to_dict()
        assert "dummy" in d
        assert "DummyAgent" in d["dummy"]
        assert "another" in d

    def test_repr(self) -> None:
        registry = AgentRegistry()
        registry.register(DummyAgent())
        registry.register(AnotherAgent())

        r = repr(registry)
        assert "DummyAgent" in r
        assert "AnotherAgent" in r
        assert "AgentRegistry" in r

    # ------------------------------------------------------------------
    # Phase 3: Capability-based resolution
    # ------------------------------------------------------------------

    def test_resolve_by_capability_exact_match(self) -> None:
        registry = AgentRegistry()
        agent = DummyAgent()  # CODING + INTROSPECTION
        registry.register(agent)

        resolved = registry.resolve_by_capability([Capability.CODING, Capability.INTROSPECTION])
        assert resolved is agent

    def test_resolve_by_capability_no_match(self) -> None:
        registry = AgentRegistry()
        registry.register(DummyAgent())  # CODING + INTROSPECTION

        resolved = registry.resolve_by_capability([Capability.PLANNING])
        assert resolved is None

    def test_resolve_by_capability_prefers_best_match(self) -> None:
        registry = AgentRegistry()

        class BroadAgent(BaseAgent):
            agent_id = "broad"
            capabilities = [Capability.CODING, Capability.INTROSPECTION, Capability.VERIFICATION]
            execution_tier = "local"
            def execute(self, ctx): return {"status": "ok"}

        class NarrowAgent(BaseAgent):
            agent_id = "narrow"
            capabilities = [Capability.CODING]
            execution_tier = "local"
            def execute(self, ctx): return {"status": "ok"}

        registry.register(BroadAgent())
        registry.register(NarrowAgent())

        # Both match CODING, but BroadAgent also has INTROSPECTION
        resolved = registry.resolve_by_capability([Capability.CODING, Capability.INTROSPECTION])
        assert resolved is not None
        assert resolved.agent_id == "broad"

    def test_resolve_all_by_capability(self) -> None:
        registry = AgentRegistry()
        agent1 = DummyAgent()  # CODING + INTROSPECTION
        agent2 = AnotherAgent()  # VERIFICATION

        class CodingAgent(BaseAgent):
            agent_id = "coder"
            capabilities = [Capability.CODING]
            execution_tier = "local"
            def execute(self, ctx): return {"status": "ok"}

        registry.register(agent1)
        registry.register(agent2)
        registry.register(CodingAgent())

        # Find all CODING agents
        results = registry.resolve_all_by_capability([Capability.CODING])
        assert len(results) == 2
        assert any(a.agent_id == "dummy" for a in results)
        assert any(a.agent_id == "coder" for a in results)

    def test_resolve_by_capability_with_tier_filter(self) -> None:
        registry = AgentRegistry()
        local_agent = DummyAgent()  # local
        cloud_agent = CloudAgent()  # cloud, has CODING... no, CloudAgent has PLANNING, RAG_SEARCH, ARCHITECTURE_REVIEW

        registry.register(local_agent)
        registry.register(cloud_agent)

        # Request PLANNING capability with cloud tier requirement
        resolved = registry.resolve_by_capability(
            [Capability.PLANNING, Capability.RAG_SEARCH],
            required_tier="cloud",
        )
        assert resolved is cloud_agent

    def test_resolve_by_capability_tier_mismatch_excluded(self) -> None:
        registry = AgentRegistry()
        local_agent = DummyAgent()  # local, no PLANNING anyway

        # Agent with CODING but local tier
        registry.register(local_agent)

        # Request with cloud tier — no match (DummyAgent has CODING but is local)
        resolved = registry.resolve_by_capability(
            [Capability.CODING],
            required_tier="cloud",
        )
        assert resolved is None

    def test_find_agents_by_capability(self) -> None:
        registry = AgentRegistry()
        agent1 = DummyAgent()  # CODING + INTROSPECTION
        registry.register(agent1)

        results = registry.find_agents_by_capability(Capability.CODING)
        assert len(results) >= 1
        assert agent1 in results

    def test_capability_registry_property(self) -> None:
        registry = AgentRegistry()
        registry.register(DummyAgent())
        cap_reg = registry.capability_registry
        assert isinstance(cap_reg, CapabilityRegistry)
        assert "dummy" in cap_reg


# ---------------------------------------------------------------------------
# TierViolationError
# ---------------------------------------------------------------------------


class TestTierViolationError:
    def test_error_message_format(self) -> None:
        err = TierViolationError(
            role=Role.ARCHITECTURE,
            agent_id="worker",
            agent_tier="local",
            required_tier="cloud",
        )
        assert "worker" in str(err)
        assert "local" in str(err)
        assert "cloud" in str(err)
        # Role.ARCHITECTURE.value == "Architecture" (StrEnum), not "ARCHITECTURE"
        assert "Architecture" in str(err)

    def test_error_attributes(self) -> None:
        err = TierViolationError(
            role=Role.ARCHITECTURE,
            agent_id="worker",
            agent_tier="local",
            required_tier="cloud",
        )
        assert err.role == Role.ARCHITECTURE
        assert err.agent_id == "worker"
        assert err.agent_tier == "local"
        assert err.required_tier == "cloud"
