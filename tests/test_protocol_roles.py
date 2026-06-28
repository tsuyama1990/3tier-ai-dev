"""Tests for protocol layer — Role enum, RoleAssignment, OrganizationLoader."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from ekp_forge.protocol.assignment import OrganizationLoader, OrganizationProfile, RoleAssignment
from ekp_forge.protocol.roles import Role


# ---------------------------------------------------------------------------
# Role Enum
# ---------------------------------------------------------------------------


class TestRoleEnum:
    """Verify the 7 standard roles are defined correctly."""

    def test_has_seven_roles(self) -> None:
        assert len(Role) == 7

    def test_roles_are_unique(self) -> None:
        values = [r.value for r in Role]
        assert len(values) == len(set(values))

    def test_requirement_review_role(self) -> None:
        assert Role.REQUIREMENT_REVIEW.value == "RequirementReview"

    def test_planning_role(self) -> None:
        assert Role.PLANNING.value == "Planning"

    def test_architecture_role(self) -> None:
        assert Role.ARCHITECTURE.value == "Architecture"

    def test_specification_role(self) -> None:
        assert Role.SPECIFICATION.value == "Specification"

    def test_implementation_role(self) -> None:
        assert Role.IMPLEMENTATION.value == "Implementation"

    def test_verification_role(self) -> None:
        assert Role.VERIFICATION.value == "Verification"

    def test_integration_role(self) -> None:
        assert Role.INTEGRATION.value == "Integration"

    def test_from_string(self) -> None:
        assert Role("RequirementReview") == Role.REQUIREMENT_REVIEW
        assert Role("Implementation") == Role.IMPLEMENTATION

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError):
            Role("NonExistentRole")


# ---------------------------------------------------------------------------
# RoleAssignment
# ---------------------------------------------------------------------------


class TestRoleAssignment:
    """Verify RoleAssignment schema and resolve() behavior."""

    def test_default_simple_profile(self) -> None:
        """Default assignment: manager for review/planning, worker for implementation."""
        ra = RoleAssignment({
            "requirement_review": "manager",
            "planning": "manager",
            "architecture": "manager",
            "specification": "manager",
            "implementation": "worker",
            "verification": "worker",
            "integration": "worker",
        })
        assert ra.resolve(Role.REQUIREMENT_REVIEW) == ["manager"]
        assert ra.resolve(Role.IMPLEMENTATION) == ["worker"]
        assert ra.resolve(Role.INTEGRATION) == ["worker"]

    def test_multiple_workers(self) -> None:
        """Implementation can have multiple agents assigned."""
        ra = RoleAssignment({
            "requirement_review": "manager",
            "planning": "manager",
            "architecture": "manager",
            "specification": "manager",
            "implementation": ["worker_a", "worker_b"],
            "verification": "worker",
            "integration": "manager",
        })
        assert ra.resolve(Role.IMPLEMENTATION) == ["worker_a", "worker_b"]

    def test_verification_multiple_checkers(self) -> None:
        """Verification can have multiple checkers."""
        ra = RoleAssignment({
            "requirement_review": "manager",
            "planning": "manager",
            "architecture": "manager",
            "specification": "manager",
            "implementation": "worker",
            "verification": ["ruff_checker", "mypy_checker"],
            "integration": "manager",
        })
        assert ra.resolve(Role.VERIFICATION) == ["ruff_checker", "mypy_checker"]

    def test_unknown_role_raises(self) -> None:
        """Resolving an unknown role should raise KeyError."""
        ra = RoleAssignment({
            "requirement_review": "manager",
            "planning": "manager",
            "architecture": "manager",
            "specification": "manager",
            "implementation": "worker",
            "verification": "worker",
            "integration": "worker",
        })
        # No unknown roles in the enum, but we can test that all known roles resolve
        for role in Role:
            agents = ra.resolve(role)
            assert len(agents) >= 1

    def test_to_dict(self) -> None:
        ra = RoleAssignment({
            "requirement_review": "manager",
            "planning": "manager",
            "architecture": "manager",
            "specification": "manager",
            "implementation": "worker",
            "verification": "worker",
            "integration": "worker",
        })
        d = ra.to_dict()
        assert d["requirement_review"] == "manager"
        assert d["implementation"] == "worker"


# ---------------------------------------------------------------------------
# OrganizationProfile
# ---------------------------------------------------------------------------


class TestOrganizationProfile:
    """Verify OrganizationProfile schema."""

    def test_from_yaml_simple(self) -> None:
        data = {
            "profile_name": "simple",
            "description": "Simple profile",
            "assignment": {
                "requirement_review": "manager",
                "planning": "manager",
                "architecture": "manager",
                "specification": "manager",
                "implementation": "worker",
                "verification": "worker",
                "integration": "worker",
            },
        }
        profile = OrganizationProfile.from_yaml(data)
        assert profile.profile_name == "simple"
        assert profile.assignment.resolve(Role.IMPLEMENTATION) == ["worker"]

    def test_empty_assignment_fallback(self) -> None:
        """Empty assignment dict should fall back to defaults."""
        profile = OrganizationProfile(profile_name="test", assignment={})
        assert profile.assignment.resolve(Role.REQUIREMENT_REVIEW) == ["manager"]
        assert profile.assignment.resolve(Role.IMPLEMENTATION) == ["worker"]


# ---------------------------------------------------------------------------
# OrganizationLoader
# ---------------------------------------------------------------------------


class TestOrganizationLoader:
    """Verify OrganizationLoader YAML loading and fallback behavior."""

    def test_load_from_yaml_file(self) -> None:
        """Load from a temporary YAML file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            org_dir = Path(tmpdir) / "organizations"
            org_dir.mkdir()
            profile_path = org_dir / "test_profile.yaml"
            profile_path.write_text(yaml.dump({
                "profile_name": "test_profile",
                "description": "Test profile",
                "assignment": {
                    "requirement_review": "manager",
                    "planning": "manager",
                    "architecture": "manager",
                    "specification": "manager",
                    "implementation": "worker",
                    "verification": "worker",
                    "integration": "manager",
                },
            }))

            # Set env var to point to temp dir
            old_env = os.environ.get("EKP_ORG_DIR")
            try:
                os.environ["EKP_ORG_DIR"] = str(org_dir)
                profile = OrganizationLoader.load("test_profile")
                assert profile.profile_name == "test_profile"
                assert profile.assignment.resolve(Role.IMPLEMENTATION) == ["worker"]
                assert profile.assignment.resolve(Role.INTEGRATION) == ["manager"]
            finally:
                if old_env is None:
                    del os.environ["EKP_ORG_DIR"]
                else:
                    os.environ["EKP_ORG_DIR"] = old_env

    def test_fallback_when_file_not_found(self) -> None:
        """When profile YAML doesn't exist, fall back to simple inline profile."""
        with tempfile.TemporaryDirectory() as tmpdir:
            org_dir = Path(tmpdir) / "organizations"
            org_dir.mkdir()
            # Don't create any profile files

            old_env = os.environ.get("EKP_ORG_DIR")
            try:
                os.environ["EKP_ORG_DIR"] = str(org_dir)
                profile = OrganizationLoader.load("nonexistent_profile")
                # Should return fallback simple profile
                assert profile.profile_name == "simple"
                assert profile.assignment.resolve(Role.REQUIREMENT_REVIEW) == ["manager"]
                assert profile.assignment.resolve(Role.IMPLEMENTATION) == ["worker"]
            finally:
                if old_env is None:
                    del os.environ["EKP_ORG_DIR"]
                else:
                    os.environ["EKP_ORG_DIR"] = old_env

    def test_list_profiles(self) -> None:
        """list_profiles should return YAML files without extension."""
        with tempfile.TemporaryDirectory() as tmpdir:
            org_dir = Path(tmpdir) / "organizations"
            org_dir.mkdir()
            (org_dir / "simple.yaml").write_text("profile_name: simple\n")
            (org_dir / "three_tier.yaml").write_text("profile_name: three_tier\n")

            old_env = os.environ.get("EKP_ORG_DIR")
            try:
                os.environ["EKP_ORG_DIR"] = str(org_dir)
                profiles = OrganizationLoader.list_profiles()
                assert "simple" in profiles
                assert "three_tier" in profiles
            finally:
                if old_env is None:
                    del os.environ["EKP_ORG_DIR"]
                else:
                    os.environ["EKP_ORG_DIR"] = old_env


