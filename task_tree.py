"""TaskTree implementation for parallel execution of decomposed tasks."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from manager import ManagerAgent
    from worker import WorkerAgent

from schemas.task_schema import TaskSchema


@dataclass
class TaskNode:
    task: TaskSchema
    status: str = "pending"  # pending | running | success | failed | escalated
    result: dict | None = None
    children: list["TaskNode"] = field(default_factory=list)
    parent_id: str | None = None


class TaskTree:
    def __init__(self) -> None:
        self._nodes: dict[str, TaskNode] = {}  # task_id -> TaskNode
        self._lock = threading.Lock()

    def add_task(self, task: TaskSchema, parent_id: str | None = None) -> TaskNode:
        """Add a task to the tree. If parent_id is provided, associate it as a child."""
        with self._lock:
            node = TaskNode(task=task, parent_id=parent_id)
            self._nodes[task.task_id] = node
            if parent_id and parent_id in self._nodes:
                self._nodes[parent_id].children.append(node)
            return node

    def decompose(self, epic: TaskSchema, subtasks: list[TaskSchema]) -> None:
        """Decompose an epic task into subtasks in the tree."""
        self.add_task(epic)
        for subtask in subtasks:
            self.add_task(subtask, parent_id=epic.task_id)

    def execute_parallel(
        self,
        worker: "WorkerAgent",
        manager: "ManagerAgent",
        max_workers: int = 4,
    ) -> dict[str, dict]:
        """
        Execute subtasks in parallel, triaging each and verifying outcomes.
        Returns a dictionary mapping task_id to worker execution result.
        """
        # Find all nodes to execute (nodes with parent_id != None, i.e., subtasks)
        with self._lock:
            subtask_nodes = [node for node in self._nodes.values() if node.parent_id is not None]
            epic_nodes = [node for node in self._nodes.values() if node.parent_id is None]

        # If there are no subtasks, fall back to executing the epic tasks directly
        targets = subtask_nodes if subtask_nodes else epic_nodes
        results: dict[str, dict] = {}

        def run_single_node(node: TaskNode) -> tuple[str, dict]:
            with self._lock:
                node.status = "running"

            # Step 1: Triage
            triage_status, triage_result = manager.triage(node.task)
            if triage_status == "REJECT":
                res = {
                    "status": "rejected",
                    "rejection_reason": triage_result
                }
                with self._lock:
                    node.status = "rejected"
                    node.result = res
                return node.task.task_id, res

            plan = triage_result

            # Step 2: Worker execute verification loop
            worker_result = worker.execute_verification_loop(node.task, plan)

            # Step 3: Validation if worker was successful
            if worker_result.get("status") == "success":
                validation_ok, feedback = manager.validate_outcome(
                    node.task,
                    worker_result.get("git_diff", ""),
                    worker_result["error_chunk_summary"]
                )
                if validation_ok:
                    status = "success"
                else:
                    status = "failed"
                    worker_result["status"] = "failed"
                    worker_result["validation_feedback"] = feedback
            else:
                status = worker_result.get("status", "failed")

            with self._lock:
                node.status = status
                node.result = worker_result

            return node.task.task_id, worker_result

        # Execute target tasks in parallel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_node = {executor.submit(run_single_node, node): node for node in targets}
            for future, node in future_to_node.items():
                try:
                    tid, res = future.result()
                    results[tid] = res
                except Exception as e:
                    err_res = {
                        "status": "failed",
                        "error": str(e)
                    }
                    results[node.task.task_id] = err_res
                    with self._lock:
                        node.status = "failed"
                        node.result = err_res

        # Determine and set the status of parent nodes (epics)
        with self._lock:
            for epic_node in epic_nodes:
                if epic_node.children:
                    if all(child.status == "success" for child in epic_node.children):
                        epic_node.status = "success"
                    else:
                        epic_node.status = "failed"

        return results

    def get_summary(self) -> dict:
        """
        Return the execution summary of the tree.
        """
        task_results = {}
        total = 0
        success = 0
        failed = 0
        escalated = 0

        with self._lock:
            for tid, node in self._nodes.items():
                total += 1
                if node.status == "success":
                    success += 1
                elif node.status == "failed":
                    failed += 1
                elif node.status == "escalated":
                    escalated += 1

                retries = 0
                if node.result and isinstance(node.result, dict):
                    if "retries" in node.result:
                        retries = node.result["retries"]
                    elif "error_chunk_summary" in node.result:
                        ecs = node.result["error_chunk_summary"]
                        if hasattr(ecs, "total_retries"):
                            retries = ecs.total_retries
                        elif isinstance(ecs, dict) and "total_retries" in ecs:
                            retries = ecs["total_retries"]

                task_results[tid] = {
                    "status": node.status,
                    "retries": retries
                }

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "escalated": escalated,
            "task_results": task_results
        }
