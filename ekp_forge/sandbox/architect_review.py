"""Architect Approval Gate — deterministic ADR consistency check for Task Planner output.

This module provides a **non‑LLM, deterministic** cross‑reference between a generated
implementation plan and existing Architecture Decision Records (ADRs). It prevents the
common anti‑pattern where a Task Planner receives an abstract instruction like "add caching"
and concretises it as "implement Redis" without consulting the Architect's ADR (which
specified a ``CacheProvider`` interface).

Key design decision
-------------------
The review is deterministic (token‑based), not LLM‑based. This prevents the review gate
itself from hallucinating interface violations.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ekp_forge.schemas.task_schema import AdrComplianceResult, TaskSchema

# ---------------------------------------------------------------------------
# Technology keywords that indicate concrete implementation decisions
# ---------------------------------------------------------------------------
_IMPLEMENTATION_KEYWORDS: set[str] = {
    # Databases / storage
    "redis",
    "postgresql",
    "postgres",
    "mysql",
    "mongodb",
    "sqlite",
    "sqlalchemy",
    "django-orm",
    "peewee",
    # Message queues
    "kafka",
    "rabbitmq",
    "celery",
    "rq",
    "dramatiq",
    # HTTP / API frameworks
    "fastapi",
    "flask",
    "django",
    "aiohttp",
    "uvicorn",
    "gunicorn",
    # Caching
    "memcached",
    "cachetools",
    "disk-cache",
    # Monitoring
    "prometheus",
    "grafana",
    "datadog",
    "newrelic",
    "sentry",
}


def _extract_adr_decisions(decisions_dir: Path) -> list[dict[str, Any]]:
    """Extract Decision sections and Assumptions from all ADR files.

    Returns a list of dicts with keys:
        ``file``       — ADR filename
        ``decision``   — text from ``## 3. Decision`` section
        ``assumptions`` — parsed JSON from ``## 2. Assumptions``
    """
    results: list[dict[str, Any]] = []
    if not decisions_dir.exists():
        return results

    for adr_file in sorted(decisions_dir.glob("*.md")):
        try:
            content = adr_file.read_text(encoding="utf-8")
        except OSError:
            continue

        # Extract Decision section
        decision_match = re.search(
            r"## 3\. Decision\s*\n(.*?)(?=\n##|\Z)",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        decision_text = decision_match.group(1).strip() if decision_match else ""

        # Extract Assumptions JSON
        assumptions: dict[str, Any] = {}
        assumptions_match = re.search(
            r"## 2\. Assumptions[^#]*```json\s*(\{.*?\})\s*```",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        if assumptions_match:
            try:
                assumptions = json.loads(assumptions_match.group(1))
            except json.JSONDecodeError:
                pass

        results.append(
            {
                "file": adr_file.name,
                "decision": decision_text,
                "assumptions": assumptions,
            }
        )

    return results


def _extract_plan_technologies(plan: str) -> set[str]:
    """Extract concrete technology/infrastructure references from a plan text.

    Uses a keyword set + simple token matching. This is deliberately conservative:
    false positives trigger a re‑review, while false negatives would miss ADR violations.
    """
    plan_lower = plan.lower()
    found: set[str] = set()
    for kw in _IMPLEMENTATION_KEYWORDS:
        # Use word-boundary matching to avoid partial matches
        if re.search(r"\b" + re.escape(kw) + r"\b", plan_lower):
            found.add(kw)
    return found


def _check_adr_for_technology(
    adr: dict[str, Any],
    tech: str,
    task: TaskSchema,
) -> str | None:
    """Check if an ADR has already specified a different technology for the same concern.

    Returns a violation reason string or ``None`` if no violation found.
    """
    decision_lower = adr["decision"].lower()

    # Check if ADR explicitly mentions any keyword related to this technology
    # For example, if plan says "redis" but ADR says "use sqlite" for caching
    tech_alternatives: dict[str, list[str]] = {
        "redis": ["cacheprovider", "cache_provider", "cache interface", "lru_cache"],
        "postgresql": ["sqlite", "local storage"],
        "mysql": ["sqlite", "local storage"],
        "mongodb": ["sqlite", "json file", "file storage"],
        "kafka": ["queue.queue", "local queue", "pub/sub"],
        "rabbitmq": ["queue.queue", "local queue"],
        "fastapi": ["flask", "aiohttp", "http.server"],
        "sqlalchemy": ["sqlite3", "raw sql"],
    }

    alternatives = tech_alternatives.get(tech, [])
    for alt in alternatives:
        if alt in decision_lower:
            return (
                f"ADR '{adr['file']}' specifies using '{alt}', "
                f"but the plan proposes '{tech}'. "
                f"This violates the architecture decision."
            )

    return None


def review_plan_against_adrs(
    plan: str,
    task: TaskSchema,
    decisions_dir: Path = Path("decisions"),
) -> AdrComplianceResult:
    """Review an implementation plan for ADR compliance.

    This is a **deterministic, non‑LLM** check that:
    1. Extracts technology references from the plan text
    2. Cross‑references them against ADR decision sections
    3. Returns an ``AdrComplianceResult`` with any violations found

    Parameters
    ----------
    plan:
        The implementation plan generated by the Task Planner.
    task:
        The original task schema (used for ``task_id``).
    decisions_dir:
        Path to the directory containing ADR ``*.md`` files.

    Returns
    -------
    AdrComplianceResult
        Structured result indicating compliance and any violated ADRs.
    """
    result = AdrComplianceResult(task_id=task.task_id)

    adrs = _extract_adr_decisions(decisions_dir)
    if not adrs:
        # No ADRs to check against — pass by default
        return result

    plan_technologies = _extract_plan_technologies(plan)

    for tech in plan_technologies:
        for adr in adrs:
            violation = _check_adr_for_technology(adr, tech, task)
            if violation is not None:
                result.compliant = False
                if adr["file"] not in result.violated_adrs:
                    result.violated_adrs.append(adr["file"])
                result.violation_reasons.append(violation)

    if not result.compliant:
        result.requires_regeneration = True

    return result
