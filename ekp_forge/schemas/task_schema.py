"""Pydantic models for EKP-Forge v3.0/v4.0 Task Schema, HelpRequest, ErrorChunk, and more."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# 1. Task Schema (Tier 1 → Tier 2)
# ---------------------------------------------------------------------------


class TaskSchema(BaseModel):
    """Structured task definition — no natural language prompts allowed."""

    model_config = ConfigDict(strict=True)

    task_id: str = Field(pattern=r"^T-\d{14}-[a-f0-9]{6}$")
    parent_task_id: str | None = None
    manager_id: str
    goal: str = Field(max_length=200, min_length=1)
    constraints: list[str] = Field(min_length=1)
    acceptance_tests: list[str]
    affected_modules: list[str] = Field(min_length=1)
    assumptions_required: dict[str, Any] = Field(default_factory=dict)
    force_accept: bool = False
    bypass_challenge_agent: bool = False

    @model_validator(mode="after")
    def _validate_affected_modules_have_py_ext(self) -> TaskSchema:
        for mod in self.affected_modules:
            if not mod.endswith(".py"):
                raise ValueError(f"affected_module '{mod}' must end with '.py'")
        return self


class ConfigChangeRequest(BaseModel):
    """Metadata request for a configuration change (e.g., pyproject.toml update)."""

    model_config = ConfigDict(strict=True)

    key_path: list[str]
    action: str  # "append", "set", "remove"
    value: Any


# ---------------------------------------------------------------------------
# 2. Help Request Schema (Worker → Manager Escalation)
# ---------------------------------------------------------------------------


class EscalationReason(StrEnum):
    CYCLIC_ERROR = "cyclic_error"
    CONTEXT_MISSING = "missing_context"
    CONFIDENCE_DROP = "confidence_drop"


class HelpRequestSchema(BaseModel):
    """Sent by Worker when it cannot resolve an issue on its own."""

    model_config = ConfigDict(strict=True)

    task_id: str
    status: str = "needs_help"
    reason: EscalationReason
    confidence: float = Field(ge=0.0, le=1.0)
    attempts: list[str] = Field(default_factory=list)
    needed_information: list[str] = Field(default_factory=list)
    current_diff_stash: str | None = None


# ---------------------------------------------------------------------------
# 3. Error Chunk Summary (Worker internal accumulation)
# ---------------------------------------------------------------------------


class ErrorChunkEntry(BaseModel):
    """A single error occurrence during a verification attempt."""

    attempt: int
    error_type: str
    module: str
    action_taken: str


class ErrorChunkSummary(BaseModel):
    """Accumulated error log across verification loop iterations."""

    task_id: str
    entries: list[ErrorChunkEntry] = Field(default_factory=list)
    total_retries: int = 0

    def add_entry(self, entry: ErrorChunkEntry) -> None:
        self.entries.append(entry)
        self.total_retries = len(self.entries)


# ---------------------------------------------------------------------------
# 4. Reflection Log (v4.0 Phase 1)
# ---------------------------------------------------------------------------


class ReflectionEntry(BaseModel):
    """A single lesson learned from a task execution."""

    task_id: str
    timestamp: str
    trigger: str
    root_cause: str
    actionable_tactic: str
    error_types_encountered: list[str]


class ReflectionLog(BaseModel):
    """Collection of reflection entries scoped to a model."""

    model_name: str
    entries: list[ReflectionEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 5. Agent Scorecard (v4.0 Phase 2)
# ---------------------------------------------------------------------------


class AgentScore(BaseModel):
    """Performance metrics for a single agent model."""

    model_name: str
    success_rate: float = Field(ge=0.0, le=1.0)
    avg_retry_count: float = Field(ge=0.0)
    escalation_accuracy: float = Field(ge=0.0, le=1.0)
    domain_expertise: dict[str, float] = Field(default_factory=dict)
    total_tasks: int = 0
    last_updated: str = ""


class AgentScorecard(BaseModel):
    """Collection of agent scores keyed by model name."""

    agents: dict[str, AgentScore] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 7. Challenge Agent Budget & Architect Approval Schemas (v4.1)
# ---------------------------------------------------------------------------


class ChallengeObjection(BaseModel):
    """A single objection raised by the Challenge Agent."""

    objection_id: int
    category: str  # "over_engineering", "missing_assumption", "redundant_feature"
    description: str
    alternative_proposal: str  # MANDATORY — cannot object without proposing alternative


class ChallengeResult(BaseModel):
    """Structured output of the Challenge Agent with budget constraints."""

    task_id: str
    objections: list[ChallengeObjection] = Field(default_factory=list)
    max_objections: int = 3  # Hard cap — enforced in code
    blocked: bool = False
    force_bypass_applied: bool = False

    def add_objection(self, objection: ChallengeObjection) -> bool:
        """Add an objection if under the max_objections cap. Returns True if added."""
        if len(self.objections) >= self.max_objections:
            return False
        if not objection.alternative_proposal.strip():
            raise ValueError(f"Objection {objection.objection_id} missing alternative_proposal")
        self.objections.append(objection)
        return True


class AdrComplianceResult(BaseModel):
    """Output of Architect's ADR compliance check on a Task Planner output."""

    task_id: str
    compliant: bool = True
    violated_adrs: list[str] = Field(default_factory=list)
    violation_reasons: list[str] = Field(default_factory=list)
    requires_regeneration: bool = False


# ---------------------------------------------------------------------------
# 8. Success Pattern Schema (v4.1 — Knowledge Manager Upgrade)
# ---------------------------------------------------------------------------


class SuccessPattern(BaseModel):
    """A verified, integrated change that can be reused as a template."""

    pattern_id: str
    task_goal: str
    adr_file: str  # Reference to the ADR that authorized this
    unified_diff: str
    affected_modules: list[str]
    constraint_keywords: list[str]  # For semantic search
    timestamp: str


# ---------------------------------------------------------------------------
# 6. Utility functions
# ---------------------------------------------------------------------------


def _generate_task_id(goal: str) -> str:
    """Deterministic task ID based on timestamp + goal hash."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    goal_hash = hashlib.sha256(goal.encode()).hexdigest()[:6]
    return f"T-{timestamp}-{goal_hash}"


def _is_project_specific(tactic: str, project_pkgs: list[str]) -> bool:
    """Check if a tactic references project-specific package names."""
    return any(pkg.lower() in tactic.lower() for pkg in project_pkgs)


def _error_fingerprint(pytest_output: str) -> str:
    """Extract essential error signature from pytest output for cyclic detection."""
    lines = [
        line for line in pytest_output.splitlines() if line.startswith("FAILED") or "Error:" in line or "assert" in line
    ]
    fingerprint_str = "\n".join(sorted(lines))
    return hashlib.sha256(fingerprint_str.encode()).hexdigest()[:16]


def _estimate_confidence(attempt: int, error_chunk: ErrorChunkSummary) -> float:
    """Estimate confidence based on attempt count and error diversity."""
    base = max(0.0, 1.0 - attempt * 0.25)
    unique_error_types = len({e.error_type for e in error_chunk.entries})
    diversity_penalty = min(0.3, max(0.0, (unique_error_types - 1) * 0.1))
    return round(base - diversity_penalty, 2)