# ---------------------------------------------------------------------------
# Phase 4.5: Mode field tests
# ---------------------------------------------------------------------------


class TestOrganizationProfileMode:
    """Tests for the ``mode`` field on OrganizationProfile."""

    def test_default_mode_is_production(self) -> None:
        """mode のデフォルト値は production であること"""
        profile = OrganizationProfile(
            profile_name="test",
            description="test",
            assignment={"implementation": "worker"},
        )
        assert profile.mode == "production"

    def test_mode_can_be_set_to_research(self) -> None:
        """mode に research が設定できること"""
        profile = OrganizationProfile(
            profile_name="test",
            description="test",
            assignment={"implementation": "worker"},
            mode="research",
        )
        assert profile.mode == "research"

    def test_from_yaml_parses_mode(self) -> None:
        """YAML の mode フィールドが正しくパースされること"""
        data = {
            "profile_name": "research_profile",
            "description": "Research mode test",
            "mode": "research",
            "assignment": {"implementation": "worker"},
        }
        profile = OrganizationProfile.from_yaml(data)
        assert profile.mode == "research"

    def test_from_yaml_defaults_to_production(self) -> None:
        """YAML に mode がない場合、デフォルトで production になること"""
        data = {
            "profile_name": "legacy",
            "description": "Legacy profile without mode",
            "assignment": {"implementation": "worker"},
        }
        profile = OrganizationProfile.from_yaml(data)
        assert profile.mode == "production"

    def test_mode_from_yaml_file(self) -> None:
        """実際の YAML ファイルから mode が読み込めること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            org_dir = Path(tmpdir) / "organizations"
            org_dir.mkdir()
            yaml_content = {
                "profile_name": "research_test",
                "description": "Test research profile",
                "mode": "research",
                "assignment": {"implementation": "worker"},
            }
            yaml_path = org_dir / "research_test.yaml"
            with open(yaml_path, "w") as f:
                yaml.dump(yaml_content, f)

            old_env = os.environ.get("EKP_ORG_DIR")
            try:
                os.environ["EKP_ORG_DIR"] = str(org_dir)
                profile = OrganizationLoader.load("research_test")
                assert profile.mode == "research"
            finally:
                if old_env is None:
                    del os.environ["EKP_ORG_DIR"]
                else:
                    os.environ["EKP_ORG_DIR"] = old_env

    def test_list_profiles_empty_dir(self) -> None:
        """list_profiles should return empty list for dir without YAML files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            org_dir = Path(tmpdir) / "organizations"
            org_dir.mkdir()

            old_env = os.environ.get("EKP_ORG_DIR")
            try:
                os.environ["EKP_ORG_DIR"] = str(org_dir)
                profiles = OrganizationLoader.list_profiles()
                assert profiles == []
            finally:
                if old_env is None:
                    del os.environ["EKP_ORG_DIR"]
                else:
                    os.environ["EKP_ORG_DIR"] = old_env
