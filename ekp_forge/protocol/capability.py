"""Capability schema and dynamic routing — Phase 3.

This module defines:

1. ``Capability`` as a ``StrEnum`` of well-known capability identifiers.
2. ``ROLE_REQUIRED_CAPABILITIES`` — maps each ``Role`` to the list of
   ``Capability`` values it requires for execution.
3. ``CapabilityRegistry`` — maps agents to their declared capabilities
   and supports capability-based lookup (used by ``AgentRegistry``).

Design:
- Capabilities are fixed at definition time (StrEnum), never modified at runtime.
- The ``ROLE_REQUIRED_CAPABILITIES`` table is the single source of truth
  for what capabilities each Role needs.
- ``CapabilityRegistry`` is a low-level index; the main ``AgentRegistry``
  wraps it for agent-level queries.
"""

from __future__ import annotations

from enum import StrEnum

from ekp_forge.protocol.roles import Role


# ---------------------------------------------------------------------------
# 1. Capability Enum — fixed, well-known capabilities
# ---------------------------------------------------------------------------


class Capability(StrEnum):
    """Standard capabilities understood by the protocol layer.

    Each capability represents a distinct skill or tool that an agent may
    provide. The dispatcher uses these values to match Role requirements
    to agent capabilities at runtime.
    """

    # Planning & Management
    REQUIREMENT_REVIEW = "requirement_review"
    PLANNING = "planning"
    ARCHITECTURE_REVIEW = "architecture_review"
    SPECIFICATION = "specification"

    # Execution
    CODING = "coding"
    VERIFICATION = "verification"
    INTEGRATION = "integration"
    ADVERSARIAL_REVIEW = "adversarial_review"  # Organizational-theory: independent edge-case audit

    # Phase 3: Dynamic Knowledge
    INTROSPECTION = "introspection"  # dir() / help() in sandbox
    RAG_SEARCH = "rag_search"  # knowledge base search (Manager)
    KNOWLEDGE_INGEST = "knowledge_ingest"  # PyPI doc ingestion

    # Execution Tier Hints
    LOCAL_EXECUTION = "local_execution"
    CLOUD_EXECUTION = "cloud_execution"


# ---------------------------------------------------------------------------
# 2. Role → Capability Mapping
# ---------------------------------------------------------------------------

# Each Role requires one or more Capabilities to be fulfilled.
# This is the single source of truth used by the Dispatcher.
ROLE_REQUIRED_CAPABILITIES: dict[Role, list[Capability]] = {
    Role.REQUIREMENT_REVIEW: [Capability.REQUIREMENT_REVIEW],
    Role.PLANNING: [Capability.PLANNING, Capability.RAG_SEARCH],
    Role.ARCHITECTURE: [Capability.ARCHITECTURE_REVIEW, Capability.RAG_SEARCH],
    Role.SPECIFICATION: [Capability.SPECIFICATION, Capability.RAG_SEARCH],
    Role.IMPLEMENTATION: [Capability.CODING, Capability.INTROSPECTION],
    Role.VERIFICATION: [Capability.VERIFICATION],
    Role.INTEGRATION: [Capability.INTEGRATION],
}

# Minimum execution_tier required for each Role.
# Agents with execution_tier below this level will be skipped during dispatch.
ROLE_MINIMUM_TIER: dict[Role, str] = {
    Role.REQUIREMENT_REVIEW: "local",
    Role.PLANNING: "local",
    Role.ARCHITECTURE: "cloud",  # Architecture reviews need cloud-tier context
    Role.SPECIFICATION: "local",
    Role.IMPLEMENTATION: "local",
    Role.VERIFICATION: "local",
    Role.INTEGRATION: "local",
}


# ---------------------------------------------------------------------------
# 3. Tier Violation Error
# ---------------------------------------------------------------------------


class TierViolationError(ValueError):
    """Raised when an agent's ``execution_tier`` is insufficient for a Role.

    This is caught by ``WorkflowEngine`` which may attempt auto-escalation
    to a higher-tier agent before propagating.
    """

    def __init__(
        self,
        role: Role,
        agent_id: str,
        agent_tier: str,
        required_tier: str,
    ) -> None:
        self.role = role
        self.agent_id = agent_id
        self.agent_tier = agent_tier
        self.required_tier = required_tier
        super().__init__(
            f"Agent '{agent_id}' has execution_tier='{agent_tier}' "
            f"but Role {role.value} requires at least '{required_tier}'"
        )


