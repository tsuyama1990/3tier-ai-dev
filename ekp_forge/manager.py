"""Manager Agent — Tier 2: triage, validation, ADR generation, and help request handling."""

from __future__ import annotations

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ekp_forge.schemas.task_schema import (
    ChallengeObjection,
    ChallengeResult,
    ErrorChunkSummary,
    HelpRequestSchema,
    TaskSchema,
    _generate_task_id,
)

# ---------------------------------------------------------------------------
# Manager Agent
# ---------------------------------------------------------------------------


class ManagerAgent:
    """Receives structured tasks, validates, triages, and oversees Worker execution."""

    def __init__(self, manager_id: str = "MGR-Default-01") -> None:
        self.manager_id = manager_id
        self._decisions_dir = Path("decisions")
        self._decisions_dir.mkdir(parents=True, exist_ok=True)
        self._last_challenge_result: ChallengeResult | None = None
        self._last_success_patterns: list = []

    # -------------------------------------------------------------------
    # Triage
    # -------------------------------------------------------------------

    def triage(self, task: TaskSchema) -> tuple[str, str]:
        """
        Validate the task schema and check assumptions.

        Returns:
            ("ACCEPT", implementation_plan: str)
            ("REJECT", reason: str)
        """
        # Step 1: Pydantic validation is automatic (caller must pass TaskSchema)

        # NEW: Challenge Agent with budget enforcement (v4.1)
        if not (task.bypass_challenge_agent or task.force_accept):
            challenge_result = self._run_challenge_agent(task)
            if challenge_result.blocked:
                return ("REJECT", self._format_challenge_rejection(challenge_result))
            if len(challenge_result.objections) > 0:
                # Non-blocking objections — attach them to the plan as warnings
                self._last_challenge_result = challenge_result

        # Architect check (Module boundary constraints)
        for mod in task.affected_modules:
            p = Path(mod)
            if p.name in {"pyproject.toml", "mcp_config.json", "README.md"} or any(
                part in {".venv", "__pycache__", ".git", "tests/generated"} for part in p.parts
            ):
                return (
                    "REJECT",
                    f"Architect: Modifying '{mod}' violates architectural boundaries (restricted file or directory).",
                )

        # Step 2: Assumption Check
        reject_reason = self._check_assumptions(task)
        if reject_reason:
            return ("REJECT", reject_reason)

        # Step 3: Search success patterns for plan reuse (v4.1)
        try:
            from ekp_forge.sandbox.success_patterns import search_success_patterns

            patterns = search_success_patterns(task.goal + " " + " ".join(task.constraints), top_k=2)
            if patterns:
                self._last_success_patterns = patterns
        except Exception:
            self._last_success_patterns = []

        # Step 4: Generate Implementation Plan
        plan = self._generate_implementation_plan(task)

        # Step 5: Architect Approval — ADR compliance check (v4.1)
        try:
            from ekp_forge.sandbox.architect_review import review_plan_against_adrs

            adr_result = review_plan_against_adrs(plan, task, self._decisions_dir)
            if not adr_result.compliant:
                # Append violation context and regenerate
                violation_context = "\n".join(f"- {reason}" for reason in adr_result.violation_reasons)
                plan = self._generate_implementation_plan(
                    task,
                    extra_context=f"\n## Architect Review Violations\n{violation_context}\n\nRevise the plan to comply with existing ADRs.",
                )
        except Exception:
            pass  # Architect review failure should not block execution

        return ("ACCEPT", plan)

    # -------------------------------------------------------------------
    # Challenge Agent (Budget-constrained, v4.1)
    # -------------------------------------------------------------------

    def _run_challenge_agent(self, task: TaskSchema) -> ChallengeResult:
        """
        Run Challenge Agent with budget constraints:
        - max 3 objections
        - each objection MUST include alternative_proposal
        - cannot block if user sets force_accept=True
        - force_bypass skips all checks
        """
        result = ChallengeResult(task_id=task.task_id)

        if task.force_accept:
            result.force_bypass_applied = True
            return result

        # Keyword-based overengineering detection (budgeted)
        overengineered_keywords = [
            ("redis", "Use `functools.lru_cache` or an in-memory dict instead of Redis."),
            ("postgresql", "Use `sqlite3` (stdlib) instead of PostgreSQL."),
            ("postgres", "Use `sqlite3` (stdlib) instead of PostgreSQL."),
            ("mysql", "Use `sqlite3` (stdlib) instead of MySQL."),
            ("mongodb", "Use `sqlite3` (stdlib) or JSON file storage instead of MongoDB."),
            ("mongo", "Use `sqlite3` (stdlib) or JSON file storage instead of MongoDB."),
            ("kafka", "Use a local queue (`queue.Queue`) or Redis pub/sub instead of Kafka."),
            ("rabbitmq", "Use a local queue (`queue.Queue`) instead of RabbitMQ."),
        ]

        combined_text = (task.goal + " " + " ".join(task.constraints)).lower()
        for kw, alternative in overengineered_keywords:
            if re.search(r"\b" + kw + r"\b", combined_text):
                added = result.add_objection(
                    ChallengeObjection(
                        objection_id=len(result.objections) + 1,
                        category="over_engineering",
                        description=f"Task references '{kw}' which suggests over-engineering.",
                        alternative_proposal=alternative,
                    )
                )
                if not added:
                    break  # Budget exhausted

        # If max_objections reached without being blocked, still allow execution
        # Blocking only happens if a critical objection is raised (e.g., redundant feature)
        # For now, only over-engineering objections are non-blocking
        result.blocked = False  # Over-engineering objections are warnings, not blockers
        return result

    def _format_challenge_rejection(self, result: ChallengeResult) -> str:
        """Format ChallengeResult as a rejection reason string."""
        parts = ["Challenge Agent rejected the task:"]
        for obj in result.objections:
            parts.append(f"\n  [{obj.category}] {obj.description}")
            parts.append(f"    Alternative: {obj.alternative_proposal}")
        parts.append(f"\nObjections: {len(result.objections)}/{result.max_objections}")
        return "\n".join(parts)

    def _check_assumptions(self, task: TaskSchema) -> str | None:
        """
        Verify task assumptions against api_schema.yaml and existing ADRs.

        Returns None if all checks pass, or a rejection reason string.
        """
        # 2a: Check api_schema.yaml constraints
        schema_path = Path("api_schema.yaml")
        if schema_path.exists():
            with open(schema_path) as f:
                schema = yaml.safe_load(f)

            allowed_imports = set(schema.get("allowed_imports", []))
            # Check if any constraint references disallowed imports
            for constraint in task.constraints:
                # Look for import references in constraints
                import_matches = re.findall(r"(?:import|from)\s+(\w+)", constraint)
                for pkg in import_matches:
                    if pkg not in allowed_imports and not pkg.startswith("_"):
                        return (
                            f"Assumption violated: constraint '{constraint}' references "
                            f"import '{pkg}' which is not in api_schema.yaml allowed_imports ({allowed_imports})"
                        )

        # 2b: Check decisions/ ADRs for conflicting assumptions (RAG Crawler)
        from ekp_forge.rag_crawler import AssumptionRAGCrawler

        crawler = AssumptionRAGCrawler(self._decisions_dir)
        crawler.build_index()
        conflicts = crawler.check_assumption_conflicts(task.assumptions_required)
        if conflicts:
            msgs = [f"{c['key']}: ADR expects {c['adr_value']!r}, task has {c['new_value']!r}" for c in conflicts]
            return f"Assumption violated (RAG): {'; '.join(msgs)}"

        return None

    def _check_adr_conflict(self, adr_path: Path, task: TaskSchema) -> str | None:
        """Check a single ADR for assumption conflicts with the given task."""
        try:
            content = adr_path.read_text(encoding="utf-8")
            # Extract Assumptions JSON block
            match = re.search(
                r"## 2\. Assumptions[^#]*```json\s*(\{.*?\})\s*```",
                content,
                re.DOTALL,
            )
            if not match:
                return None

            adr_assumptions = json.loads(match.group(1))
            # Simple check: if ADR has 'api_schema_version' and task has 'assumptions_required'
            if "api_schema_version" in adr_assumptions and "api_schema_version" in task.assumptions_required:
                adr_ver = str(adr_assumptions["api_schema_version"])
                task_ver = str(task.assumptions_required["api_schema_version"])
                if adr_ver != task_ver:
                    return (
                        f"Assumption violated: ADR {adr_path.stem} expects "
                        f"api_schema_version={adr_ver}, but task requires {task_ver}"
                    )
        except (json.JSONDecodeError, OSError):
            pass
        return None

    def _generate_implementation_plan(self, task: TaskSchema, extra_context: str = "") -> str:
        """Generate an implementation plan from the task schema.

        Parameters
        ----------
        task:
            The task to generate a plan for.
        extra_context:
            Optional additional context to include (e.g., Architect Review violation
            feedback requesting plan regeneration).
        """
        lines: list[str] = [
            f"# Implementation Plan: {task.task_id}",
            f"**Goal**: {task.goal}",
            f"**Manager**: {task.manager_id}",
            "",
            "## Relevant ADRs",
        ]

        # Attach relevant ADR summaries (using RAG search)
        from ekp_forge.rag_crawler import AssumptionRAGCrawler

        crawler = AssumptionRAGCrawler(self._decisions_dir)
        crawler.build_index()

        query = task.goal + " " + " ".join(task.constraints)
        relevant = crawler.search(query, top_k=3)
        for r in relevant:
            summary = r["decision"][:200]
            lines.append(f"- [{r['file']}] (relevance: {r['score']:.2f}) {summary}")

        lines.extend(
            [
                "",
                "## Constraints",
            ]
        )
        for c in task.constraints:
            lines.append(f"- {c}")

        lines.extend(
            [
                "",
                "## Affected Modules",
            ]
        )
        for mod in task.affected_modules:
            lines.append(f"- `{mod}`")

        lines.extend(
            [
                "",
                "## Implementation Steps",
            ]
        )
        for i, mod in enumerate(task.affected_modules, 1):
            lines.append(f"{i}. Modify `{mod}` to satisfy the goal and acceptance tests.")

        lines.extend(
            [
                "",
                "## Acceptance Criteria",
            ]
        )
        for t in task.acceptance_tests:
            lines.append(f"- [ ] {t}")

        lines.append("")
        lines.append("## Current Interface Signatures")
        for mod in task.affected_modules:
            try:
                sig = self._extract_signatures(Path(mod))
                if sig:
                    lines.append(f"### {mod}")
                    lines.append(f"```python\n{sig}\n```")
            except Exception:
                pass

        if extra_context:
            lines.append("")
            lines.append("## Additional Context")
            lines.append(extra_context)

        return "\n".join(lines)

    @staticmethod
    def _extract_signatures(path: Path) -> str:
        """Extract function/class signatures from a Python file."""
        if not path.exists():
            return ""
        try:
            import ast

            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            sigs: list[str] = []
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ClassDef):
                    bases = ", ".join(ast.unparse(b) if isinstance(b, ast.Name) else "" for b in node.bases)
                    sigs.append(f"class {node.name}({bases}): ...")
                    for item in ast.iter_child_nodes(node):
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            args = ast.unparse(item.args) if hasattr(item, "args") else ""
                            sigs.append(f"    def {item.name}({args}): ...")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = ast.unparse(node.args) if hasattr(node, "args") else ""
                    sigs.append(f"def {node.name}({args}): ...")
            return "\n".join(sigs)
        except (SyntaxError, OSError):
            return ""

    # -------------------------------------------------------------------
    # Validation / Reflection
    # -------------------------------------------------------------------

    def validate_outcome(
        self,
        task: TaskSchema,
        git_diff: str,
        error_chunk: ErrorChunkSummary,
    ) -> tuple[bool, str]:
        """
        Validate the Worker's output using static checks and optional LLM reflection.

        Returns:
            (True, "")              ← approved
            (False, review_feedback) ← rework required
        """
        # Step 1: Static checks (no LLM)
        feedback = self._static_validation(task, git_diff)
        if feedback:
            return (False, feedback)

        # Step 2: LLM Reflection — uses Ollama-first approach with OpenRouter fallback
        llm_feedback = self._llm_validation(task, git_diff, error_chunk)
        if llm_feedback:
            return (False, llm_feedback)

        return (True, "")

    def _static_validation(self, _task: TaskSchema, git_diff: str) -> str:
        """Perform static analysis on the diff without LLM calls."""
        findings: list[str] = []

        # Check for hardcoded assert True
        if re.search(r"assert\s+True\b", git_diff):
            findings.append("Hardcoded `assert True` detected in diff")

        # Check for TEST_MODE conditionals
        if "TEST_MODE" in git_diff:
            findings.append("`TEST_MODE` conditional detected in diff")

        # Check constraints against diff
        schema_path = Path("api_schema.yaml")
        if schema_path.exists():
            with open(schema_path) as f:
                schema = yaml.safe_load(f)
            allowed = set(schema.get("allowed_imports", []))
            # Check added imports in diff
            for line in git_diff.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    stripped = line[1:].strip()
                    if stripped.startswith("import ") or stripped.startswith("from "):
                        pkg = stripped.split()[1].split(".")[0]
                        if pkg not in allowed and not pkg.startswith("_"):
                            findings.append(f"Constraint violation: unauthorized import '{pkg}' in diff")

        if findings:
            return "## Review Findings\n" + "\n".join(f"- {f}" for f in findings)
        return ""

    def _llm_validation(
        self,
        task: TaskSchema,
        git_diff: str,
        error_chunk: ErrorChunkSummary,
    ) -> str:
        """
        Perform LLM-based reflection.

        Uses Ollama-first (local) with Criteria-based fallback to OpenRouter.
        Returns review feedback string if issues found, or empty string if approved.
        """
        if os.environ.get("SKIP_LLM_VALIDATION") == "1":
            return ""

        # Build prompt
        prompt = self._build_validation_prompt(task, git_diff, error_chunk)

        # Try Ollama first
        ollama_response = self._call_ollama(prompt)
        if ollama_response is not None:
            # Quality check
            quality_ok, feedback = self._check_ollama_quality(ollama_response)
            if quality_ok:
                return feedback

        # Fallback to OpenRouter
        openrouter_response = self._call_openrouter(prompt)
        if openrouter_response is not None:
            quality_ok, feedback = self._check_ollama_quality(openrouter_response)
            if quality_ok:
                return feedback

        # Both failed — return safe default
        return ""

    def _build_validation_prompt(
        self,
        task: TaskSchema,
        git_diff: str,
        error_chunk: ErrorChunkSummary,
    ) -> str:
        """Build the LLM validation prompt."""
        error_summary = ""
        if error_chunk.entries:
            items = "\n".join(
                f"- Attempt {e.attempt}: {e.error_type} in {e.module} — {e.action_taken}" for e in error_chunk.entries
            )
            error_summary = f"\n### Error Summary\n{items}"

        return f"""You are a code reviewer. Review the following implementation against the task constraints.

## Task: {task.task_id}
Goal: {task.goal}
Constraints: {", ".join(task.constraints)}
Acceptance Tests: {", ".join(task.acceptance_tests)}
{error_summary}

## Git Diff
```diff
{git_diff[:3000]}
```

## Validation Criteria
1. Does the implementation violate any constraints?
2. Are there any hardcoded values or test-specific workarounds?
3. Are there naming convention violations or design smells?
4. Is the implementation correct for the given acceptance tests?

## Required Output Format
You MUST output EXACTLY in this format:

## Review Findings
- [finding 1]
- [finding 2]
...

Decision: APPROVE or REJECT

If no issues found, output:
## Review Findings
No issues found.

Decision: APPROVE
"""

    def _call_ollama(self, prompt: str) -> str | None:
        """Call Ollama API via urllib.request. Returns response text or None on failure."""
        import urllib.error
        import urllib.request

        data = json.dumps(
            {
                "model": "qwen2.5-coder:7b",
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
        ).encode()

        req = urllib.request.Request(
            "http://localhost:11434/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                return result.get("message", {}).get("content", "")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    def _call_openrouter(self, prompt: str) -> str | None:
        """Call OpenRouter API via urllib.request. Returns response text or None on failure."""
        import urllib.error
        import urllib.request

        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            return None

        data = json.dumps(
            {
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode()

        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                return result.get("choices", [{}])[0].get("message", {}).get("content", "")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    def _check_ollama_quality(self, response: str) -> tuple[bool, str]:
        """
        Check Ollama response quality against 4 criteria.

        Returns:
            (True, extracted_feedback) if quality passes
            (False, "") if quality fails (should fallback to OpenRouter)
        """
        # Criterion 1: Must have ## Review Findings section
        if "## Review Findings" not in response:
            return (False, "")

        # Criterion 2: Must contain APPROVE or REJECT
        if "APPROVE" not in response and "REJECT" not in response:
            return (False, "")

        # Criterion 3: Response length check (~3000 tokens ≈ ~12000 chars)
        if len(response) > 12000:
            return (False, "")

        # Extract the review findings section for feedback
        if "REJECT" in response:
            # Extract findings after ## Review Findings
            match = re.search(r"## Review Findings\s*\n(.*?)(?=\nDecision:|\Z)", response, re.DOTALL)
            if match:
                findings = match.group(1).strip()
                return (True, f"## Review Findings\n{findings}")
            return (True, "## Review Findings\nLLM rejected the implementation.")

        return (True, "")

    # -------------------------------------------------------------------
    # ADR Generation
    # -------------------------------------------------------------------

    def generate_adr(
        self,
        task: TaskSchema,
        error_chunk: ErrorChunkSummary,
        reflection_notes: str = "",
        validation_history: list[str] | None = None,
    ) -> str:
        """
        Generate an Assumption-Driven ADR and save to decisions/.

        Returns the file path of the saved ADR.
        """
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        filename_ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        adr_filename = f"{filename_ts}_{task.task_id}.md"
        adr_path = self._decisions_dir / adr_filename

        # Build error summary table
        error_table = "| Attempt | Error Type | Module | Action Taken |\n"
        error_table += "|---------|-----------|--------|-------------|\n"
        for entry in error_chunk.entries:
            error_table += f"| {entry.attempt} | {entry.error_type} | {entry.module} | {entry.action_taken} |\n"
        if not error_chunk.entries:
            error_table += "| — | None | — | — |\n"

        # Build validation history
        val_history = "\n".join(f"- {h}" for h in validation_history) if validation_history else "No rework cycles."

        # Build the ADR content from template
        adr_content = f"""# ADR: {task.task_id} — {task.goal}

**Date**: {timestamp}
**Status**: Accepted

## 1. Context
{task.goal}

The following constraints were imposed:
{chr(10).join(f"- {c}" for c in task.constraints)}

## 2. Assumptions (Machine Readable)
```json
{json.dumps(task.assumptions_required, indent=2) if task.assumptions_required else "{}"}
```

## 3. Decision
The implementation was completed following the accepted implementation plan for {task.task_id}.
Validation passed with {error_chunk.total_retries} retries.

## 4. Reflection & Compromises
### Error Summary
{error_table}

### Validation History
{val_history}

### Compromises
{reflection_notes if reflection_notes else "No compromises were recorded."}
"""
        adr_path.write_text(adr_content, encoding="utf-8")
        return str(adr_path)

    # -------------------------------------------------------------------
    # Help Request Handling
    # -------------------------------------------------------------------

    def handle_help_request(
        self,
        help_req: HelpRequestSchema,
    ) -> tuple[str, str]:
        """
        Handle an escalation from Worker.

        Returns:
            ("PROVIDE_CONTEXT", additional_rag_context: str)
            ("REJECT", reason: str)
        """
        if help_req.reason.value == "missing_context":
            # Search decisions/ and .ai-knowledge/ for additional context
            context = self._search_additional_context(help_req.needed_information)
            if context:
                return ("PROVIDE_CONTEXT", context)
            return ("REJECT", f"Could not find requested context: {help_req.needed_information}")

        if help_req.reason.value == "cyclic_error":
            return ("REJECT", "Cyclic error detected. Task requires human intervention.")

        if help_req.reason.value == "confidence_drop":
            # Re-run assumption check
            # Since we don't have the original TaskSchema here, we log and reject
            return (
                "REJECT",
                f"Worker confidence dropped to {help_req.confidence}. Task requires human review.",
            )

        return ("REJECT", f"Unknown escalation reason: {help_req.reason}")

    def _search_additional_context(self, needed_info: list[str]) -> str:
        """Search decisions/ and .ai-knowledge/ for additional context."""
        context_parts: list[str] = []

        # Search decisions/ ADRs
        if self._decisions_dir.exists():
            for adr_file in sorted(self._decisions_dir.glob("*.md")):
                try:
                    content = adr_file.read_text(encoding="utf-8")
                    for info in needed_info:
                        if info.lower() in content.lower():
                            # Extract Decision section
                            match = re.search(r"## 3\. Decision\s*\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
                            if match:
                                context_parts.append(f"### From {adr_file.name}\n{match.group(1).strip()}")
                            break
                except OSError:
                    pass

        # Search .ai-knowledge/ directory
        ai_knowledge_dir = Path(".ai-knowledge")
        if ai_knowledge_dir.exists():
            for md_file in sorted(ai_knowledge_dir.glob("*.md")):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    for info in needed_info:
                        if info.lower() in content.lower():
                            context_parts.append(f"### From {md_file.name}\n{content[:1000]}")
                            break
                except OSError:
                    pass

        return "\n\n".join(context_parts) if context_parts else ""

    def decompose_epic(self, epic: TaskSchema) -> list[TaskSchema]:
        """
        Decompose an EPIC task into actionable subtasks.
        """
        subtasks: list[TaskSchema] = []

        # Rule 1: affected_modules >= 3 -> split by module
        if len(epic.affected_modules) >= 3:
            for _idx, module in enumerate(epic.affected_modules, 1):
                sub_id = _generate_task_id(f"{epic.task_id}-subtask-{module}")
                subtask = TaskSchema(
                    task_id=sub_id,
                    parent_task_id=epic.task_id,
                    manager_id=epic.manager_id,
                    goal=f"Decomposed subtask for {module}: {epic.goal}",
                    constraints=epic.constraints,
                    acceptance_tests=[f"Verify changes to {module}"],
                    affected_modules=[module],
                    assumptions_required=epic.assumptions_required,
                )
                subtasks.append(subtask)
        # Rule 2: constraints >= 5 -> split constraints
        elif len(epic.constraints) >= 5:
            for idx in range(0, len(epic.constraints), 2):
                chunk = epic.constraints[idx : idx + 2]
                sub_id = _generate_task_id(f"{epic.task_id}-constraint-{idx}")
                subtask = TaskSchema(
                    task_id=sub_id,
                    parent_task_id=epic.task_id,
                    manager_id=epic.manager_id,
                    goal=f"Decomposed constraint chunk: {epic.goal}",
                    constraints=chunk,
                    acceptance_tests=epic.acceptance_tests,
                    affected_modules=epic.affected_modules,
                    assumptions_required=epic.assumptions_required,
                )
                subtasks.append(subtask)

        return subtasks


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pass
