"""Abstract base class for all agents in the EKP-Forge system.

All agents (ManagerAgent, WorkerAgent, future specialized agents) must
implement the ``BaseAgent`` interface to be compatible with the
``AgentRegistry`` and ``WorkflowEngine``.

CRITICAL DESIGN RULES:
1. **Exception Transparency**: Exceptions MUST propagate transparently
   through ``execute()``. Do NOT catch-and-wrap exceptions here.
   Let ``AiderExecutionError``, ``Pydantic ValidationError``, etc. bubble
   up to ``WorkflowEngine`` and ``mcp_server.py`` so escalation policies
   work correctly.

2. **No Circular Imports**: ``BaseAgent`` MUST NOT import from
   ``ekp_forge.engine`` or ``ekp_forge.protocol``.

3. **Context Contract**: Each role defines its expected context keys.
   See the docstring of ``execute()`` for the contract.

Phase 3 additions:
- ``capabilities``: Static declaration of Capability values this agent provides.
- ``execution_tier``: Declares whether this agent runs on "local" or "cloud" hardware.
  Enables the Dispatcher to enforce tier boundaries for Role routing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from ekp_forge.protocol.capability import Capability

# Type alias for execution tier — prevents magic string proliferation
ExecutionTier = Literal["local", "cloud"]


class BaseAgent(ABC):
    """Abstract interface for all agents in the registry.

    Implementations must provide an ``agent_id`` attribute, a
    ``capabilities`` list, an ``execution_tier``, and implement
    the ``execute()`` method.

    Example::

        class MyAgent(BaseAgent):
            agent_id = "my_agent"
            capabilities = [Capability.CODING, Capability.INTROSPECTION]
            execution_tier: ExecutionTier = "local"

            def execute(self, context: dict[str, Any]) -> dict[str, Any]:
                task = context["task"]
                # ... implementation ...
                return {"status": "ok", "result": ...}
    """

    agent_id: str
    capabilities: list[Capability] = []

    # NEW Phase 3: Runtime tier boundary for dispatch enforcement.
    # - "local":  Runs on local hardware (e.g., 7B model via Ollama).
    # - "cloud":  Runs on cloud API (e.g., GPT-4o via OpenRouter).
    execution_tier: ExecutionTier = "local"

    @abstractmethod
    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        """Execute this agent's role with given context.

        Args:
            context: Role-specific context dict.
                Contract: each role defines its expected context keys.

                Known context contracts:

                - ``Role.REQUIREMENT_REVIEW``:
                    ``{"task": TaskSchema}``
                - ``Role.PLANNING``:
                    ``{"task": TaskSchema, "triage_result": dict}``
                - ``Role.IMPLEMENTATION``:
                    ``{"task": TaskSchema, "plan": str}``
                - ``Role.VERIFICATION``:
                    ``{"task": TaskSchema, "impl_result": dict}``
                - ``Role.INTEGRATION``:
                    ``{"task": TaskSchema, "verification_result": dict}``

                Additionally, the key ``"_role"`` (a ``Role`` enum) is always
                injected by the ``WorkflowEngine`` for introspection.

        Returns:
            Role-specific result dict. At minimum should include ``"status"``
            key with values ``"success"``, ``"failed"``, or ``"escalated"``.

        Raises:
            Propagates all underlying exceptions transparently.
            No exception swallowing inside ``execute()``.
        """
        ...

    # ------------------------------------------------------------------
    # Phase 3: Capability helpers
    # ------------------------------------------------------------------

    def has_capability(self, capability: Capability) -> bool:
        """Check if this agent provides a specific capability.

        Args:
            capability: The ``Capability`` value to check.

        Returns:
            True if this agent's ``capabilities`` list includes the value.
        """
        return capability in self.capabilities

    def has_all_capabilities(self, capabilities: list[Capability]) -> bool:
        """Check if this agent provides ALL specified capabilities.

        Args:
            capabilities: List of ``Capability`` values to check.

        Returns:
            True if this agent provides every capability in the list.
        """
        agent_caps = set(self.capabilities)
        return agent_caps.issuperset(capabilities)
