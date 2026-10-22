import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from shared_task_state import state
from shared_task_state_mcp.backend import Backend


def request(op="create", actor="test-agent", **kwargs):
    return {"schema_version": 1, "request_id": uuid.uuid4().hex,
            "actor": actor, "op": op, **kwargs}


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="supervision ")
        self.base = Path(self.temp.name)
        self.config = self.base / "config.json"
        state.bootstrap(self.config, self.base / "db", self.base / "exchange")
        self.backend = Backend(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, task_id="task-a", actor="controller-a"):
        return self.backend.apply(request(
            actor=actor, fields={"title": task_id, "goal": "supervise exact run"},
            execution_pool="cloud", read_only=True, task_id=task_id))["receipt"]["task"]

    def apply(self, op, task, actor, **kwargs):
        return self.backend.apply(request(
            op, actor=actor, task_id=task["task_id"], expected_revision=task["revision"],
            **({"lease_id": task["lease_id"]} if task.get("lease_id") else {}), **kwargs))

    def test_snapshot_is_readonly_and_separates_heartbeat_from_reported_progress(self):
        task = self.create()
        task = self.apply("claim", task, "worker-b")["receipt"]["task"]
        task = self.apply("bind", task, "worker-b", execution={
            "executor": "command", "run_id": "run-a"})["receipt"]["task"]
        task = self.apply("update", task, "worker-b", fields={
            "summary": "step completed", "last_confirmed_step": "fixture step"})["receipt"]["task"]
        task = self.apply("heartbeat", task, "worker-b")["receipt"]["task"]
        history = self.backend.history(task["task_id"], 0, 100)["events"]
        progress_seq = next(event["seq"] for event in history if event["action"] == "update")
        heartbeat_seq = max(event["seq"] for event in history if event["action"] == "heartbeat")
        before = self.backend.status()

        result = self.backend.supervise([task["task_id"]], after_seq=progress_seq - 1)
        item = result["tasks"][0]
        self.assertEqual(item["last_reported_progress_event"]["seq"], progress_seq)
        self.assertLess(item["last_reported_progress_event"]["seq"], heartbeat_seq)
        self.assertEqual(item["progress"]["summary"], "step completed")
        self.assertTrue(item["heartbeat_fresh"])
        self.assertEqual(item["delegation"]["run_id"], "run-a")
        self.assertEqual(result["snapshot_seq"], before["latest_event"])
        self.assertEqual(self.backend.status()["latest_event"], before["latest_event"])
        self.assertEqual(self.backend.read(task["task_id"])["task"]["revision"], task["revision"])

        config = state._read_config(self.config)
        store = state.Store(config)
        try:
            with store.transaction():
                current = store.get(task["task_id"])
                current["heartbeat_epoch"] = 0
                store.save(current, "fixture", "expire-heartbeat")
        finally:
            store.db.close()
        stale = self.backend.supervise([task["task_id"]], after_seq=progress_seq)
        self.assertFalse(stale["tasks"][0]["heartbeat_fresh"])
        self.assertIsNone(stale["tasks"][0]["last_reported_progress_event"])
        self.assertEqual(self.backend.status()["latest_event"], before["latest_event"] + 1)

    def test_supervision_bounds_exact_ids_and_marks_review_needs(self):
        task = self.create()
        completed = self.apply("claim", task, "worker-b")["receipt"]["task"]
        completed = self.apply("finish", completed, "worker-b", exit_code=0)["receipt"]["task"]
        result = self.backend.supervise([completed["task_id"]])
        self.assertTrue(result["tasks"][0]["review_needed"])
        cli = subprocess.run([sys.executable, "-m", "shared_task_state_mcp", "--config",
                              str(self.config), "supervise", "--task-id", completed["task_id"]],
                             cwd=Path(__file__).resolve().parents[1], capture_output=True,
                             text=True, timeout=20)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["tasks"][0]["task_id"], completed["task_id"])
        with self.assertRaises(state.StateError):
            self.backend.supervise(["missing-task"])
        with self.assertRaises(state.StateError):
            self.backend.supervise([f"task-{i}" for i in range(101)])

    def test_supervision_does_not_attribute_a_prior_generation_binding(self):
        task = self.create()
        task = self.apply("claim", task, "worker-a")["receipt"]["task"]
        task = self.apply("bind", task, "worker-a", execution={
            "executor": "command", "run_id": "prior-run"})["receipt"]["task"]
        prior_binding = task["delegation"]
        task = self.apply("finish", task, "worker-a", exit_code=0)["receipt"]["task"]
        task = self.apply("requeue", task, "controller-a", stop_confirmed=True,
                          stop_evidence="old worker finished", resume_evidence="reviewed")["receipt"]["task"]
        task = self.apply("claim", task, "worker-b")["receipt"]["task"]

        # Simulate a cooperating 0.1 writer claiming a new generation without
        # knowing to clear metadata introduced by a newer writer.
        store = state.Store(state._read_config(self.config))
        try:
            with store.transaction():
                current = store.get(task["task_id"])
                current["delegation"] = prior_binding
                store.db.execute("UPDATE tasks SET data=? WHERE id=?",
                                 (json.dumps(current, ensure_ascii=False), task["task_id"]))
        finally:
            store.db.close()

        item = self.backend.supervise([task["task_id"]])["tasks"][0]
        self.assertEqual(item["binding_status"], "stale")
        self.assertIsNone(item["delegation"])
        self.assertNotIn("prior-run", json.dumps(item))


class SupervisionWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_stdio_clients_bind_and_read_through_readonly_supervision(self):
        with tempfile.TemporaryDirectory(prefix="supervision wire ") as tmp:
            base = Path(tmp)
            config = base / "config.json"
            state.bootstrap(config, base / "db", base / "exchange")
            child_env = dict(os.environ)
            command = [sys.executable, "-m", "shared_task_state_mcp", "--config", str(config)]
            writable = StdioServerParameters(command=command[0], args=command[1:], env=child_env)
            read_only = StdioServerParameters(command=command[0], args=command[1:] + ["--read-only"], env=child_env)
            async with stdio_client(writable) as streams_a:
                async with stdio_client(writable) as streams_b:
                    async with stdio_client(read_only) as streams_c:
                        async with ClientSession(*streams_a) as client_a:
                            async with ClientSession(*streams_b) as client_b:
                                async with ClientSession(*streams_c) as client_c:
                                    await client_a.initialize()
                                    await client_b.initialize()
                                    await client_c.initialize()
                                    tools = {tool.name for tool in (await client_c.list_tools()).tools}
                                    self.assertIn("shared_task_supervise", tools)
                                    self.assertNotIn("shared_task_apply", tools)

                                    created = await client_a.call_tool("shared_task_apply", {"request": request(
                                        actor="controller-a", fields={"title": "wire", "goal": "bind run"},
                                        execution_pool="cloud", read_only=True, task_id="wire-task")})
                                    self.assertFalse(created.is_error, created)
                                    task = created.structured_content["receipt"]["task"]
                                    claimed = await client_b.call_tool("shared_task_apply", {"request": request(
                                        "claim", actor="worker-b", task_id=task["task_id"],
                                        expected_revision=task["revision"])})
                                    self.assertFalse(claimed.is_error, claimed)
                                    task = claimed.structured_content["receipt"]["task"]
                                    bound = await client_b.call_tool("shared_task_apply", {"request": request(
                                        "bind", actor="worker-b", task_id=task["task_id"],
                                        expected_revision=task["revision"], lease_id=task["lease_id"],
                                        execution={"executor": "command", "run_id": "wire-run"})})
                                    self.assertFalse(bound.is_error, bound)

                                    supervised = await client_c.call_tool("shared_task_supervise", {
                                        "task_ids": [task["task_id"]], "after_seq": 0})
                                    self.assertFalse(supervised.is_error, supervised)
                                    item = supervised.structured_content["tasks"][0]
                                    self.assertEqual(item["delegation"]["controller"], "controller-a")
                                    self.assertEqual(item["delegation"]["worker"], "worker-b")
                                    self.assertEqual(item["delegation"]["run_id"], "wire-run")


if __name__ == "__main__":
    unittest.main()
