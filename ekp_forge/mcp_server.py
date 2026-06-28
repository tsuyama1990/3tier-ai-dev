"""MCP Server — EKP-Forge tools: simple aider, strict compile, and managed task pipeline."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

# The real ``mcp`` package is not installed in the test environment. We provide a
# minimal stub that mimics the API used in this file.
try:
    from mcp.server.fastmcp import FastMCP  # type: ignore
except Exception:  # pragma: no cover

    class _FastMCPStub:  # pylint: disable=too-few-public-methods
        """Stub for FastMCP when the real package is unavailable."""

        def __init__(self, name: str) -> None:
            self.name = name

        def tool(self) -> Callable[[Callable[[str], Any]], Callable[[str], Any]]:
            def decorator(func: Callable[[str], Any]) -> Callable[[str], Any]:
                return func

            return decorator

        def run(self) -> None:
            print(f"[FastMCP stub] Running server for {self.name}")

    FastMCP = _FastMCPStub  # type: ignore[misc,assignment]

import os

from ekp_forge.agents.registry import AgentRegistry
from ekp_forge.engine.workflow import WorkflowEngine
from ekp_forge.manager import ManagerAgent
from ekp_forge.orchestrator import REAL_AIDER
from ekp_forge.protocol.assignment import OrganizationLoader
from ekp_forge.protocol.roles import Role
from ekp_forge.schemas.task_schema import TaskSchema, _generate_task_id
from ekp_forge.worker import WorkerAgent

mcp = FastMCP("EKP-Forge")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# WorkflowEngine factory (lazy-init, singleton per profile)
# ---------------------------------------------------------------------------

_ENGINE_CACHE: dict[str, WorkflowEngine] = {}


def _get_workflow_engine(profile_name: str | None = None) -> WorkflowEngine:
    """Get or create a WorkflowEngine for the given profile.

    Args:
        profile_name: Profile name (default: ``EKP_PROFILE`` env var or ``"simple"``).

    Returns:
        A cached WorkflowEngine instance.
    """
    if profile_name is None:
        profile_name = os.environ.get("EKP_PROFILE", "simple")

    cache_key = profile_name
    if cache_key in _ENGINE_CACHE:
        return _ENGINE_CACHE[cache_key]

    # Build registry with available agents
    # Note: agent_id is a class attribute on both ManagerAgent and WorkerAgent
    registry = AgentRegistry()
    registry.register(ManagerAgent(manager_id="MGR-Engine-01"))
    registry.register(WorkerAgent())

    # Load profile
    profile = OrganizationLoader.load(profile_name)

    # Create engine
    engine = WorkflowEngine(profile, registry)
    _ENGINE_CACHE[cache_key] = engine
    return engine


@mcp.tool()
def execute_simple_aider(prompt: str, target_files: list[str], model: str | None = None) -> dict:
    """
    Execute aider with a simple message without static analysis or self-repair.
    """
    cmd = [REAL_AIDER, "--message", prompt, "--yes"]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(target_files)

    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )

    return {
        "success": res.returncode == 0,
        "stdout": res.stdout,
        "stderr": res.stderr,
    }


@mcp.tool()
def execute_strict_compile(
    prompt: str,
    target_pkg: str,
    target_files: list[str],
    model: str = "ollama/qwen2.5-coder:7b",
) -> dict:
    """
    Execute strict compilation pipeline through WorkflowEngine.

    Replaced legacy ``run_3tier_dev`` with Phase 3 ``WorkflowEngine`` dispatch.
    Creates a ``TaskSchema`` from the prompt and runs the full role pipeline:
    REQUIREMENT_REVIEW → PLANNING → IMPLEMENTATION → VERIFICATION → INTEGRATION.

    Args:
        prompt:       Implementation goal / instruction.
        target_pkg:   Target package name (used in task generation).
        target_files: Files to modify.
        model:        Model identifier (default: ollama/qwen2.5-coder:7b).

    Returns:
        Structured result dict with status and pipeline output.
    """
    # Generate a task schema from the prompt
    task = TaskSchema(
        task_id=_generate_task_id(target_pkg),
        manager_id="MGR-StrictCompile-01",
        goal=prompt,
        constraints=["Strict compilation: all checks must pass"],
        acceptance_tests=[f"Implementation for: {prompt[:100]}"],
        affected_modules=target_files,
    )

    # Delegate to run_managed_task (uses WorkflowEngine under the hood)
    return run_managed_task(task.model_dump())


@mcp.tool()
def run_managed_task(task_schema: dict) -> dict:
    """
    Task Schema を受け取り、WorkflowEngine 経由で管理パイプラインを実行する。

    Role-based Protocol Architecture（フェーズ1）:
    - REQUIREMENT_REVIEW → PLANNING → IMPLEMENTATION → VERIFICATION → INTEGRATION
    - 使用プロファイルは ``EKP_PROFILE`` 環境変数で切替可能（デフォルト: simple）

    Args:
        task_schema: TaskSchema 準拠の dict

    Returns:
        {
            "status": "success" | "rejected" | "failed" | "escalated",
            "task_id": str,
            "adr_path": str | None,
            "rejection_reason": str | None,
            "help_request": dict | None,
            "error_summary": list | None,
        }
    """
    try:
        # Parse and validate the task schema
        task = TaskSchema(**task_schema)
    except Exception as e:
        return {
            "status": "rejected",
            "task_id": task_schema.get("task_id", "unknown"),
            "adr_path": None,
            "rejection_reason": f"Schema validation failed: {e!s}",
            "help_request": None,
            "error_summary": None,
        }

    # Get WorkflowEngine (lazy-initialized, profile from EKP_PROFILE env)
    engine = _get_workflow_engine()

    # Phase 1: Requirement Review (Challenge Agent / Triage)
    triage_result = engine.run(Role.REQUIREMENT_REVIEW, {"task": task})
    if triage_result.get("status") == "rejected":
        return {
            "status": "rejected",
            "task_id": task.task_id,
            "adr_path": None,
            "rejection_reason": triage_result.get("rejection_reason", "Rejected by RequirementReview"),
            "help_request": None,
            "error_summary": None,
        }

    plan = triage_result.get("plan", "")

    # Phase 2: Planning (generate implementation plan)
    planning_result = engine.run(Role.PLANNING, {"task": task, "triage_result": triage_result})
    plan = planning_result.get("plan", plan)

    # Phase 2.5: Specification (generate WorkerContract) — contract-driven pipeline
    spec_result = engine.run(Role.SPECIFICATION, {"task": task, "plan": plan})
    worker_contract = spec_result.get("worker_contract")

    # Phase 3: Implementation (Worker: Aider + verification loop)
    impl_context = {"task": task, "plan": plan}
    if worker_contract is not None:
        impl_context["worker_contract"] = worker_contract
        impl_context["execution_mode"] = "contract"  # Signal to use contract-driven verification
    impl_result = engine.run(Role.IMPLEMENTATION, impl_context)

    if impl_result.get("status") == "escalated":
        help_req = impl_result.get("help_request")
        help_req_serialized: dict | str | None = None
        if help_req is not None:
            help_req_serialized = help_req.model_dump() if hasattr(help_req, "model_dump") else str(help_req)
        return {
            "status": "escalated",
            "task_id": task.task_id,
            "adr_path": None,
            "rejection_reason": None,
            "help_request": help_req_serialized,
            "error_summary": _extract_error_summary(impl_result),
            "manager_action": "escalated",
            "manager_payload": str(help_req) if help_req else "",
        }

    if impl_result.get("status") == "failed":
        return {
            "status": "failed",
            "task_id": task.task_id,
            "adr_path": None,
            "rejection_reason": None,
            "help_request": None,
            "error_summary": _extract_error_summary(impl_result),
        }

    # Phase 4: Integration (validate outcome + generate ADR)
    # Use contract-driven semantic validation if contract is available
    integration_context = {
        "task": task,
        "impl_result": impl_result,
        "error_chunk_summary": impl_result.get("error_chunk_summary"),
        "git_diff": impl_result.get("git_diff", ""),
    }
    if worker_contract is not None:
        integration_context["worker_contract"] = worker_contract
    integration_result = engine.run(Role.INTEGRATION, integration_context)

    if integration_result.get("status") == "failed":
        return {
            "status": "failed",
            "task_id": task.task_id,
            "adr_path": None,
            "rejection_reason": f"Validation failed: {integration_result.get('feedback', '')}",
            "help_request": None,
            "error_summary": _extract_error_summary(impl_result),
        }

    return {
        "status": "success",
        "task_id": task.task_id,
        "adr_path": integration_result.get("adr_path"),
        "rejection_reason": None,
        "help_request": None,
        "error_summary": _extract_error_summary(impl_result),
    }


def _extract_error_summary(result: dict) -> list:
    """Extract error summary entries from a worker/impl result dict."""
    error_chunk = result.get("error_chunk_summary")
    if error_chunk is None:
        return []
    if hasattr(error_chunk, "entries"):
        return [e.model_dump() if hasattr(e, "model_dump") else e for e in error_chunk.entries]
    if isinstance(error_chunk, dict):
        return error_chunk.get("entries", [])
    return []


@mcp.tool()
def run_epic_task(epic_schema: dict, max_workers: int = 4) -> dict:
    """
    EPIC タスクを受け取り、サブタスクに分解して並列実行する。

    WorkflowEngine からエージェントインスタンスを取得し、TaskTree に渡す。
    TaskTree 自体の WorkflowEngine 統合は Phase 2 で実施。
    """
    try:
        from ekp_forge.task_tree import TaskTree

        epic = TaskSchema(**epic_schema)
        engine = _get_workflow_engine()

        # Resolve manager and worker agents from the engine's registry
        # Note: dispatch returns BaseAgent — we cast to concrete types since
        # TaskTree.execute_parallel() requires the full ManagerAgent/WorkerAgent API.
        # This is safe because _get_workflow_engine() registers ManagerAgent and WorkerAgent.
        raw_manager = engine.dispatcher.resolve_agents(Role.REQUIREMENT_REVIEW)[0]
        raw_worker = engine.dispatcher.resolve_agents(Role.IMPLEMENTATION)[0]

        from ekp_forge.manager import ManagerAgent as _MA
        from ekp_forge.worker import WorkerAgent as _WA

        assert isinstance(raw_manager, _MA), f"Expected ManagerAgent, got {type(raw_manager)}"
        assert isinstance(raw_worker, _WA), f"Expected WorkerAgent, got {type(raw_worker)}"

        manager: _MA = raw_manager
        worker: _WA = raw_worker

        # Note: ManagerAgent.decompose_epic() is a static-like method that
        # doesn't depend on the protocol layer yet — kept as-is for backward compat
        subtasks = manager.decompose_epic(epic)

        # Build tree and execute
        tree = TaskTree()
        tree.decompose(epic, subtasks)

        results = tree.execute_parallel(worker, manager, max_workers=max_workers)
        summary = tree.get_summary()

        # Collect generated ADR paths for successful subtasks
        adr_paths = []
        for tid, node in tree._nodes.items():
            if node.status == "success" and tid != epic.task_id:
                try:
                    if node.result and "error_chunk_summary" in node.result:
                        adr_path = manager.generate_adr(node.task, node.result["error_chunk_summary"])
                        adr_paths.append(adr_path)
                except Exception:
                    pass

        # Determine overall status string
        if all(n.status == "success" for n in tree._nodes.values() if n.parent_id is not None):
            overall_status = "success"
        elif any(n.status == "success" for n in tree._nodes.values() if n.parent_id is not None):
            overall_status = "partial"
        else:
            overall_status = "failed"

        return {
            "status": overall_status,
            "epic_task_id": epic.task_id,
            "summary": summary,
            "subtask_results": {tid: res for tid, res in results.items() if tid != epic.task_id},
            "adr_paths": adr_paths,
        }
    except Exception as e:
        return {
            "status": "failed",
            "epic_task_id": epic_schema.get("task_id", "unknown"),
            "summary": {"total": 1, "success": 0, "failed": 1, "escalated": 0, "task_results": {}},
            "subtask_results": {},
            "adr_paths": [],
            "error": str(e),
        }


@mcp.tool()
def generate_task_id(goal: str) -> dict:
    """
    Generate a deterministic task ID from a goal string.
    """
    tid = _generate_task_id(goal)
    return {"task_id": tid}


if __name__ == "__main__":
    mcp.run()
