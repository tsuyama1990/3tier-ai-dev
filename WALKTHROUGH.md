# EKP-Forge Development Walkthrough

This document chronicles the development and validation of EKP-Forge, covering Ollama integration, Safe Factory implementation, and the v4.1 multi-agent architecture.

---

## Table of Contents

1. [Ollama Integration Validation](#1-ollama-integration-validation)
2. [Safe Factory Architecture Implementation](#2-safe-factory-architecture-implementation)
3. [v4.1 Multi-Agent Pipeline Improvements](#3-v41-multi-agent-pipeline-improvements)
4. [Verification Methods & Results](#4-verification-methods--results)

---

## 1. Ollama Integration Validation

Integration and validation of local Ollama (`qwen2.5-coder:7b`) as the backend for Aider and Asset Synthesizer.

### Changes Implemented

#### 1.1. Master Validation Script
- [`validate_ollama.sh`](validate_ollama.sh): Sequential execution of all 4 test steps with summary output.

#### 1.2. Step Implementations

| Step | Test File | Description |
|------|-----------|-------------|
| **Step 1: Baseline** | [`tests/step1_baseline/test_ollama_baseline.py`](tests/step1_baseline/test_ollama_baseline.py) | Verifies Ollama `/api/chat` connectivity with timeout handling |
| **Step 2: Fake API Reference** | [`tests/step2_fake_api/test_fake_api_ref.py`](tests/step2_fake_api/test_fake_api_ref.py) | Validates Aider correctly reads `.ai-knowledge/fake_lib.md` and generates code matching special parameters (`FakeCalculator(use_magic_mode=True, offset=-99)`) |
| **Step 3: Self-Healing Stress** | [`tests/step3_stress/test_self_healing_stress.py`](tests/step3_stress/test_self_healing_stress.py) | Tests AST gatekeeper detecting banned imports (`os`), triggering self-healing loop, verifying max retry (3) rollback |
| **Step 4: Synthesizer Integration** | [`tests/step4_ollama_synthesizer/test_ollama_synthesizer.py`](tests/step4_ollama_synthesizer/test_ollama_synthesizer.py) | Validates `--llm-provider ollama` graph synthesis with automatic template fallback on connectivity failure |

#### 1.3. Core Code Extensions

- **[`ekp_forge/orchestrator_api.py`](ekp_forge/orchestrator_api.py)**: Extended call interface with `model` and `skip_self_healing` parameters. Added `--yes`/`--no-git` flags for non-interactive Aider execution.
- **[`dsc/asset_synthesizer.py`](dsc/asset_synthesizer.py)**: Added `_call_ollama` using standard library (`urllib.request`). CLI extended with `--llm-provider` and `--ollama-url` flags.

---

## 2. Safe Factory Architecture Implementation

The Safe Factory transforms EKP-Forge from a monolithic agent system into a **multi-agent pipeline with strict security boundaries**. Core principle: **isolate the worker in a sandbox, remove all destructive capabilities, and delegate Git operations to a dedicated integrator agent.**

### Problems Solved

| Issue | Solution |
|-------|----------|
| **Git Rollback Workspace Wipeout** | Worker operates in temporary sandbox; no `git` access on host |
| **Global Checker Scope Leakage** | Scoped linters check only changed files via `git diff --name-only` |
| **Self-Overwriting Configuration** | `pyproject.toml` excluded from sandbox; Config Agent uses structured TOML parsing |
| **Destructive pyproject.toml Parsing** | Config Agent with `ConfigChangeRequest` schema prevents regex corruption |
| **Hardcoded Path Dependencies** | All paths resolved relative to sandbox workspace |

### Sandbox Components

| Module | File | Purpose |
|--------|------|---------|
| **SandboxWorkspace** | [`ekp_forge/sandbox/workspace.py`](ekp_forge/sandbox/workspace.py) | Context manager creating ephemeral temp directory with whitelisted file copy |
| **Cloner Agent** | [`ekp_forge/sandbox/cloner.py`](ekp_forge/sandbox/cloner.py) | Shallow git clone or fallback copy into sandbox |
| **Integrator Agent** | [`ekp_forge/sandbox/integrator.py`](ekp_forge/sandbox/integrator.py) | Copies verified diffs back + global mypy/pytest regression |
| **Config Agent** | [`ekp_forge/sandbox/config_agent.py`](ekp_forge/sandbox/config_agent.py) | Safe TOML/YAML modification via structured requests |
| **Constraints** | [`ekp_forge/sandbox/constraints.py`](ekp_forge/sandbox/constraints.py) | Path allow/deny rules for sandbox file copying |
| **Verification** | [`ekp_forge/sandbox/verification.py`](ekp_forge/sandbox/verification.py) | CWD-scoped Ruff + Mypy execution inside sandbox |
| **Scoped Lint** | [`ekp_forge/sandbox/scoped_lint.py`](ekp_forge/sandbox/scoped_lint.py) | Git-diff-scoped linting on changed files only |

---

## 3. v4.1 Multi-Agent Pipeline Improvements

Based on an architectural review scoring EKP-Forge at **88-90 points**, five targeted improvements were implemented to reach a "sustainably growing, collapse-resistant organization."

### Improvement 1: Architect Approval Gate

- **Problem**: Task Planner concretizes abstract instructions (e.g., "add caching" → "implement Redis") without consulting ADRs.
- **Solution**: [`ekp_forge/sandbox/architect_review.py`](ekp_forge/sandbox/architect_review.py) — deterministic (non-LLM) token-based cross-reference between plan text and ADR decision sections.
- **Result**: Plan violations detected and regenerated before reaching Worker.

### Improvement 2: Success Pattern DB

- **Problem**: Knowledge Manager was failure-only; successful patterns could not be reused.
- **Solution**: [`ekp_forge/sandbox/success_patterns.py`](ekp_forge/sandbox/success_patterns.py) — stores verified diffs as reusable templates in `.ai-knowledge/successes/`.
- **Result**: Manager queries via TF-IDF during plan generation for reusable templates.

### Improvement 3: Integrator Cross-Module Regression

- **Problem**: Parallel Workers produce changes with conflicting type assumptions.
- **Solution**: [`ekp_forge/sandbox/integrator.py`](ekp_forge/sandbox/integrator.py) — runs global `mypy .` and `pytest` after file copy; reverts on failure.
- **Result**: Cross-module type conflicts detected before commit.

### Improvement 4: Adversarial Reviewer as Independent Gate

- **Problem**: Adversarial testing was embedded in Worker's verification loop, risking false-positive blocking.
- **Solution**: [`ekp_forge/adversarial_tester.py`](ekp_forge/adversarial_tester.py) — `AdversarialReviewer` as separate gate between Worker and Integrator. Failures are warnings, not blockers.
- **Result**: Edge case testing without blocking progress.

### Improvement 5: Challenge Agent with Budget

- **Problem**: Challenge Agent tends toward "reject everything" behavior as it gets smarter.
- **Solution**: [`ekp_forge/schemas/task_schema.py`](ekp_forge/schemas/task_schema.py) — `ChallengeResult` enforces `max_objections=3` hard cap with mandatory `alternative_proposal`.
- **Result**: Budgeted objections with concrete alternatives.

---

## 4. Verification Methods & Results

### Ollama Validation

```bash
bash validate_ollama.sh
```

Expected output:
```
=== [SUMMARY] ===
Step 1: PASS ✅
Step 2: PASS ✅
Step 3: PASS ✅
Step 4: PASS ✅
```

### Test Suite

```bash
pytest -v --ignore=tests/step4_ollama_synthesizer/fixtures/
```

### Component Tests

| Component | Tests | Focus |
|-----------|-------|-------|
| Sandbox Workspace | [`tests/test_sandbox_components.py`](tests/test_sandbox_components.py) | Workspace creation, whitelist filtering, cleanup |
| Manager Agent | [`tests/test_manager.py`](tests/test_manager.py) | Triage, challenge agent, ADR generation |
| Worker Agent | [`tests/test_worker.py`](tests/test_worker.py) | Aider execution, verification loop, escalation |
| Architect Review | Covered in sandbox tests | ADR compliance, keyword detection |
| Success Patterns | Covered in sandbox tests | Store, search, TF-IDF matching |
| Integrator | Covered in sandbox tests | File copy, cross-module regression, revert |

---

*See [`README.md`](README.md) for the full project overview and [`docs/organization_design.md`](docs/organization_design.md) for the complete v4.1 architecture design.*
