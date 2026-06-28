"""Dispatcher — resolves Role → agent(s) via capability or name.

Phase 1 provides a static dispatcher that uses the RoleAssignment mapping
directly.

Phase 3 upgrades dispatch to **capability-first** resolution:
1. Look up the Role's required capabilities from ``ROLE_REQUIRED_CAPABILITIES``.
2. Query ``AgentRegistry.resolve_by_capability()`` with those capabilities.
3. If capability resolution yields results, use them.
4. Fall back to name-based resolution (Phase 1/2 backward compatibility).

The dispatcher is intentionally stateless; all state is managed by the
WorkflowEngine.
"""

from __future__ import annotations

from typing import Any

from ekp_forge.agents.base import BaseAgent
from ekp_forge.agents.registry import AgentRegistry
from ekp_forge.protocol.assignment import OrganizationProfile
from ekp_forge.protocol.capability import (
    ROLE_MINIMUM_TIER,
    ROLE_REQUIRED_CAPABILITIES,
    TierViolationError,
)
from ekp_forge.protocol.roles import Role


class Dispatcher:
    """Resolves a Role to concrete agent instances.

    This is the core dispatch mechanism that connects the Assignment layer
    (what should be done) to the Agent layer (who should do it).

    Phase 3: Capability-first dispatch with tier enforcement.
    """

    def __init__(self, profile: OrganizationProfile, registry: AgentRegistry) -> None:
        """Initialize the dispatcher.

        Args:
            profile:  The active ``OrganizationProfile`` containing ``RoleAssignment``.
            registry: The ``AgentRegistry`` containing all registered agents.
        """
        self._profile = profile
        self._registry = registry

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve_agents(self, role: Role) -> list[BaseAgent]:
        """Resolve a Role to a list of agent instances.

        Resolution order (Phase 3):
        1. Look up the Role's required capabilities from the mapping table.
        2. Try capability-based resolution with tier enforcement.
        3. If capability resolution succeeds, return matched agents.
        4. Fall back to name-based resolution (Phase 1/2 backward compat).

        Args:
            role: The role to dispatch.

        Returns:
            List of ``BaseAgent`` instances assigned to this role.

        Raises:
            TierViolationError: If capability resolution finds agents but
                their ``execution_tier`` is insufficient for the Role.
            ValueError: If no agent can be resolved by any method.
        """
        # Phase 3: Capability-first resolution
        required_caps = ROLE_REQUIRED_CAPABILITIES.get(role)
        required_tier = ROLE_MINIMUM_TIER.get(role)

        if required_caps:
            agents = self._registry.resolve_all_by_capability(
                required_caps,
                required_tier=required_tier,
            )
            if agents:
                return agents

            # Check if there are agents with the right capabilities but wrong tier
            wrong_tier_agents = self._registry.resolve_all_by_capability(
                required_caps,
                required_tier=None,
            )
            if wrong_tier_agents and required_tier:
                # Found agents with capabilities but insufficient tier
                example = wrong_tier_agents[0]
                raise TierViolationError(
                    role=role,
                    agent_id=example.agent_id,
                    agent_tier=example.execution_tier,
                    required_tier=required_tier,
                )

        # Fallback to name-based (Phase 1/2 backward compat)
        agent_names = self._profile.assignment.resolve(role)
        agents = self._registry.resolve_all(agent_names)

        # Validate that all names resolved
        resolved_names = {a.agent_id for a in agents}
        missing = [name for name in agent_names if name not in resolved_names]
        if missing:
            raise ValueError(
                f"Cannot dispatch role {role.value}: "
                f"agent(s) {missing} not registered. "
                f"Registered agents: {self._registry.list_agents()}"
            )

        return agents

    def dispatch(self, role: Role, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Dispatch a role to its assigned agent(s) and collect results.

        This is a convenience method that combines ``resolve_agents()``
        and ``agent.execute()``.

        Args:
            role:    The role to execute.
            context: Context dict passed to each agent's ``execute()``.

        Returns:
            List of result dicts, one per agent. If multiple agents are
            assigned to the same role, results are returned in assignment
            order.

        Raises:
            TierViolationError: If agent tier is insufficient for the Role.
            ValueError: If no agent is registered.
            Propagates all agent exceptions transparently.
        """
        agents = self.resolve_agents(role)
        context["_role"] = role
        results: list[dict[str, Any]] = []
        for agent in agents:
            result = agent.execute(context)
            results.append(result)
        return results

    @property
    def profile(self) -> OrganizationProfile:
        """Return the active organization profile."""
        return self._profile
