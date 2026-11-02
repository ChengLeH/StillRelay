#!/usr/bin/env python3
"""Bridge plug-in boundary: structured stdin, no shell interpolation."""
import json
from pathlib import Path
import re
import sys
import time
import uuid
from shared_state import Store, StateError, default_config, scrub


def perform(store, payload):
    actor = payload.get("actor", "antigravity")
    op = payload["op"]
    if op == "reconcile":
        changed = store.reconcile()
        store.export()
        return {"changed": changed}
    if op == "queued":
        return [t for t in store.all() if t["status"] == "queued" and (t.get("origin") or {}).get("target") == actor]
    if op == "enqueue":
        text = payload["text"]
        match = re.search(r"\[\[task:([A-Za-z0-9][A-Za-z0-9._-]{0,127})\]\]", text)
        if match:
            task = store.get(match.group(1))
            if task.get("legacy_readonly"):
                raise StateError("legacy snapshot cannot be dispatched")
            if task["status"] != "queued":
                return {"skip": True, "task": task}
            origin = task.get("origin")
            if origin and origin.get("target") != actor:
                raise StateError("task dispatch target mismatch")
            # A pre-created task can be explicitly bound to this delivery.
            if not origin:
                with store.transaction():
                    task = store.get(task["task_id"])
                    if task["status"] != "queued":
                        return {"skip": True, "task": task}
                    task["origin"] = {"kind": "slack", "channel": payload["channel"], "message_ts": payload["ts"],
                                      "thread_ts": payload.get("threadTs") or payload["ts"], "target": actor}
                    store.save(task, "slack-adapter", "bind-delivery")
            store.export()
            return {"skip": False, "task": task}
        request = {"schema_version": 1, "request_id": str(uuid.uuid4()), "actor": "slack-adapter", "op": "create",
                   "source_key": "slack:" + payload["channel"] + ":" + payload["ts"],
                   "origin": {"kind": "slack", "channel": payload["channel"], "message_ts": payload["ts"],
                              "thread_ts": payload.get("threadTs") or payload["ts"], "target": actor},
                   "fields": {"title": text[:80], "goal": text, "next_step": "等待指定工作者接單"}}
        task = store.apply(request)["task"]
        store.export()
        return {"skip": task["status"] != "queued", "task": task}
    if op == 'heartbeat':
        max_retries = 3
        for attempt in range(max_retries):
            task = store.get(payload["task_id"])
            request = {"schema_version": 1, "request_id": str(uuid.uuid4()), "actor": actor, "op": op,
                       "task_id": task["task_id"], "expected_revision": task["revision"], "lease_id": payload["lease_id"]}
            if "pid" in payload:
                request["pid"] = payload["pid"]
            if "process_group" in payload:
                request["process_group"] = payload["process_group"]
            try:
                result = store.apply(request)
                store.export()
                return result
            except StateError as err:
                if "revision conflict" in str(err) and attempt < max_retries - 1:
                    time.sleep(0.1)
                    continue
                raise

    task = store.get(payload["task_id"])
    # Existing plain Slack messages have no structured scope. A configured
    # delivery profile supplies the actual backend and conservative reservation.
    if op == 'claim' and task['status']=='queued':
        profile=store.config.get('slack_dispatch_profiles',{}).get(actor)
        if profile and (task.get('origin') or {}).get('target')==actor:
            declaration={}
            if task.get('execution_pool') is None:declaration['execution_pool']=profile['execution_pool']
            if not task.get('resources') and not task.get('read_only'):
                declaration['resources']=profile['fallback_resources']
            if declaration:
                task=store.apply({'schema_version':1,'request_id':str(uuid.uuid4()),'actor':'slack-adapter','op':'plan',
                    'task_id':task['task_id'],'expected_revision':task['revision'],**declaration})['task']
    request = {"schema_version": 1, "request_id": str(uuid.uuid4()), "actor": actor, "op": op,
               "task_id": task["task_id"], "expected_revision": task["revision"]}
    if op in {'heartbeat','update','finish','release'}:
        request["lease_id"] = payload["lease_id"]
    for key in ["pid", "exit_code", "fields", "report",'execution_pool','resources','read_only']:
        if key in payload:
            request[key] = payload[key]
    result = store.apply(request)
    store.export()
    return result


def main():
    try:
        store = Store(json.loads(default_config().read_text()))
        result = perform(store, json.load(sys.stdin))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": scrub(str(exc))}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
