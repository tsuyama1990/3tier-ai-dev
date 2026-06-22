"""RAG Crawler — Phase C: Semantic assumption check via TF-IDF over decisions/ ADRs."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any


class AssumptionRAGCrawler:
    """
    Crawls decisions/*.md, builds a TF-IDF index, and provides:
    - Semantic search (query → relevant ADRs)
    - Assumption conflict detection (new assumptions vs existing ADRs)
    """

    def __init__(self, decisions_dir: Path = Path("decisions")) -> None:
        self._decisions_dir = decisions_dir
        # Each entry: {"file": str, "assumptions": dict, "decision": str, "context": str, "tokens": list[str]}
        self._index: list[dict[str, Any]] = []
        self._idf: dict[str, float] = {}

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def build_index(self) -> None:
        """
        Crawl all *.md files in decisions_dir, extract Assumptions JSON blocks,
        Decision sections, and Context sections, then build the TF-IDF index.
        """
        self._index = []

        if not self._decisions_dir.exists():
            return

        for adr_file in sorted(self._decisions_dir.glob("*.md")):
            try:
                content = adr_file.read_text(encoding="utf-8")
            except OSError:
                continue

            assumptions = self._extract_assumptions(content)
            decision = self._extract_decision(content)
            context = self._extract_context(content)

            # Full text for TF-IDF (combine decision + context + assumption keys/values)
            full_text = " ".join([
                decision,
                context,
                " ".join(assumptions.keys()),
                " ".join(str(v) for v in assumptions.values())
            ])
            tokens = self._tokenize(full_text)

            self._index.append({
                "file": adr_file.name,
                "assumptions": assumptions,
                "decision": decision,
                "context": context,
                "tokens": tokens,
            })

        # Calculate IDF
        all_tokens = set()
        for doc in self._index:
            all_tokens.update(doc["tokens"])

        total_docs = len(self._index)
        self._idf = {}
        for token in all_tokens:
            doc_count = sum(1 for doc in self._index if token in doc["tokens"])
            # Smooth IDF (scikit-learn style) to ensure IDF is never zero
            self._idf[token] = math.log((1 + total_docs) / (1 + doc_count)) + 1.0

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """
        Search for ADRs semantically similar to query.
        Returns up to top_k elements sorted by similarity score desc.
        """
        if not self._index:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        query_tf = Counter(query_tokens)
        query_vector: dict[str, float] = {}
        for term, count in query_tf.items():
            if term in self._idf:
                query_vector[term] = (count / len(query_tokens)) * self._idf[term]

        results = []
        for doc in self._index:
            doc_tokens = doc["tokens"]
            if not doc_tokens:
                score = 0.0
            else:
                doc_tf = Counter(doc_tokens)
                doc_vector: dict[str, float] = {}
                for term, count in doc_tf.items():
                    doc_vector[term] = (count / len(doc_tokens)) * self._idf.get(term, 0.0)

                # Cosine similarity
                dot_product = sum(query_vector.get(t, 0.0) * doc_vector.get(t, 0.0) for t in set(query_vector) | set(doc_vector))
                query_norm = math.sqrt(sum(v**2 for v in query_vector.values()))
                doc_norm = math.sqrt(sum(v**2 for v in doc_vector.values()))
                score = dot_product / (query_norm * doc_norm) if query_norm > 0 and doc_norm > 0 else 0.0

            results.append({
                "file": doc["file"],
                "score": score,
                "assumptions": doc["assumptions"],
                "decision": doc["decision"],
            })

        # Sort by score desc, then by filename asc (alphabetical deterministic sorting)
        results.sort(key=lambda x: (-x["score"], x["file"]))
        return results[:top_k]

    def check_assumption_conflicts(
        self, new_assumptions: dict[str, Any], _threshold: float = 0.7
    ) -> list[dict[str, Any]]:
        """
        Identify conflicting assumptions (matching keys but mismatched values).
        """
        conflicts = []
        for doc in self._index:
            for key, val in new_assumptions.items():
                if key in doc["assumptions"] and doc["assumptions"][key] != val:
                    conflicts.append({
                        "adr_file": doc["file"],
                        "key": key,
                        "adr_value": doc["assumptions"][key],
                        "new_value": val,
                    })
        return conflicts

    # -------------------------------------------------------------------
    # Internal parsing & helper methods
    # -------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Lowercase + split on non-alphanumeric characters."""
        return [tok for tok in re.split(r"[^a-z0-9]", text.lower()) if tok]

    @staticmethod
    def _extract_assumptions(content: str) -> dict[str, Any]:
        """Extract JSON code block under ## 2. Assumptions."""
        match = re.search(
            r"## 2\. Assumptions[^#]*```json\s*(\{.*?\})\s*```",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return {}

    @staticmethod
    def _extract_decision(content: str) -> str:
        """Extract content under ## 3. Decision."""
        match = re.search(
            r"## 3\. Decision\s*\n(.*?)(?=\n##|\Z)",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_context(content: str) -> str:
        """Extract content under ## 1. Context."""
        match = re.search(
            r"## 1\. Context\s*\n(.*?)(?=\n##|\Z)",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        return match.group(1).strip() if match else ""
