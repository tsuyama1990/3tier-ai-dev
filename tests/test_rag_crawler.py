"""Unit tests for AssumptionRAGCrawler."""

import json
import shutil
import tempfile
from pathlib import Path
import unittest

from rag_crawler import AssumptionRAGCrawler


class TestAssumptionRAGCrawler(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.crawler = AssumptionRAGCrawler(decisions_dir=self.temp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_build_index_empty_dir(self) -> None:
        """Building index on empty dir results in an empty index."""
        self.crawler.build_index()
        self.assertEqual(len(self.crawler._index), 0)

    def test_build_index_with_adrs(self) -> None:
        """Building index extracts assumptions, decision, and context correctly."""
        adr1 = self.temp_dir / "20260623_000000_T-1.md"
        adr1.write_text(
            "# ADR: T-1 — Goal description\n"
            "## 1. Context\n"
            "This is some context about why we did this.\n"
            "## 2. Assumptions (Machine Readable)\n"
            "```json\n"
            '{\n  "api_schema_version": "v1.2",\n  "use_magic": true\n}\n'
            "```\n"
            "## 3. Decision\n"
            "We decided to use magic.",
            encoding="utf-8"
        )

        self.crawler.build_index()
        self.assertEqual(len(self.crawler._index), 1)
        doc = self.crawler._index[0]
        self.assertEqual(doc["file"], "20260623_000000_T-1.md")
        self.assertEqual(doc["assumptions"], {"api_schema_version": "v1.2", "use_magic": True})
        self.assertEqual(doc["decision"], "We decided to use magic.")
        self.assertEqual(doc["context"], "This is some context about why we did this.")

    def test_search_returns_top_k(self) -> None:
        """search() returns up to top_k matching elements."""
        for i in range(5):
            adr = self.temp_dir / f"adr_{i}.md"
            adr.write_text(
                f"# ADR: T-{i}\n"
                f"## 1. Context\nSearchable query words here document_{i}\n"
                "## 2. Assumptions\n```json\n{}\n```\n"
                f"## 3. Decision\nWe decided to do task_{i}",
                encoding="utf-8"
            )

        self.crawler.build_index()
        results = self.crawler.search("query words", top_k=3)
        self.assertEqual(len(results), 3)

    def test_search_score_range(self) -> None:
        """search() scores are in 0.0 to 1.0 range."""
        adr = self.temp_dir / "adr.md"
        adr.write_text(
            "# ADR: T-1\n"
            "## 1. Context\nTermA TermB\n"
            "## 2. Assumptions\n```json\n{}\n```\n"
            "## 3. Decision\nDecision content",
            encoding="utf-8"
        )
        self.crawler.build_index()
        results = self.crawler.search("TermA", top_k=1)
        if results:
            score = results[0]["score"]
            self.assertTrue(0.0 <= score <= 1.0)

    def test_check_assumption_conflicts_no_conflict(self) -> None:
        """No conflict detected when assumptions are identical or keys do not match."""
        adr = self.temp_dir / "adr.md"
        adr.write_text(
            "# ADR: T-1\n"
            "## 1. Context\nContext\n"
            "## 2. Assumptions (Machine Readable)\n"
            "```json\n"
            '{"version": 1.0}\n'
            "```\n"
            "## 3. Decision\nDecision",
            encoding="utf-8"
        )
        self.crawler.build_index()
        # Identical value -> no conflict
        conflicts1 = self.crawler.check_assumption_conflicts({"version": 1.0})
        self.assertEqual(len(conflicts1), 0)
        # Disjoint key -> no conflict
        conflicts2 = self.crawler.check_assumption_conflicts({"other_key": 2.0})
        self.assertEqual(len(conflicts2), 0)

    def test_check_assumption_conflicts_found(self) -> None:
        """Conflict detected when key matches but value differs."""
        adr = self.temp_dir / "adr.md"
        adr.write_text(
            "# ADR: T-1\n"
            "## 1. Context\nContext\n"
            "## 2. Assumptions (Machine Readable)\n"
            "```json\n"
            '{"version": 1.0}\n'
            "```\n"
            "## 3. Decision\nDecision",
            encoding="utf-8"
        )
        self.crawler.build_index()
        conflicts = self.crawler.check_assumption_conflicts({"version": 2.0})
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["adr_file"], "adr.md")
        self.assertEqual(conflicts[0]["key"], "version")
        self.assertEqual(conflicts[0]["adr_value"], 1.0)
        self.assertEqual(conflicts[0]["new_value"], 2.0)

    def test_tfidf_score_identical_docs(self) -> None:
        """TF-IDF similarity of exact matching queries is high."""
        adr = self.temp_dir / "adr.md"
        adr.write_text(
            "# ADR: T-1\n"
            "## 1. Context\nquick brown fox\n"
            "## 2. Assumptions\n```json\n{}\n```\n"
            "## 3. Decision\njumped over lazy dog",
            encoding="utf-8"
        )
        self.crawler.build_index()
        results = self.crawler.search("quick brown fox jumped over lazy dog", top_k=1)
        self.assertEqual(len(results), 1)
        self.assertGreater(results[0]["score"], 0.9)

    def test_tfidf_score_disjoint_docs(self) -> None:
        """TF-IDF similarity is 0.0 when query has no words in common with document."""
        adr = self.temp_dir / "adr.md"
        adr.write_text(
            "# ADR: T-1\n"
            "## 1. Context\napple banana orange\n"
            "## 2. Assumptions\n```json\n{}\n```\n"
            "## 3. Decision\ncherry date",
            encoding="utf-8"
        )
        self.crawler.build_index()
        results = self.crawler.search("elephant giraffe zebra", top_k=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["score"], 0.0)


if __name__ == "__main__":
    unittest.main()
