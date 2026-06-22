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

    @model_validator(mode="after")
    def _validate_affected_modules_have_py_ext(self) -> TaskSchema:
        for mod in self.affected_modules:
            if not mod.endswith(".py"):
                raise ValueError(f"affected_module '{mod}' must end with '.py'")
        return self


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
