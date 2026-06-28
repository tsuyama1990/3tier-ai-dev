"""AgentRegistry — central registry of all available agent instances.

The registry maps agent identifier strings to ``BaseAgent`` instances.
It is populated at startup (typically in ``mcp_server.py``) and queried
by the ``WorkflowEngine`` dispatcher at runtime.

Phase 3 adds:
- ``_capability_registry``: A ``CapabilityRegistry`` for capability-based lookup.
- ``resolve_by_capability()``: Find best agent matching ALL required capabilities
  and execution_tier requirements.
- ``resolve_all_by_capability()``: Find ALL agents matching required capabilities.
"""

from __future__ import annotations

from typing import Any

from ekp_forge.agents.base import BaseAgent
from ekp_forge.protocol.capability import Capability, CapabilityRegistry, TierViolationError


class AgentRegistry:
    """Central registry of all available agents.

    Supports both name-based (Phase 1/2) and capability-based (Phase 3) resolution.
    When capability-based resolution fails to find a match, callers fall back to
    name-based resolution.

    Usage::

        registry = AgentRegistry()
        registry.register(ManagerAgent(agent_id="manager", manager_id="MGR-01"))
        registry.register(WorkerAgent(agent_id="worker"))

        # Name-based (Phase 1/2)
        agent = registry.resolve("manager")

        # Capability-based (Phase 3)
        agent = registry.resolve_by_capability([Capability.CODING, Capability.INTROSPECTION])
    """

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}
        # Phase 3: Capability index for dynamic dispatch
        self._capability_registry = CapabilityRegistry()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, agent: BaseAgent) -> None:
        """Register an agent instance.

        Also indexes the agent's capabilities in the capability registry
        for capability-based lookup.

        Args:
            agent: A ``BaseAgent``-compatible instance with a unique ``agent_id``.

        Raises:
            ValueError: If an agent with the same ``agent_id`` is already
                        registered.
        """
        if agent.agent_id in self._agents:
            raise ValueError(f"Agent '{agent.agent_id}' is already registered")
        self._agents[agent.agent_id] = agent
        # Phase 3: Index capabilities
        if hasattr(agent, "capabilities") and agent.capabilities:
            self._capability_registry.register(agent.agent_id, agent.capabilities)

    def unregister(self, agent_id: str) -> None:
        """Remove an agent from the registry.

        Args:
            agent_id: The agent identifier to remove.
        """
        if agent_id in self._agents:
            del self._agents[agent_id]
            self._capability_registry.unregister(agent_id)

    # ------------------------------------------------------------------
    # Name-based Resolution (Phase 1/2 backward compatibility)
    # ------------------------------------------------------------------

    def resolve(self, agent_name: str) -> BaseAgent | None:
        """Resolve a single agent by name.

        Args:
            agent_name: The agent identifier string (e.g. ``"manager"``).

        Returns:
            The ``BaseAgent`` instance, or ``None`` if not found.
        """
        return self._agents.get(agent_name)

    def resolve_all(self, agent_names: list[str]) -> list[BaseAgent]:
        """Resolve multiple agents by name.

        Args:
            agent_names: List of agent identifier strings.

        Returns:
            List of ``BaseAgent`` instances in the same order as input.
            Unknown names are silently skipped.
        """
        return [self._agents[name] for name in agent_names if name in self._agents]

    # ------------------------------------------------------------------
    # Phase 3: Capability-based Resolution
    # ------------------------------------------------------------------

    def resolve_by_capability(
        self,
        required_capabilities: list[Capability],
        required_tier: str | None = None,
    ) -> BaseAgent | None:
        """Find the best agent matching ALL required capabilities.

        Matching logic:
        1. Filter agents that have ALL required capabilities.
        2. If ``required_tier`` is provided, filter to agents whose
           ``execution_tier`` meets or exceeds the requirement.
           The tier hierarchy is: ``local`` < ``cloud``.
        3. If multiple agents match, prefer the one with highest total
           confidence (sum of individual capability confidences).
        4. Returns ``None`` if no agent satisfies all requirements.

        Args:
            required_capabilities: List of ``Capability`` values needed.
            required_tier:         Minimum ``execution_tier`` required
                                   (``"local"`` or ``"cloud"``). ``None``
                                   means no tier filtering.

        Returns:
            A ``BaseAgent`` instance, or ``None`` if no match found.
        """
        # Step 1: Find agents with ALL required capabilities
        agent_ids = self._capability_registry.find_agents_for(required_capabilities)
        if not agent_ids:
            return None

        # Step 2: Filter by execution_tier
        candidates: list[BaseAgent] = []
        for aid in agent_ids:
            agent = self._agents.get(aid)
            if agent is None:
                continue
            if required_tier and not self._tier_satisfies(agent.execution_tier, required_tier):
                continue
            candidates.append(agent)

        if not candidates:
            return None

        # Step 3: Prefer agent with highest capability count match
        # (agents with more matching capabilities are preferred)
        def _match_score(agent: BaseAgent) -> int:
            required_set = set(required_capabilities)
            agent_set = set(agent.capabilities)
            return len(required_set & agent_set)

        candidates.sort(key=_match_score, reverse=True)
        return candidates[0]

    def resolve_all_by_capability(
        self,
        required_capabilities: list[Capability],
        required_tier: str | None = None,
    ) -> list[BaseAgent]:
        """Find ALL agents matching the required capabilities.

        Args:
            required_capabilities: List of ``Capability`` values needed.
            required_tier:         Minimum ``execution_tier`` required.
                                   ``None`` means no tier filtering.

        Returns:
            List of ``BaseAgent`` instances that satisfy all requirements.
            Empty list if no matches found.
        """
        agent_ids = self._capability_registry.find_agents_for(required_capabilities)
        if not agent_ids:
            return []

        result: list[BaseAgent] = []
        for aid in agent_ids:
            agent = self._agents.get(aid)
            if agent is None:
                continue
            if required_tier and not self._tier_satisfies(agent.execution_tier, required_tier):
                continue
            result.append(agent)

        return result

    def find_agents_by_capability(self, capability: Capability) -> list[BaseAgent]:
        """Find all agents that provide a single specific capability.

        Args:
            capability: The ``Capability`` to search for.

        Returns:
            List of ``BaseAgent`` instances providing this capability.
        """
        agent_ids = self._capability_registry.find_agents_by_capability(capability)
        return [self._agents[aid] for aid in agent_ids if aid in self._agents]

    # ------------------------------------------------------------------
    # Tier validation
    # ------------------------------------------------------------------

    @staticmethod
    def _tier_satisfies(agent_tier: str, required_tier: str) -> bool:
        """Check if an agent's tier satisfies a requirement.

        Tier hierarchy: ``local`` (0) < ``cloud`` (1).
        An agent satisfies a requirement if its tier level >= required level.

        Args:
            agent_tier:    The agent's ``execution_tier`` value.
            required_tier: The minimum required tier.

        Returns:
            True if the agent's tier meets or exceeds the requirement.
        """
        tier_level = {"local": 0, "cloud": 1}
        return tier_level.get(agent_tier, 0) >= tier_level.get(required_tier, 0)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def list_agents(self) -> list[str]:
        """Return a list of all registered agent IDs."""
        return list(self._agents.keys())

    def to_dict(self) -> dict[str, str]:
        """Return a summary dict of registered agents for logging/debugging.

        Includes agent type, capabilities, and execution tier.
        """
        return {
            aid: (f"{type(agent).__name__}(caps={[c.value for c in agent.capabilities]}, tier={agent.execution_tier})")
            for aid, agent in self._agents.items()
        }

    @property
    def capability_registry(self) -> CapabilityRegistry:
        """Return the underlying capability registry (for inspection)."""
        return self._capability_registry

    def __contains__(self, agent_name: str) -> bool:
        return agent_name in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def __repr__(self) -> str:
        agents = ", ".join(f"{aid}: {type(a).__name__}(tier={a.execution_tier})" for aid, a in self._agents.items())
        return f"AgentRegistry({agents})"