# ---------------------------------------------------------------------------
# 4. CapabilityRegistry — low-level capability index
# ---------------------------------------------------------------------------


class CapabilityRegistry:
    """Maps agents to their capabilities — low-level index.

    This is used internally by ``AgentRegistry`` for capability-based
    matching. It is not meant to be used directly by consumers.

    Usage::

        cap_reg = CapabilityRegistry()
        cap_reg.register("agent_1", [Capability.CODING, Capability.INTROSPECTION])
        agents = cap_reg.find_agents_for([Capability.CODING, Capability.INTROSPECTION])
        # → ["agent_1"]
    """

    def __init__(self) -> None:
        self._agent_capabilities: dict[str, list[Capability]] = {}
        # Reverse index: capability → list of agent IDs
        self._capability_index: dict[Capability, list[str]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, agent_id: str, capabilities: list[Capability]) -> None:
        """Register capabilities for a given agent.

        Args:
            agent_id:     The agent identifier (must match ``AgentRegistry`` key).
            capabilities: List of ``Capability`` values this agent provides.
        """
        self._agent_capabilities[agent_id] = list(capabilities)
        for cap in capabilities:
            if cap not in self._capability_index:
                self._capability_index[cap] = []
            self._capability_index[cap].append(agent_id)

    def unregister(self, agent_id: str) -> None:
        """Remove an agent from the registry.

        Args:
            agent_id: The agent identifier to remove.
        """
        caps = self._agent_capabilities.pop(agent_id, [])
        for cap in caps:
            if cap in self._capability_index and agent_id in self._capability_index[cap]:
                self._capability_index[cap].remove(agent_id)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def find_agents_for(
        self,
        required_capabilities: list[Capability],
    ) -> list[str]:
        """Find agents that provide ALL required capabilities.

        Only agents that have **every** capability in the list are returned.
        Agents missing even one required capability are excluded.

        Args:
            required_capabilities: The capability values required.

        Returns:
            List of agent IDs that satisfy all requirements.
        """
        if not required_capabilities:
            return list(self._agent_capabilities.keys())

        # Start with candidates from the first capability
        first_cap = required_capabilities[0]
        candidates = set(self._capability_index.get(first_cap, []))

        # Intersect with remaining capabilities
        for cap in required_capabilities[1:]:
            candidates &= set(self._capability_index.get(cap, []))

        return list(candidates)

    def find_agents_by_capability(self, capability: Capability) -> list[str]:
        """Find agents that provide a single specific capability.

        Args:
            capability: The capability to search for.

        Returns:
            List of agent IDs that provide this capability.
        """
        return list(self._capability_index.get(capability, []))

    def get_capabilities(self, agent_id: str) -> list[Capability]:
        """Return the list of capabilities for a given agent.

        Args:
            agent_id: The agent identifier.

        Returns:
            List of ``Capability`` values, or empty list if agent not found.
        """
        return self._agent_capabilities.get(agent_id, [])

    def has_capability(self, agent_id: str, capability: Capability) -> bool:
        """Check if an agent has a specific capability.

        Args:
            agent_id:   The agent identifier.
            capability: The capability to check.

        Returns:
            True if the agent has this capability.
        """
        caps = self._agent_capabilities.get(agent_id, [])
        return capability in caps

    def has_all_capabilities(self, agent_id: str, capabilities: list[Capability]) -> bool:
        """Check if an agent has ALL specified capabilities.

        Args:
            agent_id:      The agent identifier.
            capabilities:  The capabilities to check.

        Returns:
            True if the agent has all capabilities.
        """
        caps = self._agent_capabilities.get(agent_id, [])
        agent_caps = set(caps)
        return agent_caps.issuperset(capabilities)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def list_agents(self) -> list[str]:
        """Return all registered agent IDs."""
        return list(self._agent_capabilities.keys())

    def to_dict(self) -> dict[str, list[str]]:
        """Return a summary dict for debugging."""
        return {aid: [c.value for c in caps] for aid, caps in self._agent_capabilities.items()}

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._agent_capabilities

    def __len__(self) -> int:
        return len(self._agent_capabilities)
