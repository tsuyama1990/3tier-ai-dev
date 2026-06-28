"""Knowledge management package for Phase 3 — PyPI doc harvesting + structured KB.

Components:
- ``KnowledgeHarvester``: Fetches PyPI metadata, compresses docs to
  ``.ai-knowledge/libs/<package>.md``.
- ``search_knowledge_base``: Deterministic keyword/BM25 search over
  harvested docs (used by ManagerAgent).
"""
