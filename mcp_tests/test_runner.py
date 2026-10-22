"""Focused runner lifecycle tests against disposable SQLite task state."""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from shared_task_state import state

from shared_task_state_mcp import runner
from shared_task_state_mcp.backend import Backend


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner tests ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "config.json"
        state.bootstrap(self.config, self.base / "db", self.base / "exchange")
        self.backend = Backend(self.config)
        self.task = self.create_task()

    def request(self, op, *, task=None, actor="controller", **values):
        request = {
            "schema_version": 1,
            "request_id": uuid.uuid4().hex,
            "op": op,
            "actor": actor,
            **values,
        }
        if task is not None:
            request.update(task_id=task["task_id"], expected_revision=task["revision"])
            if task.get("lease_id"):
                request["lease_id"] = task["lease_id"]
        return request

    def create_task(self, *, resources=None):
        task_id = f"runner-{uuid.uuid4().hex}"
        return self.backend.apply(self.request(
            "create", task_id=task_id, fields={"title": "runner fixture", "goal": "run a local command"},
            execution_pool="local", resources=resources or [], read_only=not resources,
        ))["receipt"]["task"]

    def current(self, task_id=None):
        return self.backend.read(task_id or self.task["task_id"])["task"]

    def events(self, task_id=None):
        return self.backend.history(task_id or self.task["task_id"], 0, 100)["events"]

    def run_command(self, command, **kwargs):
        run_id = kwargs.pop("run_id", f"run-{uuid.uuid4().hex}")
        result = runner.run(self.config, self.task["task_id"], "worker-a", command,
                            heartbeat_seconds=0.05, run_id=run_id, **kwargs)
        if result.get("logs"):
            self.addCleanup(shutil.rmtree, Path(result["logs"]["stdout"]).parent, ignore_errors=True)
        return result

    def test_success_keeps_output_local_and_records_exact_binding_and_exit(self):
        local_secret = f"local-only-output-{uuid.uuid4().hex}"
        result = self.run_command([
            sys.executable, "-c",
            f"import sys; print({local_secret!r}); print('local-stderr', file=sys.stderr)",
        ], cwd=self.base)

        task = self.current()
        actions = [event["action"] for event in self.events()]
        self.assertEqual(result["status"], "worker_done")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["reservation_active"])
        self.assertEqual(task["status"], "worker_done")
        self.assertFalse(task["reservation_active"])
        self.assertEqual(task["exit_code"], 0)
        self.assertIsNone(task.get("acceptance"))
        self.assertEqual(task["delegation"]["controller"], "controller")
        self.assertEqual(task["delegation"]["worker"], "worker-a")
        self.assertEqual(task["delegation"]["generation"], 1)
        self.assertEqual(task["delegation"]["executor"], "command")
        self.assertEqual(task["delegation"]["run_id"], result["run_id"])
        self.assertEqual(actions, ["create", "claim", "bind", "finish"])
        self.assertIn(local_secret, Path(result["logs"]["stdout"]).read_text())
        self.assertIn("local-stderr", Path(result["logs"]["stderr"]).read_text())
        self.assertNotIn(local_secret, json.dumps(result))
        self.assertNotIn(local_secret, json.dumps(task))
        self.assertNotIn(local_secret, json.dumps(self.events()))

    def test_nonzero_child_records_actual_code_without_acceptance(self):
        result = self.run_command([
            sys.executable, "-c", "import sys; print('failed output'); sys.exit(7)",
        ])

        task = self.current()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["exit_code"], 7)
        self.assertFalse(task["reservation_active"])
        self.assertIsNone(task.get("acceptance"))
        self.assertNotIn("failed output", json.dumps(result))

    def test_real_resource_conflict_rejects_claim_without_starting_child(self):
        resource = [{"kind": "service", "mode": "write", "name": "runner-fixture-lock"}]
        self.task = self.backend.apply(self.request(
            "plan", task=self.task, actor="controller", resources=resource, read_only=False,
        ))["receipt"]["task"]
        holder = self.create_task(resources=resource)
        held = self.backend.apply(self.request("claim", task=holder, actor="holder"))["receipt"]["task"]
        before = self.current()

        with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("child launched")):
            result = self.run_command([sys.executable, "-c", "pass"])

        current = self.current()
        self.assertEqual(result["error"], "claim_rejected")
        self.assertFalse(result["reservation_active"])
        self.assertEqual(current["revision"], before["revision"])
        self.assertEqual(current["status"], "queued")
        self.assertEqual(self.current(held["task_id"])["lease_id"], held["lease_id"])
        self.assertEqual([event["action"] for event in self.events()], ["create", "plan"])

    def test_known_bind_rejection_releases_only_own_unstarted_lease(self):
        original_apply = Backend.apply
        releases = []

        def reject_bind(backend, request):
            if request["op"] == "bind":
                raise state.StateError("injected known binding rejection")
            if request["op"] == "release":
                releases.append(deepcopy(request))
            return original_apply(backend, request)

        with mock.patch.object(Backend, "apply", new=reject_bind):
            with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("child launched")):
                result = self.run_command([sys.executable, "-c", "pass"])

        task = self.current()
        release = next(event["task"] for event in self.events() if event["action"] == "release")
        self.assertEqual(result["status"], "not_started")
        self.assertFalse(result["reservation_active"])
        self.assertEqual(task["status"], "interrupted")
        self.assertFalse(task["reservation_active"])
        self.assertEqual(task["retired_lease_id"], release["retired_lease_id"])
        self.assertEqual(len(releases), 1)
        self.assertIs(releases[0]["stop_confirmed"], True)
        self.assertEqual(releases[0]["lease_id"], release["retired_lease_id"])
        self.assertIn("No child was launched", task["stop_evidence"])
        self.assertEqual([event["action"] for event in self.events()], ["create", "claim", "release"])

    def test_ambiguous_uncommitted_bind_response_retains_reservation(self):
        original_apply = Backend.apply

        def lose_bind_response(backend, request):
            if request["op"] == "bind":
                raise OSError("simulated lost response")
            return original_apply(backend, request)

        with mock.patch.object(Backend, "apply", new=lose_bind_response):
            with mock.patch.object(runner.subprocess, "Popen", side_effect=AssertionError("child launched")):
                result = self.run_command([sys.executable, "-c", "pass"])

        task = self.current()
        self.assertEqual(result["error"], "bind_outcome_unknown")
        self.assertTrue(result["reservation_active"])
        self.assertEqual(task["status"], "running")
        self.assertTrue(task["reservation_active"])
        self.assertIsNone(task.get("delegation"))
        self.assertEqual([event["action"] for event in self.events()], ["create", "claim"])

    def test_committed_bind_with_lost_response_is_read_and_run_once(self):
        original_apply = Backend.apply
        lost = False

        def commit_then_lose(backend, request):
            nonlocal lost
            result = original_apply(backend, request)
            if request["op"] == "bind" and not lost:
                lost = True
                raise OSError("simulated lost response after commit")
            return result

        with mock.patch.object(Backend, "apply", new=commit_then_lose):
            result = self.run_command([sys.executable, "-c", "print('once')"])

        self.assertEqual(result["status"], "worker_done")
        actions = [event["action"] for event in self.events()]
        self.assertEqual(actions.count("bind"), 1)
        self.assertEqual(actions[-1], "finish")

    def test_committed_receipts_are_used_when_projection_export_fails(self):
        def fail_projection(_store):
            raise state.StateError("simulated projection failure")

        with mock.patch.object(state.Store, "export", new=fail_projection):
            result = self.run_command([sys.executable, "-c", "pass"])

        self.assertEqual(result["status"], "worker_done")
        task = self.current()
        self.assertEqual(task["status"], "worker_done")
        actions = [event["action"] for event in self.events()]
        self.assertEqual(actions, ["create", "claim", "bind", "finish"])

    def test_sustained_child_receives_serial_real_heartbeats(self):
        result = self.run_command([sys.executable, "-c", "import time; time.sleep(0.30)"])
        events = self.events()
        heartbeat_events = [event for event in events if event["action"] == "heartbeat"]

        self.assertEqual(result["status"], "worker_done")
        self.assertGreaterEqual(len(heartbeat_events), 2)
        revisions = [event["task"]["revision"] for event in heartbeat_events]
        self.assertEqual(revisions, sorted(revisions))
        self.assertTrue(all(event["task"]["delegation"]["run_id"] == result["run_id"]
                            for event in heartbeat_events))

    def test_revision_conflict_refreshes_and_uses_new_heartbeat_request(self):
        original_apply = Backend.apply
        attempted = []
        injected = False

        def conflict_once(backend, request):
            nonlocal injected
            if request["op"] == "heartbeat":
                attempted.append(deepcopy(request))
                if not injected:
                    injected = True
                    current = Backend(self.config).read(request["task_id"])["task"]
                    competing = self.request("heartbeat", task=current, actor=request["actor"])
                    original_apply(Backend(self.config), competing)
            return original_apply(backend, request)

        with mock.patch.object(Backend, "apply", new=conflict_once):
            result = self.run_command([sys.executable, "-c", "import time; time.sleep(0.20)"])

        self.assertEqual(result["status"], "worker_done")
        self.assertGreaterEqual(len(attempted), 2)
        self.assertNotEqual(attempted[0]["request_id"], attempted[1]["request_id"])
        self.assertEqual(attempted[1]["expected_revision"], attempted[0]["expected_revision"] + 1)
        heartbeats = [event for event in self.events() if event["action"] == "heartbeat"]
        self.assertGreaterEqual(len(heartbeats), 2)

    def test_finish_lost_response_is_read_without_replaying_finish(self):
        original_apply = Backend.apply
        lost = False

        def commit_then_lose_finish(backend, request):
            nonlocal lost
            result = original_apply(backend, request)
            if request["op"] == "finish" and not lost:
                lost = True
                raise OSError("simulated lost finish response")
            return result

        with mock.patch.object(Backend, "apply", new=commit_then_lose_finish):
            result = self.run_command([sys.executable, "-c", "pass"])

        self.assertEqual(result["status"], "worker_done")
        actions = [event["action"] for event in self.events()]
        self.assertEqual(actions.count("finish"), 1)

    def test_signal_reaps_direct_child_and_retains_reservation(self):
        class SignalOnWait:
            def __init__(self):
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                if self.returncode is None:
                    # os.kill(SIGTERM) uses TerminateProcess on Windows and
                    # kills the test harness instead of invoking its handler.
                    signal.raise_signal(signal.SIGTERM)
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -signal.SIGTERM

            def kill(self):
                self.returncode = -signal.SIGKILL

        child = SignalOnWait()
        with mock.patch.object(runner.subprocess, "Popen", return_value=child):
            result = self.run_command([sys.executable, "-c", "pass"])

        task = self.current()
        actions = [event["action"] for event in self.events()]
        self.assertEqual(result["status"], "reservation_retained")
        self.assertTrue(result["reservation_active"])
        self.assertTrue(result["direct_child_reaped"])
        self.assertEqual(result["signal"], "SIGTERM")
        self.assertTrue(child.terminated)
        self.assertEqual(task["status"], "running")
        self.assertTrue(task["reservation_active"])
        self.assertNotIn("finish", actions)
        self.assertNotIn("release", actions)

    def test_spawn_failure_releases_bound_lease_without_claiming_a_child(self):
        with mock.patch.object(runner.subprocess, "Popen", side_effect=FileNotFoundError("missing command")):
            result = self.run_command(["missing-runner-fixture-command"])

        task = self.current()
        self.assertEqual(result["status"], "not_started")
        self.assertFalse(task["reservation_active"])
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["delegation"]["run_id"], result["run_id"])
        self.assertIn("command launch failed", task["stop_evidence"])
        self.assertEqual([event["action"] for event in self.events()],
                         ["create", "claim", "bind", "release"])

    def test_shell_string_is_rejected_before_claim(self):
        with self.assertRaises(TypeError):
            self.run_command("echo unsafe")
        self.assertEqual(self.current()["status"], "queued")
        self.assertEqual([event["action"] for event in self.events()], ["create"])

    def test_interval_must_be_below_configured_timeout_before_claim(self):
        with self.assertRaises(ValueError):
            runner.run(self.config, self.task["task_id"], "worker-a", [sys.executable, "-c", "pass"],
                       heartbeat_seconds=180)
        self.assertEqual(self.current()["status"], "queued")

    def test_nonfinite_interval_is_rejected_before_claim(self):
        with self.assertRaises(ValueError):
            runner.run(self.config, self.task["task_id"], "worker-a", [sys.executable, "-c", "pass"],
                       heartbeat_seconds=float("nan"))
        self.assertEqual(self.current()["status"], "queued")


if __name__ == "__main__":
    unittest.main()
