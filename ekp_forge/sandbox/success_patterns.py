"""Success Pattern DB — stores verified, integrated diffs as reusable templates.

This module upgrades the Knowledge Manager from **failure-only** (reflection logs)
to **active success pattern storage**.

Key design
----------
The :func:`store_success_pattern` is called by the **Integrator** after successful
integration (cross-module checks passed). The :func:`search_success_patterns` is
called by the **Manager** during plan generation to provide reusable templates
to the Worker.

Storage structure
-----------------
```
.ai-knowledge/
├── successes/
│   ├── T-20240623120000-abc123.json  # SuccessPattern JSON
│   └── T-20240623130000-def456.json
├── reflections/
│   └── ...
└── *.md  # integration graphs
```
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ekp_forge.schemas.task_schema import SuccessPattern, TaskSchema


def store_success_pattern(
    task: TaskSchema,
    diff: str,
    adr_path: str | None = None,
    knowledge_dir: Path | None = None,
) -> str:
    """Store a verified diff as a reusable success pattern.

    Parameters
    ----------
    task:
        The task schema that was successfully implemented.
    diff:
        The unified diff of the verified changes.
    adr_path:
        Optional path to the ADR file that authorized this change.
    knowledge_dir:
        Path to the ``.ai-knowledge`` directory. Defaults to ``./.ai-knowledge``.

    Returns
    -------
    str
        The pattern ID of the stored success pattern.
    """
    if knowledge_dir is None:
        knowledge_dir = Path(".ai-knowledge")

    successes_dir = knowledge_dir / "successes"
    successes_dir.mkdir(parents=True, exist_ok=True)

    pattern_id = task.task_id

    # Build constraint keywords from task for semantic search
    constraint_keywords = _extract_keywords(task.goal + " " + " ".join(task.constraints))

    pattern = SuccessPattern(
        pattern_id=pattern_id,
        task_goal=task.goal,
        adr_file=adr_path or "",
        unified_diff=diff,
        affected_modules=list(task.affected_modules),
        constraint_keywords=constraint_keywords,
        timestamp=datetime.now(UTC).isoformat(),
    )

    pattern_path = successes_dir / f"{pattern_id}.json"
    pattern_path.write_text(
        json.dumps(pattern.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Also write a human-readable version for Worker consumption
    _write_readable_pattern(successes_dir, pattern)

    return pattern_id


def search_success_patterns(
    query: str,
    knowledge_dir: Path | None = None,
    top_k: int = 3,
) -> list[SuccessPattern]:
    """Search stored success patterns by semantic relevance to the query.

    Uses TF-IDF cosine similarity on constraint keywords.

    Parameters
    ----------
    query:
        The search query (usually task.goal + constraints).
    knowledge_dir:
        Path to the ``.ai-knowledge`` directory. Defaults to ``./.ai-knowledge``.
    top_k:
        Maximum number of patterns to return.

    Returns
    -------
    list[SuccessPattern]
        Up to ``top_k`` most relevant patterns, sorted by relevance descending.
    """
    if knowledge_dir is None:
        knowledge_dir = Path(".ai-knowledge")

    successes_dir = knowledge_dir / "successes"
    if not successes_dir.exists():
        return []

    patterns: list[SuccessPattern] = []
    for f in sorted(successes_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            patterns.append(SuccessPattern(**data))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    if not patterns:
        return []

    query_keywords = _extract_keywords(query)
    if not query_keywords:
        return patterns[:top_k]

    # Build TF-IDF vectors
    query_tf = Counter(query_keywords)
    all_docs = [Counter(p.constraint_keywords) for p in patterns]
    all_terms: set[str] = set(query_keywords)
    for doc in all_docs:
        all_terms.update(doc.keys())

    total_docs = len(patterns)
    idf: dict[str, float] = {}
    for term in all_terms:
        doc_count = sum(1 for doc in all_docs if doc.get(term, 0) > 0)
        idf[term] = math.log((1 + total_docs) / (1 + doc_count)) + 1.0

    query_vec = {t: (query_tf[t] / len(query_keywords)) * idf.get(t, 1.0) for t in query_keywords}

    scored: list[tuple[float, SuccessPattern]] = []
    for doc_tf, pattern in zip(all_docs, patterns, strict=False):
        doc_len = sum(doc_tf.values()) or 1
        doc_vec = {t: (doc_tf[t] / doc_len) * idf.get(t, 1.0) for t in doc_tf}

        dot_product = sum(query_vec.get(t, 0.0) * doc_vec.get(t, 0.0) for t in set(query_vec) | set(doc_vec))
        query_norm = math.sqrt(sum(v**2 for v in query_vec.values()))
        doc_norm = math.sqrt(sum(v**2 for v in doc_vec.values()))
        score = dot_product / (query_norm * doc_norm) if query_norm > 0 and doc_norm > 0 else 0.0

        scored.append((score, pattern))

    scored.sort(key=lambda x: (-x[0], x[1].pattern_id))
    return [p for _, p in scored[:top_k]]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from text by lowercasing and splitting."""
    return [tok for tok in re.split(r"[^a-z0-9_]", text.lower()) if len(tok) > 2]


def _write_readable_pattern(successes_dir: Path, pattern: SuccessPattern) -> None:
    """Write a human-readable version of the pattern for Worker prompt injection."""
    readable_path = successes_dir / f"{pattern.pattern_id}.md"
    lines = [
        f"# Success Pattern: {pattern.pattern_id}",
        f"**Goal**: {pattern.task_goal}",
        f"**Modules**: {', '.join(pattern.affected_modules)}",
        "",
        "## Diff",
        "```diff",
        pattern.unified_diff,
        "```",
        "",
    ]
    if pattern.adr_file:
        lines.append(f"**ADR**: {pattern.adr_file}")
    readable_path.write_text("\n".join(lines), encoding="utf-8")
