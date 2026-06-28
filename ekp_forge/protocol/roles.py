"""Standard role definitions for the Role-based Protocol Architecture.

This module defines the 7 standard roles that the EKP-Forge system
recognizes. Each role represents a distinct職務 (job function) in the
AI-agent organization pipeline.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """7 standard protocol roles — static, never modified at runtime.

    Each role corresponds to a phase in the agent execution pipeline.

    Attributes:
        REQUIREMENT_REVIEW: 要件定義・トリップ・制約チェック (Challenge/PM)
        PLANNING:           実装計画の生成 (Task Planner)
        ARCHITECTURE:       モジュール境界・ADR準拠性の確認 (Architect)
        SPECIFICATION:      関数・クラスの入出力およびContractの固定 (Specifier)
        IMPLEMENTATION:     コードの実装・Aiderの実行 (Worker)
        VERIFICATION:       Ruff, Mypy, Pytest等による品質診断 (Gatekeeper)
        INTEGRATION:        リポジトリへの差分統合、ADR生成 (Integrator)
    """

    REQUIREMENT_REVIEW = "RequirementReview"
    PLANNING = "Planning"
    ARCHITECTURE = "Architecture"
    SPECIFICATION = "Specification"
    IMPLEMENTATION = "Implementation"
    VERIFICATION = "Verification"
    INTEGRATION = "Integration"
