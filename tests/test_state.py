import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/state.py"
spec = importlib.util.spec_from_file_location("shared_state", SCRIPT)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.cp=base/'config.json'
        state.bootstrap(self.cp,base/'db',base/'exchange',2)
        self.config=json.loads(self.cp.read_text());self.config['heartbeat_seconds']=5
        self.store = state.Store(self.config)
        self.store.export()

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def request(self, op, actor="worker-a", task=None, **kwargs):
        if op in {'requeue','release'}:
            kwargs.setdefault('stop_confirmed',True);kwargs.setdefault('stop_evidence','fixture confirms all mutation work has stopped')
        r = {"schema_version": 1, "request_id": str(uuid.uuid4()), "op": op, "actor": actor, **kwargs}
        if task:
            r.update(task_id=task["task_id"], expected_revision=task["revision"])
            if task.get("lease_id"):
                r["lease_id"] = task["lease_id"]
        return r

    def create(self, **kwargs):
        kwargs.setdefault('execution_pool','local')
        kwargs.setdefault('resources',[{'kind':'path','path':str(Path(self.temp.name)/('target-'+str(uuid.uuid4()))),'mode':'write'}])
        return self.store.apply(self.request("create", fields={"title": "測試任務", "goal": "驗證續接"}, **kwargs))["task"]

    def symlink(self, link, target):
        try:link.symlink_to(target)
        except OSError as exc:
            if os.name=='nt' and getattr(exc,'winerror',None)==1314:
                self.skipTest('Windows account lacks symbolic-link privilege')
            raise

    def claim(self, task, actor="worker-a", **kwargs):
        return self.store.apply(self.request("claim", actor, task, **kwargs))["task"]

    def test_resume_last_confirmed_and_cross_agent(self):
        t = self.claim(self.create())
        t = self.store.apply(self.request("update", task=t, fields={"last_confirmed_step": "已驗證 A", "next_step": "檢查 B", "failed_attempts": ["方法 C 無效"]}))["task"]
        t = self.store.apply(self.request("finish", task=t, exit_code=0))["task"]
        reopened = state.Store(self.config)
        self.assertEqual(reopened.get(t["task_id"])["last_confirmed_step"], "已驗證 A")
        t = reopened.apply(self.request("requeue", "controller", t, resume_evidence="已讀 A 的證據，B 尚未執行"))["task"]
        t = reopened.apply(self.request("claim", "worker-b", t))["task"]
        self.assertEqual(t["generation"], 2)
        self.assertEqual(t["next_step"], "檢查 B")
        reopened.db.close()

    def test_revision_conflict_and_old_owner_fencing(self):
        original = self.claim(self.create())
        current = self.store.apply(self.request("update", task=original, fields={"summary": "新版本"}))["task"]
        with self.assertRaisesRegex(state.StateError, "revision conflict"):
            self.store.apply(self.request("update", task=original, fields={"summary": "過期覆蓋"}))
        t = self.store.apply(self.request("finish", task=current, exit_code=0))["task"]
        t = self.store.apply(self.request("requeue", "controller", t, resume_evidence="已確認可交接"))["task"]
        t = self.claim(t, "worker-b")
        forged = self.request("heartbeat", "worker-a", t)
        forged["lease_id"] = original["lease_id"]
        with self.assertRaisesRegex(state.StateError, "ownership"):
            self.store.apply(forged)

    def test_three_concurrent_claims_only_two(self):
        tasks = [self.create() for _ in range(3)]
        config = self.config
        requests = [self.request("claim", "worker" + str(i), t) for i, t in enumerate(tasks)]
        def run(r):
            s = state.Store(config)
            try:
                s.apply(r)
                return True
            except state.StateError:
                return False
            finally:
                s.db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(run, requests))
        self.assertEqual(sum(results), 2)
        self.assertEqual(sum(t["status"] == "running" for t in self.store.all()), 2)

    def test_same_task_claim_race_only_once(self):
        t = self.create()
        a = self.claim(t)
        with self.assertRaises(state.StateError):
            self.claim(t, "worker-b")
        self.assertEqual(self.store.get(t["task_id"])["lease_id"], a["lease_id"])

    def test_request_replay_idempotent_and_collision_rejected(self):
        r = self.request("create", fields={"title": "a", "goal": "b"}, source_key="slack:channel:1")
        first = self.store.apply(r)
        self.assertEqual(first, self.store.apply(r))
        second = dict(r, request_id=str(uuid.uuid4()))
        self.assertEqual(first["task"]["task_id"], self.store.apply(second)["task"]["task_id"])
        with self.assertRaisesRegex(state.StateError, "reused"):
            self.store.apply(dict(r, fields={"title": "changed", "goal": "b"}))
        self.assertEqual(len(self.store.all()), 1)


    def test_original_live_pid_retains_capacity_and_release_refused(self):
        t = self.claim(self.create(), pid=os.getpid())
        with self.store.transaction():
            t["heartbeat_epoch"] = 0
            self.store.save(t, "test", "expire")
        self.store.reconcile()
        t = self.store.get(t["task_id"])
        self.assertEqual(t["status"], "running")
        with self.assertRaisesRegex(state.StateError, "still alive"):
            self.store.apply(self.request("release", task=t))

    def test_muse_file_only_roundtrip_partial_json_and_restart(self):
        r = self.request("create", "controller", fields={"title": "Muse 任務", "goal": "檔案操作"})
        path = Path(self.config["exchange"]) / "inbox" / (r["request_id"] + ".json")
        path.write_text('{"schema_version":')
        self.assertEqual(self.store.process_inbox(), [])
        path.write_text(json.dumps(r))
        s = state.Store(self.config)
        self.assertTrue(s.process_inbox()[0]["ok"])
        s.export()
        receipt = json.loads((path.parent.parent / "receipts" / path.name).read_text())
        self.assertEqual(receipt["task"]["created_by"], "controller")
        s.process_inbox()
        self.assertEqual(len(s.all()), 1)
        s.db.close()


    def test_acceptance_requires_independent_evidence(self):
        t = self.claim(self.create())
        t = self.store.apply(self.request("finish", task=t, exit_code=0))["task"]
        checks = {k: "已檢查證據" for k in ["source", "diff", "tests", "runtime"]}
        with self.assertRaisesRegex(state.StateError, "independent"):
            self.store.apply(self.request("accept", task=t, checks=checks))
        t = self.store.apply(self.request("accept", "controller", t, checks=checks))["task"]
        self.assertEqual(t["status"], "accepted")

    def test_secrets_scrubbed_and_paths_cannot_escape(self):
        t = self.store.apply(self.request("create", fields={"title": "secret", "goal": "Bearer abcdefghijklmnop"}))["task"]
        self.assertNotIn("abcdefghijklmnop", json.dumps(t))
        with self.assertRaises(state.StateError):
            self.create(task_id="../escape")
        root = Path(self.config["exchange"])
        target = Path(self.temp.name)/"outside"
        target.mkdir()
        r = self.request("create", fields={"title":"escape", "goal":"blocked"})
        link = root / "inbox" / (r["request_id"]+".json")
        original = target / "external.json"
        original.write_text(json.dumps(r))
        self.symlink(link,original)
        self.assertEqual(self.store.process_inbox(), [])
        (root / "overview.md").unlink()
        self.symlink(root / "overview.md",target/"victim")
        with self.assertRaises(state.StateError):
            self.store.export()
        self.assertFalse((target/"victim").exists())

    def test_history_keeps_confirmed_evidence(self):
        t = self.claim(self.create())
        for step in ["A 已完成", "B 已完成"]:
            t = self.store.apply(self.request("update", task=t, fields={"last_confirmed_step":step}))["task"]
        snapshots = [json.loads(r[0]) for r in self.store.db.execute("SELECT snapshot FROM events WHERE task_id=?",(t["task_id"],))]
        self.assertIn("A 已完成", [s["last_confirmed_step"] for s in snapshots])

    def test_malformed_pid_and_checks_cannot_stop_consumer(self):
        task = self.create()
        r = self.request("claim", task=task, pid={"bad":"type"})
        root = Path(self.config["exchange"])
        p = root / "inbox" / (r["request_id"] + ".json")
        p.write_text(json.dumps(r))
        self.assertFalse(self.store.process_inbox()[0]["ok"])
        t = self.claim(task)
        t = self.store.apply(self.request("finish", task=t, exit_code=0))["task"]
        with self.assertRaises(state.StateError):
            self.store.apply(self.request("accept", "controller", t, checks=None))
        self.assertFalse(self.store.process_inbox()[0]["ok"])

    def test_failed_file_request_stays_failed_when_capacity_changes(self):
        a, b = [self.claim(self.create()) for _ in range(2)]
        t = self.create()
        r = self.request("claim", "worker-b", t)
        path = Path(self.config["exchange"]) / "inbox" / (r["request_id"]+".json")
        path.write_text(json.dumps(r))
        self.assertFalse(self.store.process_inbox()[0]["ok"])
        self.store.apply(self.request("finish", task=a, exit_code=0))
        self.assertFalse(self.store.process_inbox()[0]["ok"])
        self.assertEqual(self.store.get(t["task_id"])["status"], "queued")





    def test_rejected_stale_finish_cannot_modify_report(self):
        old = self.claim(self.create(), "worker-b")
        t = self.store.apply(self.request("finish", "worker-b", old, exit_code=0))["task"]
        t = self.store.apply(self.request("requeue", "controller", t, resume_evidence="舊工作者已結束"))["task"]
        current = self.claim(t, "worker-b")
        path = self.store.root/"reports"/(current["task_id"]+"-"+current["lease_id"]+".md")
        path.parent.mkdir(exist_ok=True); path.write_text("CURRENT EVIDENCE")
        stale=self.request("finish", "worker-b", current,exit_code=0,report="STALE OVERRIDE");stale["lease_id"]=old["lease_id"]
        with self.assertRaisesRegex(state.StateError,"ownership"):
            self.store.apply(stale)
        self.assertEqual(path.read_text(),"CURRENT EVIDENCE")

    def test_cloud_third_distinct_target_allowed(self):
        tasks=[self.create(execution_pool='cloud') for _ in range(3)]
        for t in tasks:self.claim(t)
        self.assertEqual(sum(t['status']=='running' for t in self.store.all()),3)

    def test_same_target_two_writers_rejected(self):
        resources=[{'kind':'path','path':str(Path(self.temp.name)/'same'),'mode':'write'}]
        self.claim(self.create(execution_pool='cloud',resources=resources))
        with self.assertRaisesRegex(state.StateError,'resource conflict'):
            self.claim(self.create(execution_pool='cloud',resources=resources),'antigravity')

    def test_read_sharing_and_parent_child(self):
        parent=Path(self.temp.name)/'project'
        resources=[{'kind':'path','path':str(parent),'mode':'read'}]
        self.claim(self.create(execution_pool='cloud',resources=resources))
        self.claim(self.create(execution_pool='cloud',resources=resources),'reader2')
        with self.assertRaisesRegex(state.StateError,'resource conflict'):
            self.claim(self.create(execution_pool='cloud',resources=[{'kind':'path','path':str(parent/'file'),'mode':'write'}]))
        self.claim(self.create(execution_pool='cloud',resources=[{'kind':'path','path':str(Path(self.temp.name)/'project-other'),'mode':'write'}]))

    def test_alias_and_all_or_nothing(self):
        p=Path(self.temp.name)/'existing';p.write_text('x')
        alias=Path(self.temp.name)/'alias';self.symlink(alias,p)
        a=self.claim(self.create(execution_pool='cloud',resources=[{'kind':'path','path':str(p),'mode':'write'}]))
        with self.assertRaisesRegex(state.StateError,'resource conflict'):
            self.claim(self.create(execution_pool='cloud',resources=[{'kind':'path','path':str(alias),'mode':'write'}]))
        other=Path(self.temp.name)/'other'
        with self.assertRaises(state.StateError):
            self.claim(self.create(execution_pool='cloud',resources=[{'kind':'path','path':str(other),'mode':'write'},{'kind':'path','path':str(p),'mode':'write'}]))
        self.claim(self.create(execution_pool='cloud',resources=[{'kind':'path','path':str(other),'mode':'write'}]))

    def test_undeclared_scope_does_not_claim_and_plan_changes(self):
        t=self.create(execution_pool=None,resources=[])
        with self.assertRaisesRegex(state.StateError,'declaration required'):
            self.claim(t)
        t=self.store.apply(self.request('plan','muse',t,execution_pool='cloud',resources=[{'kind':'service','name':'test-service','mode':'write'}]))['task']
        t=self.claim(t)
        with self.assertRaises(state.StateError):
            self.store.apply(self.request('plan','muse',t,resources=[]))


    def test_local_two_plus_cloud_three_can_coexist(self):
        for _ in range(2):self.claim(self.create(execution_pool='local'))
        for _ in range(3):self.claim(self.create(execution_pool='cloud'))
        with self.assertRaisesRegex(state.StateError,'capacity'):
            self.claim(self.create(execution_pool='local'))
        self.assertEqual(sum(t['status']=='running'for t in self.store.all()),5)

    def test_service_lock_and_scope_redeclaration_after_stop(self):
        r=[{'kind':'service','name':'service:example','mode':'write'}]
        t=self.claim(self.create(execution_pool='cloud',resources=r))
        with self.assertRaisesRegex(state.StateError,'resource conflict'):
            self.claim(self.create(execution_pool='cloud',resources=r))
        t=self.store.apply(self.request('finish',task=t,exit_code=0))['task']
        t=self.store.apply(self.request('plan','muse',t,resources=[{'kind':'service','name':'service:other','mode':'write'}]))['task']
        t=self.store.apply(self.request('requeue','muse',t,resume_evidence='已確認上一工作者停止'))['task']
        self.claim(t)
        self.claim(self.create(execution_pool='cloud',resources=r))

    def test_case_alias_and_hardlink_alias(self):
        p=Path(self.temp.name)/'CaseTarget';p.write_text('x')
        alias=Path(self.temp.name)/'hardlink';os.link(p,alias)
        self.claim(self.create(execution_pool='cloud',resources=[{'kind':'path','path':str(p),'mode':'write'}]))
        with self.assertRaisesRegex(state.StateError,'resource conflict'):
            self.claim(self.create(execution_pool='cloud',resources=[{'kind':'path','path':str(alias),'mode':'write'}]))
        if state.path_key(p)==state.path_key(p.with_name('casetarget')):
            with self.assertRaisesRegex(state.StateError,'resource conflict'):
                self.claim(self.create(execution_pool='cloud',resources=[{'kind':'path','path':str(p.with_name('casetarget')),'mode':'write'}]))

    def test_symlink_drift_before_claim_requires_redeclaration(self):
        a,b=Path(self.temp.name)/'a',Path(self.temp.name)/'b';a.mkdir();b.mkdir()
        link=Path(self.temp.name)/'link';self.symlink(link,a)
        t=self.create(execution_pool='cloud',resources=[{'kind':'path','path':str(link/'new-file'),'mode':'write'}])
        link.unlink();self.symlink(link,b)
        with self.assertRaisesRegex(state.StateError,'path changed'):
            self.claim(t)



    def test_unscoped_blocks_paths_and_services(self):
        t=self.claim(self.create(execution_pool='cloud',resources=[{'kind':'unscoped','mode':'write'}]))
        for r in [[{'kind':'path','path':str(Path(self.temp.name)/'x'),'mode':'write'}], [{'kind':'service','name':'arbitrary-new-service','mode':'write'}]]:
            with self.assertRaisesRegex(state.StateError,'resource conflict'):self.claim(self.create(execution_pool='cloud',resources=r))
        self.store.apply(self.request('finish',task=t,exit_code=0))
        self.claim(self.create(execution_pool='cloud',resources=[{'kind':'service','name':'arbitrary-new-service','mode':'write'}]))
        with self.assertRaisesRegex(state.StateError,'resource conflict'):
            self.claim(self.create(execution_pool='cloud',resources=[{'kind':'unscoped','mode':'write'}]))




if __name__ == "__main__":
    unittest.main()
