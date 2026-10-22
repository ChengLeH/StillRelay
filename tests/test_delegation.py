import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "state.py"
spec = importlib.util.spec_from_file_location("shared_state", SCRIPT)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config_path = self.base / "config.json"
        state.bootstrap(self.config_path, self.base / "db", self.base / "exchange", 7)
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.store = state.Store(self.config)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def request(self, op, actor, task=None, **fields):
        req = {"schema_version": 1, "request_id": uuid.uuid4().hex,
               "op": op, "actor": actor, **fields}
        if task is not None:
            req.update(task_id=task["task_id"], expected_revision=task["revision"])
            if task.get("lease_id"):
                req["lease_id"] = task["lease_id"]
        return req

    def create(self, controller, task_id="delegated"):
        result = self.store.apply(self.request(
            "create", controller, task_id=task_id,
            fields={"title": "delegation", "goal": "bind exact run"},
            execution_pool="local", read_only=True))
        return result["task"]

    def claim(self, task, worker):
        return self.store.apply(self.request("claim", worker, task))["task"]

    def bind(self, task, worker, run_id="run-1", **execution):
        return self.store.apply(self.request(
            "bind", worker, task,
            execution={"executor": "command", "run_id": run_id, **execution}))

    def test_legacy_task_bind_replay_fencing_and_controller_identity(self):
        created = self.create("controller-a")
        self.assertNotIn("delegation", created)
        claimed = self.claim(created, "worker-b")
        bound = self.bind(claimed, "worker-b", status_ref="status:run-1")["task"]
        self.assertEqual(bound["delegation"], {
            "controller": "controller-a", "worker": "worker-b", "generation": 1,
            "executor": "command", "run_id": "run-1", "status_ref": "status:run-1"})

        event_count = self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        replay = self.bind(bound, "worker-b", status_ref="status:run-1")["task"]
        self.assertEqual(replay, bound)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], event_count)
        with self.assertRaisesRegex(state.StateError, "already bound"):
            self.bind(bound, "worker-b", run_id="different-run", status_ref="status:run-1")

        stale = self.request("bind", "worker-b", claimed,
                             execution={"executor": "command", "run_id": "stale"})
        with self.assertRaisesRegex(state.StateError, "revision conflict"):
            self.store.apply(stale)
        old_owner = self.request("bind", "worker-a", bound,
                                 execution={"executor": "command", "run_id": "old-owner"})
        with self.assertRaisesRegex(state.StateError, "ownership"):
            self.store.apply(old_owner)

    def test_new_claim_clears_current_binding_but_keeps_event_history(self):
        task = self.claim(self.create("controller-a"), "worker-a")
        bound = self.bind(task, "worker-a", run_id="first-run")["task"]
        updated = self.store.apply(self.request("update", "worker-a", bound,
            fields={"last_confirmed_step": "Prior generation evidence"}))["task"]
        finished = self.store.apply(self.request("finish", "worker-a", updated, exit_code=0))["task"]
        queued = self.store.apply(self.request(
            "requeue", "controller-a", finished,
            stop_confirmed=True, stop_evidence="fixture worker finished",
            resume_evidence="controller reviewed prior run"))["task"]
        next_claim = self.claim(queued, "worker-b")

        self.assertNotIn("delegation", next_claim)
        self.assertEqual(next_claim["generation"], 2)
        observation = state.supervise(self.store, [task["task_id"]])["tasks"][0]
        self.assertEqual(observation["last_reported_progress_event"]["generation"], 1)
        self.assertEqual(observation["generation"], 2)
        prior = self.store.db.execute(
            "SELECT snapshot FROM events WHERE task_id=? AND action='bind'",
            (next_claim["task_id"],)).fetchone()
        self.assertEqual(json.loads(prior[0])["delegation"], bound["delegation"])

    def test_three_six_and_seven_participant_cohorts(self):
        for participant_count in (3, 6, 7):
            with self.subTest(participant_count=participant_count):
                self.config["pool_limits"]["local"] = participant_count
                tasks = []
                for index in range(participant_count):
                    controller = f"agent-{index}"
                    task = self.create(controller, task_id=f"cohort-{participant_count}-{index}")
                    worker = f"agent-{(index + 1) % participant_count}"
                    task = self.claim(task, worker)
                    task = self.bind(task, worker, run_id=f"run-{participant_count}-{index}")["task"]
                    tasks.append(task)
                self.assertEqual(len({task["worker"] for task in tasks}), participant_count)
                for index, task in enumerate(tasks):
                    self.assertEqual(task["delegation"]["controller"], f"agent-{index}")
                    self.assertEqual(task["delegation"]["worker"], f"agent-{(index + 1) % participant_count}")
                for task in tasks:
                    self.store.apply(self.request("finish", task["worker"], task, exit_code=0))

    def test_binding_identifiers_and_metadata_are_bounded_and_private(self):
        task = self.claim(self.create("controller-a"), "worker-a")
        invalid = self.request("bind", "worker-a", task,
                               execution={"executor": "bad/id", "run_id": "run"})
        with self.assertRaisesRegex(state.StateError, "invalid identifier"):
            self.store.apply(invalid)
        oversized = self.request("bind", "worker-a", task,
                                 execution={"executor": "command", "run_id": "run",
                                            "report_ref": "r" * 4097})
        with self.assertRaisesRegex(state.StateError, "4 KiB"):
            self.store.apply(oversized)

    def test_supervise_cli_returns_compact_snapshot_and_cursor(self):
        task = self.claim(self.create("controller-a"), "worker-a")
        task = self.bind(task, "worker-a")["task"]
        command = [sys.executable, str(SCRIPT), "--config", str(self.config_path),
                   "supervise", "--task-id", task["task_id"], "--after-seq", "0"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertGreaterEqual(snapshot["snapshot_seq"], task["revision"])
        self.assertEqual(snapshot["tasks"][0]["delegation"]["run_id"], "run-1")


if __name__ == "__main__":
    unittest.main()
