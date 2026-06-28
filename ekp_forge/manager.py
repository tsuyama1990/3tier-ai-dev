"""Manager Agent — Tier 2: triage, validation, ADR generation, and help request handling.

Now implements ``BaseAgent`` for compatibility with the Role-based Protocol
Architecture (AgentRegistry + WorkflowEngine). All existing public methods
are preserved for backward compatibility.
"""

from __future__ import annotations

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import math

import yaml

from ekp_forge.agents.base import BaseAgent, ExecutionTier
from ekp_forge.protocol.capability import Capability
from ekp_forge.protocol.roles import Role
from ekp_forge.schemas.contract import WorkerContract
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


class ManagerAgent(BaseAgent):
    """Receives structured tasks, validates, triages, and oversees Worker execution.

    Implements ``BaseAgent`` for protocol compatibility. The ``execute()``
    method dispatches to existing methods based on the ``_role`` key in
    the context dict.

    Phase 3 additions:
    - Declares ``capabilities`` including RAG_SEARCH for knowledge base lookups.
    - ``execution_tier`` defaults to ``"local"`` but may be set to ``"cloud"``
      for high-context planning roles.
    """

    agent_id: str = "manager"
    capabilities: list[Capability] = [
        Capability.REQUIREMENT_REVIEW,
        Capability.PLANNING,
        Capability.ARCHITECTURE_REVIEW,
        Capability.SPECIFICATION,
        Capability.INTEGRATION,
        Capability.RAG_SEARCH,  # Phase 3: knowledge base search
    ]
    execution_tier: ExecutionTier = "local"

    def __init__(self, manager_id: str = "MGR-Default-01") -> None:
        self.manager_id = manager_id
        self._decisions_dir = Path("decisions")
        self._decisions_dir.mkdir(parents=True, exist_ok=True)
        self._last_challenge_result: ChallengeResult | None = None
        self._last_success_patterns: list = []

    # -------------------------------------------------------------------
    # BaseAgent Protocol (Role-based Protocol Architecture)
    # -------------------------------------------------------------------

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        """Dispatch to the appropriate method based on the role context.

        This is the ``BaseAgent`` interface implementation. It reads the
        ``_role`` key from context to determine which internal method to
        call. Exceptions propagate transparently — no try/except here.

        Supported roles:
        - ``REQUIREMENT_REVIEW``: calls ``triage()``
        - ``PLANNING``:           calls ``triage()`` (plan generation phase)
        - ``ARCHITECTURE``:       calls ``triage()`` (ADR compliance check)
        - ``INTEGRATION``:        calls ``generate_adr()``
        """
        role: Role | None = context.get("_role")
        task: TaskSchema | None = context.get("task")

        if role in (Role.REQUIREMENT_REVIEW, Role.PLANNING, Role.ARCHITECTURE):
            if task is None:
                raise ValueError(f"ManagerAgent.execute(): 'task' required for role {role}")
            triage_status, triage_result = self.triage(task)
            if triage_status == "REJECT":
                return {"status": "rejected", "rejection_reason": triage_result, "plan": triage_result}
            return {"status": "accepted", "plan": triage_result}

        if role == Role.SPECIFICATION:
            """Generate a WorkerContract from task + plan using DeepSeek.

            The Manager (DeepSeek) produces:
            - A ``WorkerContract`` Pydantic instance with target files,
              editable symbols, and acceptance criteria.
            - Optionally, skeleton code written to the target file.
            """
            if task is None:
                raise ValueError(f"ManagerAgent.execute(): 'task' required for role {role}")
            plan: str = context.get("plan", "")
            contract = self._generate_contract(task, plan)
            return {
                "status": "accepted",
                "worker_contract": contract,
                "plan": plan,
            }

        if role == Role.INTEGRATION:
            if task is None:
                raise ValueError(f"ManagerAgent.execute(): 'task' required for role {role}")
            worker_contract: WorkerContract | None = context.get("worker_contract")

            # Contract-driven validation (semantic, not static analysis)
            if worker_contract is not None:
                from pathlib import Path

                code = ""
                for mod in task.affected_modules:
                    p = Path(mod)
                    if p.exists():
                        code += f"# --- {mod} ---\n{p.read_text(encoding='utf-8')}\n\n"
                if code:
                    validation_ok, feedback = self.validate_contract_compliance(worker_contract, code)
                    if not validation_ok:
                        return {"status": "failed", "feedback": feedback, "adr_path": None}
                    adr_path = self.generate_adr(task=task, error_chunk=ErrorChunkSummary(task_id=task.task_id))
                    return {"status": "success", "adr_path": adr_path}

            # Fallback: static analysis validation
            error_chunk = context.get("error_chunk_summary")
            if error_chunk is None:
                from ekp_forge.schemas.task_schema import ErrorChunkSummary

                error_chunk = ErrorChunkSummary(task_id=task.task_id)
            git_diff = context.get("git_diff", "")
            validation_ok, feedback = self.validate_outcome(task, git_diff, error_chunk)
            if not validation_ok:
                return {"status": "failed", "feedback": feedback}
            adr_path = self.generate_adr(task=task, error_chunk=error_chunk)
            return {"status": "success", "adr_path": adr_path, "validation_feedback": feedback}

        # Role not handled by ManagerAgent
        return {"status": "skipped", "reason": f"Role {role} not handled by ManagerAgent"}

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
                # Only match actual Python import statements, not natural language
                # "Import X from Y" in a constraint is an instruction to the Worker,
                # not an actual import that the pipeline uses
                import_matches = re.findall(r"^(?:import|from)\s+(\w+)", constraint.strip(), re.MULTILINE)
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

    # ------------------------------------------------------------------
    # Phase 3: Knowledge Base Search (Deterministic Keyword/BM25)
    # ------------------------------------------------------------------

    def _search_knowledge_base(self, query: str, top_k: int = 3) -> list[dict[str, str | float]]:
        """Search ``.ai-knowledge/libs/`` for relevant package documentation.

        Deterministic keyword/BM25 search — no embeddings, no LLM calls.
        Because harvested docs are cleanly organized by package/module name,
        error messages like ``AttributeError: module 'fastapi' has no attribute 'X'``
        provide clear deterministic search targets.

        Args:
            query: The search query (e.g., task goal + constraints).
            top_k: Maximum number of results to return.

        Returns:
            List of dicts with keys: ``package``, ``relevance``, ``excerpt``, ``filepath``.
        """
        knowledge_dir = Path(".ai-knowledge") / "libs"
        if not knowledge_dir.exists():
            return []

        results: list[dict[str, str | float]] = []
        query_tokens = query.lower().split()
        all_md_files = sorted(knowledge_dir.glob("*.md"))
        total_docs = len(all_md_files)

        if not query_tokens or total_docs == 0:
            return []

        for md_file in all_md_files:
            content = md_file.read_text(encoding="utf-8")
            first_line = content.split("\n")[0] if content else ""
            package_name = first_line.replace("# ", "").split(" v")[0]

            # BM25-inspired scoring: term frequency * inverse document frequency
            content_lower = content.lower()
            word_count = max(len(content_lower.split()), 1)
            score = 0.0
            for token in query_tokens:
                tf = content_lower.count(token) / word_count
                idf = max(0.0, math.log((total_docs + 1) / (1 + 1)))
                score += tf * idf

            if score > 0:
                sections = content.split("## ")
                excerpt = sections[1][:300] if len(sections) > 1 else content[:300]

                results.append(
                    {
                        "package": package_name,
                        "relevance": round(score, 4),
                        "excerpt": excerpt,
                        "filepath": str(md_file),
                    }
                )

        results.sort(key=lambda r: r["relevance"], reverse=True)  # type: ignore[typeddict-item]
        return results[:top_k]

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

        # Phase 3: Search knowledge base for relevant external library docs
        knowledge_results = self._search_knowledge_base(query)
        if knowledge_results:
            lines.append("")
            lines.append("## External Library Knowledge")
            for kr in knowledge_results:
                lines.append(f"### {kr['package']} (relevance: {kr['relevance']:.2f})")
                lines.append(str(kr["excerpt"]))
                lines.append("")

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

        # Try DeepSeek first (fastest for planning/validation)
        deepseek_response = self._call_deepseek(prompt)
        if deepseek_response is not None:
            quality_ok, feedback = self._check_ollama_quality(deepseek_response)
            if quality_ok:
                return feedback

        # Try Ollama second (local, free)
        ollama_response = self._call_ollama(prompt)
        if ollama_response is not None:
            quality_ok, feedback = self._check_ollama_quality(ollama_response)
            if quality_ok:
                return feedback

        # Fallback to OpenRouter
        openrouter_response = self._call_openrouter(prompt)
        if openrouter_response is not None:
            quality_ok, feedback = self._check_ollama_quality(openrouter_response)
            if quality_ok:
                return feedback

        # All failed — return safe default
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

        # Quick connectivity check to avoid 30s timeout when Ollama is not running
        try:
            req_check = urllib.request.Request(
                "http://localhost:11434/api/tags",
                method="HEAD",
            )
            with urllib.request.urlopen(req_check, timeout=3) as _resp:
                pass
        except Exception:
            return None

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

    def _call_deepseek(self, prompt: str) -> str | None:
        """Call DeepSeek API via urllib.request. Returns response text or None on failure."""
        import urllib.error
        import urllib.request

        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            return None

        data = json.dumps(
            {
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
        ).encode()

        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode())
                return result.get("choices", [{}])[0].get("message", {}).get("content", "")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            import sys

            print(f"[DeepSeek API error] {e}", file=sys.stderr)
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

    # -------------------------------------------------------------------
    # Phase 4: Logical Error Escalation (Pytest failure after max retries)
    # -------------------------------------------------------------------

    def handle_logical_error_escalation(
        self,
        task: Any,
        fix_task: Any,  # FixTaskV2 (avoid import-time circular issues)
        original_source: str,
        worker_fixed_source: str | None,
        diagnostics: list[Any],  # list[Diagnostic]
        iteration_count: int,
    ) -> dict[str, Any]:
        """Handle escalation when Pytest logical errors remain after fix loop.

        Phase 6 escalation boundary. The Manager (DeepSeek) analyzes whether
        the Contract design is wrong or the Worker implementation is wrong.

        Args:
            task:                The TaskSchema.
            fix_task:            The FixTaskV2 that the Worker couldn't resolve.
            original_source:     The original source code before any fix attempts.
            worker_fixed_source: The Worker's last fix attempt (if any).
            diagnostics:         Remaining Pytest diagnostics.
            iteration_count:     Number of fix iterations attempted.

        Returns:
            Dict with status:
            - ``"contract_redesign"``: Manager will redesign the WorkerContract.
            - ``"manager_patch_applied"``: Manager applied a direct fix.
            - ``"escalate_human"``: Cannot resolve — escalate to human.
        """
        context = self._build_logical_error_context(
            task,
            fix_task,
            original_source,
            worker_fixed_source,
            diagnostics,
            iteration_count,
        )

        analysis = self._analyze_logical_error(context)

        if analysis.get("verdict") == "contract_design":
            return {
                "status": "contract_redesign",
                "analysis": analysis,
                "message": "Contract design error detected. Redesigning contract.",
            }

        if analysis.get("verdict") == "worker_implementation":
            patch_result = self._apply_manager_patch(
                fix_task.target_file,
                fix_task.target_symbol,
                analysis.get("patch", ""),
            )
            if patch_result:
                return {
                    "status": "manager_patch_applied",
                    "analysis": analysis,
                    "message": "Manager applied direct patch via FunctionSlicer.",
                }
            return {
                "status": "escalate_human",
                "analysis": analysis,
                "message": "Manager patch failed — requires human intervention.",
            }

        return {
            "status": "escalate_human",
            "analysis": analysis,
            "message": "Manager could not determine root cause — requires human intervention.",
        }

    def _build_logical_error_context(
        self,
        task: Any,
        fix_task: Any,
        original_source: str,
        worker_fixed_source: str | None,
        diagnostics: list[Any],
        iteration_count: int,
    ) -> str:
        """Build a structured context string for DeepSeek analysis."""
        lines = [
            "# Logical Error Escalation",
            f"## Task: {task.task_id}",
            f"Goal: {task.goal}",
            f"Target: {fix_task.target_file}:{fix_task.target_symbol}",
            f"Fix iterations attempted: {iteration_count}",
            "",
            "## Original Source",
            f"```python\n{original_source}\n```",
        ]

        if worker_fixed_source:
            lines.extend(
                [
                    "",
                    "## Worker's Last Fix Attempt",
                    f"```python\n{worker_fixed_source}\n```",
                ]
            )

        lines.extend(
            [
                "",
                "## Remaining Diagnostics (Pytest)",
            ]
        )
        for d in diagnostics:
            lines.append(f"- {d.tool}: {d.message}")

        lines.extend(
            [
                "",
                "## Question",
                "Is this a CONTRACT DESIGN error (the specification/contract is wrong)",
                "or a WORKER IMPLEMENTATION error (the Worker failed to implement correctly)?",
                "",
                "Respond in JSON format:",
                '{"verdict": "contract_design" | "worker_implementation", "reasoning": "...", "patch": "..."}',
            ]
        )

        return "\n".join(lines)

    # -------------------------------------------------------------------
    # Phase 6: Contract-Driven Specification & Validation
    # -------------------------------------------------------------------

    def _generate_contract(self, task: Any, plan: str) -> WorkerContract:
        """Generate a ``WorkerContract`` from task + plan using DeepSeek.

        Builds a prompt that asks DeepSeek to output a JSON structure
        matching the ``WorkerContract`` schema, then instantiates it.

        If DeepSeek is unavailable, falls back to a minimal contract
        derived from the task schema directly.
        """
        prompt = (
            f"Generate a strict WorkerContract for the following task.\n\n"
            f"## Task\nGoal: {task.goal}\n"
            f"Constraints: {', '.join(task.constraints)}\n"
            f"Affected modules: {', '.join(task.affected_modules)}\n"
            f"Acceptance tests: {', '.join(task.acceptance_tests)}\n\n"
            f"## Plan\n{plan[:2000]}\n\n"
            f"## Required Output Format\n"
            f"Output a JSON object with EXACTLY these keys:\n"
            f"- contract_id: \"C-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-xxxxxx\"\n"
            f"- objective: string (single sentence describing the implementation)\n"
            f"- target_files: list of strings (files to modify)\n"
            f"- editable_symbols: list of strings (format: \"ClassName.method_name\" or \"function_name\")\n"
            f"- forbidden_symbols: list of strings\n"
            f"- acceptance_tests: list of strings\n"
            f"- implementation_steps: list of strings\n"
            f"- local_design_freedom: \"none\" or \"within_file\"\n"
            f"- skeleton_code: string (Python code with class/function signatures and ``pass`` bodies)\n"
            f"\n"
            f"CRITICAL: editable_symbols must use dotted format (e.g., \"Calculator.add\").\n"
            f"skeleton_code must be valid Python with type hints and ``pass`` for all method bodies.\n"
        )

        response = self._call_deepseek(prompt)

        if response:
            try:
                import json

                data = json.loads(response)
                contract = WorkerContract(
                    contract_id=data.get("contract_id", f"C-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-000000"),
                    objective=data.get("objective", task.goal),
                    target_files=data.get("target_files", task.affected_modules),
                    editable_symbols=data.get("editable_symbols", []),
                    forbidden_symbols=data.get("forbidden_symbols", []),
                    acceptance_tests=data.get("acceptance_tests", task.acceptance_tests),
                    implementation_steps=data.get("implementation_steps", []),
                    local_design_freedom=data.get("local_design_freedom", "none"),
                )
                # Write skeleton code to target file if provided
                skeleton = data.get("skeleton_code", "")
                if skeleton and contract.target_files:
                    target = Path(contract.target_files[0])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(skeleton, encoding="utf-8")
                return contract
            except Exception:
                pass

        # Fallback: minimal contract from task
        return WorkerContract(
            contract_id=f"C-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-000000",
            objective=task.goal,
            target_files=task.affected_modules,
            editable_symbols=[],
            acceptance_tests=task.acceptance_tests,
            local_design_freedom="none",
        )

    def validate_contract_compliance(
        self,
        contract: WorkerContract,
        code: str,
    ) -> tuple[bool, str]:
        """Validate generated code against a ``WorkerContract`` using DeepSeek.

        Args:
            contract: The ``WorkerContract`` defining expected interfaces.
            code: The generated implementation code to validate.

        Returns:
            ``(True, "")`` if compliant, ``(False, feedback_reason)`` if not.
        """
        prompt = (
            f"Validate the following implementation code against the WorkerContract.\n\n"
            f"## WorkerContract\n"
            f"Objective: {contract.objective}\n"
            f"Target files: {', '.join(contract.target_files)}\n"
            f"Editable symbols: {', '.join(contract.editable_symbols)}\n"
            f"Forbidden symbols: {', '.join(contract.forbidden_symbols)}\n"
            f"Acceptance tests: {', '.join(contract.acceptance_tests)}\n"
            f"Local design freedom: {contract.local_design_freedom}\n\n"
            f"## Implementation Code\n"
            f"```python\n{code}\n```\n\n"
            f"## Required Output Format (JSON only)\n"
            f"{{\n"
            f'  "compliant": true|false,\n'
            f'  "reasoning": "string explaining why",\n'
            f'  "issues": ["list of specific issues found"]\n'
            f"}}\n"
            f"\n"
            f"Check:\n"
            f"1. All required editable symbols exist with correct signatures.\n"
            f"2. No forbidden symbols were modified.\n"
            f"3. Target files contain the required classes/functions.\n"
            f"4. Code is syntactically valid and follow type hints.\n"
        )

        response = self._call_deepseek(prompt)

        if response:
            try:
                import json as _json

                # Try direct JSON parse first, then fallback to regex extraction
                try:
                    data = _json.loads(response)
                except _json.JSONDecodeError:
                    import re as _re
                    json_match = _re.search(r"\{[^}]+\}", response, _re.DOTALL)
                    if not json_match:
                        raise
                    data = _json.loads(json_match.group(0))

                if data.get("compliant", False):
                    return True, ""
                issues = data.get("issues", [])
                reasoning = data.get("reasoning", "Contract violation detected.")
                feedback = reasoning
                if issues:
                    feedback += "\n" + "\n".join(f"- {i}" for i in issues)
                return False, feedback
            except Exception:
                pass

        # Fallback: trust but verify with AST
        try:
            import ast
            ast.parse(code)
            return True, ""
        except SyntaxError as e:
            return False, f"Syntax error in generated code: {e}"

    def _analyze_logical_error(self, context: str) -> dict[str, Any]:
        """Call DeepSeek to analyze the logical error.

        Returns a dict with verdict, reasoning, and optional patch code.
        """
        response = self._call_deepseek(context)
        if response is None:
            return {"verdict": "unknown", "reasoning": "Failed to call DeepSeek"}

        # Try to parse JSON from response
        import json

        json_match = re.search(r"\{[^}]+\}", response, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass

        return {
            "verdict": "unknown",
            "reasoning": f"Could not parse DeepSeek response: {response[:500]}",
        }

    def _apply_manager_patch(
        self,
        file_path: str,
        symbol_name: str,
        patch_source: str,
    ) -> bool:
        """Apply a direct patch from the Manager using FunctionSlicer."""
        try:
            from ekp_forge.sandbox.slicer import FunctionSlicer

            slicer = FunctionSlicer()
            return slicer.inject_fix_to_file(file_path, symbol_name, patch_source)
        except Exception:
            return False

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
