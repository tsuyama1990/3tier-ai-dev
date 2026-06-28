"""RoleAssignment schema and OrganizationProfile YAML loader.

This module provides the data models and loading mechanism for the
Assignment (人事) layer. It maps Role enum values to agent identifiers
defined in organization YAML profiles.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml

from ekp_forge.protocol.roles import Role

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default fallback profile used when no YAML file or env var is set
_FALLBACK_ASSIGNMENT: dict[str, str | list[str]] = {
    "requirement_review": "manager",
    "planning": "manager",
    "architecture": "manager",
    "specification": "manager",
    "implementation": "worker",
    "verification": "worker",
    "integration": "worker",
}

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RoleAssignment:
    """Maps each Role to one or more agent identifiers.

    This is the core 「人事」 (Assignment) data: it declares which agent(s)
    are responsible for each role in the current organization profile.

    The ``resolve()`` method is the primary lookup interface used by the
    WorkflowEngine dispatcher.
    """

    def __init__(self, data: dict[str, str | list[str]]) -> None:
        self.requirement_review: str = self._as_str(data.get("requirement_review", "manager"))
        self.planning: str = self._as_str(data.get("planning", "manager"))
        self.architecture: str = self._as_str(data.get("architecture", "manager"))
        self.specification: str = self._as_str(data.get("specification", "manager"))
        self.implementation: str | list[str] = data.get("implementation", "worker")  # type: ignore[assignment]
        self.verification: str | list[str] = data.get("verification", "worker")  # type: ignore[assignment]
        self.integration: str = self._as_str(data.get("integration", "worker"))

    @staticmethod
    def _as_str(value: Any) -> str:
        if isinstance(value, list):
            return value[0] if value else ""
        return str(value) if value else ""

    def resolve(self, role: Role) -> list[str]:
        """Resolve a Role into agent name(s).

        Args:
            role: The role to look up.

        Returns:
            List of agent identifier strings assigned to this role.

        Raises:
            KeyError: If the role is not recognized.
        """
        mapping: dict[Role, str | list[str]] = {
            Role.REQUIREMENT_REVIEW: self.requirement_review,
            Role.PLANNING: self.planning,
            Role.ARCHITECTURE: self.architecture,
            Role.SPECIFICATION: self.specification,
            Role.IMPLEMENTATION: self.implementation,
            Role.VERIFICATION: self.verification,
            Role.INTEGRATION: self.integration,
        }
        value = mapping[role]
        if isinstance(value, str):
            return [value]
        return list(value)

    def to_dict(self) -> dict[str, str | list[str]]:
        """Return a plain dict representation for serialization."""
        return {
            "requirement_review": self.requirement_review,
            "planning": self.planning,
            "architecture": self.architecture,
            "specification": self.specification,
            "implementation": self.implementation,
            "verification": self.verification,
            "integration": self.integration,
        }


class OrganizationProfile:
    """Top-level organization profile loaded from YAML.

    Attributes:
        profile_name:   Name of this profile (e.g. "simple", "three_tier").
        description:    Human-readable description of the profile.
        mode:           Execution mode — ``"research"`` (no sandbox, no git ops,
                        direct local execution) or ``"production"`` (safe isolated
                        execution via ``git worktree``). Default: ``"production"``.
        assignment:     The RoleAssignment mapping for this profile.
    """

    def __init__(
        self,
        profile_name: str = "",
        description: str = "",
        assignment: dict[str, Any] | None = None,
        mode: Literal["research", "production"] = "production",
    ) -> None:
        self.profile_name = profile_name
        self.description = description
        self.mode = mode
        self.assignment = RoleAssignment(assignment or dict(_FALLBACK_ASSIGNMENT))

    @classmethod
    def from_yaml(cls, data: dict[str, Any]) -> OrganizationProfile:
        """Construct from a parsed YAML dictionary."""
        assignment_data = data.get("assignment", {})
        return cls(
            profile_name=data.get("profile_name", "unknown"),
            description=data.get("description", ""),
            assignment=assignment_data,
            mode=data.get("mode", "production"),
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class OrganizationLoader:
    """Loads OrganizationProfile from YAML files.

    Resolution order:
    1. ``EKP_ORG_DIR`` environment variable (absolute or relative path).
    2. ``organizations/`` directory relative to the repository root
       (resolved via ``Path(__file__).parent.parent.parent``).
    3. CWD-relative ``organizations/`` directory.
    4. Hardcoded fallback ``simple`` profile (no YAML needed).

    This design ensures the loader works regardless of the runtime working
    directory.
    """

    # Resolved relative to ekp_forge/protocol/assignment.py → ../../organizations
    _FALLBACK_DIR = Path(__file__).resolve().parent.parent.parent / "organizations"

    @classmethod
    def _organizations_dir(cls) -> Path:
        """Return the resolved organizations directory path."""
        # 1. Env var override
        env_dir = os.environ.get("EKP_ORG_DIR")
        if env_dir:
            p = Path(env_dir)
            if p.is_absolute():
                return p
            return Path.cwd() / p

        # 2. Module-relative path (repository root)
        if cls._FALLBACK_DIR.is_dir():
            return cls._FALLBACK_DIR

        # 3. CWD-relative fallback
        cwd_dir = Path.cwd() / "organizations"
        if cwd_dir.is_dir():
            return cwd_dir

        # 4. Fallback: return the module-relative path even if it doesn't exist yet
        return cls._FALLBACK_DIR

    @classmethod
    def list_profiles(cls) -> list[str]:
        """List available profile names (without ``.yaml`` extension)."""
        org_dir = cls._organizations_dir()
        if not org_dir.exists():
            return []
        return sorted(f.stem for f in org_dir.glob("*.yaml"))

    @classmethod
    def load(cls, profile_name: str = "simple") -> OrganizationProfile:
        """Load a profile by name.

        Args:
            profile_name: The profile stem (without ``.yaml`` extension).
                          Falls back to ``"simple"`` if not found.

        Returns:
            An ``OrganizationProfile`` instance.

        Raises:
            FileNotFoundError: If the profile YAML does not exist and the
                               fallback ``"simple"`` also fails.
        """
        path = cls._organizations_dir() / f"{profile_name}.yaml"
        if not path.exists():
            # Fallback: return inline simple profile
            return OrganizationProfile(
                profile_name="simple",
                description="Fallback simple profile (no YAML file found)",
                assignment=dict(_FALLBACK_ASSIGNMENT),
            )
        with open(path) as f:
            data = yaml.safe_load(f)
        return OrganizationProfile.from_yaml(data)
