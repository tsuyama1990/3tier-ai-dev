"""Unit tests for TaskTree and epic decomposition."""

import unittest
from unittest.mock import MagicMock

from manager import ManagerAgent
from mcp_server import run_epic_task
from schemas.task_schema import TaskSchema, _generate_task_id
from task_tree import TaskTree


class TestTaskTreeAndDecomposition(unittest.TestCase):

    def setUp(self) -> None:
        self.epic_task = TaskSchema(
            task_id="T-20260623000000-a1b2c3",
            manager_id="MGR-Epic-01",
            goal="Decompose this massive goal into subtasks",
            constraints=["Constraint A", "Constraint B"],
            acceptance_tests=["Acceptance Test 1"],
            affected_modules=["src/module1.py", "src/module2.py", "src/module3.py"],
            assumptions_required={"api_schema_version": "v1.2"}
        )
        self.tree = TaskTree()
        self.manager = ManagerAgent(manager_id="MGR-Epic-01")

    def test_add_task_root(self) -> None:
        """Root task can be added without parent."""
        node = self.tree.add_task(self.epic_task)
        self.assertEqual(node.task.task_id, "T-20260623000000-a1b2c3")
        self.assertIsNone(node.parent_id)
        self.assertEqual(node.status, "pending")

    def test_add_task_with_parent(self) -> None:
        """Subtask can be added with parent_id."""
        self.tree.add_task(self.epic_task)
        subtask = TaskSchema(
            task_id="T-20260623000000-000001",
            parent_task_id="T-20260623000000-a1b2c3",
            manager_id="MGR-Epic-01",
            goal="Subtask goal",
            constraints=["Constraint A"],
            acceptance_tests=["Acceptance 1"],
            affected_modules=["src/module1.py"],
            assumptions_required={}
        )
        node = self.tree.add_task(subtask, parent_id="T-20260623000000-a1b2c3")
        self.assertEqual(node.parent_id, "T-20260623000000-a1b2c3")
        root = self.tree._nodes["T-20260623000000-a1b2c3"]
        self.assertIn(node, root.children)

    def test_decompose_adds_children(self) -> None:
        """decompose() populates subtasks as children of the epic."""
        subtasks = [
            TaskSchema(
                task_id=f"T-20260623000000-00000{i}",
                parent_task_id="T-20260623000000-a1b2c3",
                manager_id="MGR-Epic-01",
                goal=f"Goal {i}",
                constraints=["Constraint A"],
                acceptance_tests=[],
                affected_modules=[f"src/module{i}.py"],
                assumptions_required={}
            ) for i in (1, 2)
        ]
        self.tree.decompose(self.epic_task, subtasks)
        root = self.tree._nodes["T-20260623000000-a1b2c3"]
        self.assertEqual(len(root.children), 2)
        self.assertEqual(root.children[0].task.task_id, "T-20260623000000-000001")
        self.assertEqual(root.children[1].task.task_id, "T-20260623000000-000002")

    def test_execute_parallel_all_success(self) -> None:
        """Parallel execution marks everything success if worker/validation succeed."""
        subtask_id = _generate_task_id("Sub goal 1")
        subtasks = [
            TaskSchema(
                task_id=subtask_id,
                parent_task_id="T-20260623000000-a1b2c3",
                manager_id="MGR-Epic-01",
                goal="Sub goal 1",
                constraints=["Constraint A"],
                acceptance_tests=[],
                affected_modules=["src/module1.py"],
                assumptions_required={}
            )
        ]
        self.tree.decompose(self.epic_task, subtasks)

        # Mocks
        worker = MagicMock()
        worker.execute_verification_loop.return_value = {
            "status": "success",
            "git_diff": "some diff",
            "error_chunk_summary": MagicMock(total_retries=1),
            "retries": 1
        }

        manager = MagicMock()
        manager.triage.return_value = ("ACCEPT", "plan content")
        manager.validate_outcome.return_value = (True, "")

        results = self.tree.execute_parallel(worker, manager)
        self.assertEqual(self.tree._nodes[subtask_id].status, "success")
        self.assertEqual(self.tree._nodes["T-20260623000000-a1b2c3"].status, "success")
        self.assertIn(subtask_id, results)

    def test_execute_parallel_partial_failure(self) -> None:
        """If any subtask fails, tree execution reflects failure."""
        sub_id1 = _generate_task_id("Sub goal 1")
        sub_id2 = _generate_task_id("Sub goal 2")
        subtasks = [
            TaskSchema(
                task_id=sub_id1,
                parent_task_id="T-20260623000000-a1b2c3",
                manager_id="MGR-Epic-01",
                goal="Sub goal 1",
                constraints=["Constraint A"],
                acceptance_tests=[],
                affected_modules=["src/module1.py"],
                assumptions_required={}
            ),
            TaskSchema(
                task_id=sub_id2,
                parent_task_id="T-20260623000000-a1b2c3",
                manager_id="MGR-Epic-01",
                goal="Sub goal 2",
                constraints=["Constraint A"],
                acceptance_tests=[],
                affected_modules=["src/module2.py"],
                assumptions_required={}
            )
        ]
        self.tree.decompose(self.epic_task, subtasks)

        worker = MagicMock()
        # Mocking task execution success for S1, failure for S2
        def worker_side_effect(task, _plan, _run_adversarial=False):
            if task.task_id == sub_id1:
                return {"status": "success", "git_diff": "diff", "error_chunk_summary": MagicMock(total_retries=0)}
            return {"status": "failed", "error_chunk_summary": MagicMock(total_retries=3)}

        worker.execute_verification_loop.side_effect = worker_side_effect

        manager = MagicMock()
        manager.triage.return_value = ("ACCEPT", "plan")
        manager.validate_outcome.return_value = (True, "")

        self.tree.execute_parallel(worker, manager)
        self.assertEqual(self.tree._nodes[sub_id1].status, "success")
        self.assertEqual(self.tree._nodes[sub_id2].status, "failed")
        self.assertEqual(self.tree._nodes["T-20260623000000-a1b2c3"].status, "failed")

    def test_get_summary_counts(self) -> None:
        """get_summary() correctly counts and indexes task details."""
        node1 = self.tree.add_task(self.epic_task)
        node1.status = "success"
        node1.result = {"retries": 2}

        summary = self.tree.get_summary()
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["success"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["task_results"]["T-20260623000000-a1b2c3"]["status"], "success")
        self.assertEqual(summary["task_results"]["T-20260623000000-a1b2c3"]["retries"], 2)

    def test_parent_id_propagation(self) -> None:
        """Subtasks have parent_task_id populated matching the epic."""
        subtasks = self.manager.decompose_epic(self.epic_task)
        self.assertGreater(len(subtasks), 0)
        for sub in subtasks:
            self.assertEqual(sub.parent_task_id, "T-20260623000000-a1b2c3")

    def test_decompose_epic_by_modules(self) -> None:
        """Epic is decomposed by modules when affected_modules count >= 3."""
        subtasks = self.manager.decompose_epic(self.epic_task)
        self.assertEqual(len(subtasks), 3)
        self.assertEqual(subtasks[0].affected_modules, ["src/module1.py"])
        self.assertEqual(subtasks[1].affected_modules, ["src/module2.py"])
        self.assertEqual(subtasks[2].affected_modules, ["src/module3.py"])

    def test_decompose_epic_single_module(self) -> None:
        """Epic is not decomposed if modules < 3 and constraints < 5."""
        single_module_epic = TaskSchema(
            task_id="T-20260623120000-ffffff",
            manager_id="MGR-Epic-01",
            goal="Simple goal",
            constraints=["Constraint A"],
            acceptance_tests=["Acceptance Test 1"],
            affected_modules=["src/module1.py"],
            assumptions_required={}
        )
        subtasks = self.manager.decompose_epic(single_module_epic)
        self.assertEqual(len(subtasks), 0)

    def test_run_epic_task_mcp_tool(self) -> None:
        """MCP run_epic_task handles decomposition and execution successfully under mock."""
        epic_dict = {
            "task_id": "T-20260623000000-111111",
            "manager_id": "MGR-Epic-02",
            "goal": "MCP Epic Goal to satisfy",
            "constraints": ["Constraint A"],
            "acceptance_tests": ["Test A"],
            "affected_modules": ["src/a.py", "src/b.py", "src/c.py"],
            "assumptions_required": {}
        }
        # Mock execute_verification_loop to run cleanly using patch context managers
        import manager
        import worker
        from unittest.mock import patch

        with patch.object(worker.WorkerAgent, "execute_verification_loop") as mock_evl, \
             patch.object(manager.ManagerAgent, "triage") as mock_triage, \
             patch.object(manager.ManagerAgent, "validate_outcome") as mock_validate:
            
            mock_evl.return_value = {
                "status": "success",
                "git_diff": "diff",
                "error_chunk_summary": MagicMock(total_retries=0)
            }
            mock_triage.return_value = ("ACCEPT", "plan")
            mock_validate.return_value = (True, "")

            res = run_epic_task(epic_dict, max_workers=2)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["epic_task_id"], "T-20260623000000-111111")
            self.assertEqual(res["summary"]["total"], 4)  # Epic + 3 subtasks
            self.assertEqual(res["summary"]["success"], 4)


if __name__ == "__main__":
    unittest.main()
