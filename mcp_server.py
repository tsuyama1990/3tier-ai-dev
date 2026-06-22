"""MCP Server — EKP-Forge tools: simple aider, strict compile, and managed task pipeline."""

from __future__ import annotations

import subprocess

from mcp.server.fastmcp import FastMCP

from manager import ManagerAgent
from orchestrator import REAL_AIDER
from orchestrator_api import run_3tier_dev
from schemas.task_schema import TaskSchema, _generate_task_id
from worker import WorkerAgent

mcp = FastMCP("EKP-Forge")


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
    Execute strict compilation pipeline through run_3tier_dev.
    """
    return run_3tier_dev(
        prompt=prompt,
        target_pkg=target_pkg,
        target_files=target_files,
        model=model,
        timeout=600,
    )


@mcp.tool()
def run_managed_task(task_schema: dict) -> dict:
    """
    Task Schema を受け取り、Manager-Worker パイプラインを実行する。

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

    # Initialize agents
    manager = ManagerAgent(manager_id=task.manager_id)
    worker = WorkerAgent()

    # Phase 1: Triage
    triage_status, triage_result = manager.triage(task)
    if triage_status == "REJECT":
        return {
            "status": "rejected",
            "task_id": task.task_id,
            "adr_path": None,
            "rejection_reason": triage_result,
            "help_request": None,
            "error_summary": None,
        }

    implementation_plan = triage_result

    # Phase 2: Worker execution
    worker_result = worker.execute_verification_loop(task, implementation_plan)

    if worker_result["status"] == "escalated":
        # Phase 2b: Handle escalation
        help_req = worker_result["help_request"]
        if help_req:
            manager_action, manager_payload = manager.handle_help_request(help_req)
            return {
                "status": "escalated",
                "task_id": task.task_id,
                "adr_path": None,
                "rejection_reason": None,
                "help_request": help_req.model_dump(),
                "error_summary": [e.model_dump() for e in worker_result["error_chunk_summary"].entries],
                "manager_action": manager_action,
                "manager_payload": manager_payload,
            }
        return {
            "status": "escalated",
            "task_id": task.task_id,
            "adr_path": None,
            "rejection_reason": None,
            "help_request": None,
            "error_summary": [e.model_dump() for e in worker_result["error_chunk_summary"].entries],
        }

    if worker_result["status"] == "failed":
        return {
            "status": "failed",
            "task_id": task.task_id,
            "adr_path": None,
            "rejection_reason": None,
            "help_request": None,
            "error_summary": [e.model_dump() for e in worker_result["error_chunk_summary"].entries],
        }

    # Phase 3: Manager validation
    validation_ok, feedback = manager.validate_outcome(
        task,
        worker_result.get("git_diff", ""),
        worker_result["error_chunk_summary"],
    )

    if not validation_ok:
        # Could trigger rework here, but for now return as failed validation
        return {
            "status": "failed",
            "task_id": task.task_id,
            "adr_path": None,
            "rejection_reason": f"Validation failed: {feedback}",
            "help_request": None,
            "error_summary": [e.model_dump() for e in worker_result["error_chunk_summary"].entries],
        }

    # Phase 4: ADR generation
    adr_path = manager.generate_adr(
        task=task,
        error_chunk=worker_result["error_chunk_summary"],
    )

    return {
        "status": "success",
        "task_id": task.task_id,
        "adr_path": adr_path,
        "rejection_reason": None,
        "help_request": None,
        "error_summary": [e.model_dump() for e in worker_result["error_chunk_summary"].entries],
    }


@mcp.tool()
def run_epic_task(epic_schema: dict, max_workers: int = 4) -> dict:
    """
    EPIC タスクを受け取り、サブタスクに分解して並列実行する。
    """
    try:
        from task_tree import TaskTree
        epic = TaskSchema(**epic_schema)
        manager = ManagerAgent(manager_id=epic.manager_id)
        worker = WorkerAgent()

        # Decompose
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
            "summary": {
                "total": 1,
                "success": 0,
                "failed": 1,
                "escalated": 0,
                "task_results": {}
            },
            "subtask_results": {},
            "adr_paths": [],
            "error": str(e)
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
