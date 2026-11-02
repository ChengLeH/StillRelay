#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, fcntl, json, os, pathlib, subprocess, tempfile, time, sys, uuid

ORCH = pathlib.Path.home() / ".chatgpt/local-orchestration"
LEASES = ORCH / "leases"
MAX_ACTIVE = 2
LEASE_TTL = 90
TERMINATION_REASONS = {
    "completed", "user_stopped", "context_rollover", "worker_failed",
    "stale_heartbeat", "explicit_controller_stop", "budget_exhausted",
}

def now():
    return dt.datetime.now().astimezone()

def iso(ts=None):
    return (ts or now()).isoformat(timespec="seconds")

def atomic_json(path, obj):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="."+path.name+".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def read_json(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default

def pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def git_capture(project, *args):
    try:
        p = subprocess.run(["git","-C",project,*args], text=True, capture_output=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None

def git_metadata(project, base_commit=None):
    if not project:
        return {"base_commit":base_commit,"changed_files":[],"diff_stat":None}
    base = base_commit or git_capture(project,"rev-parse","HEAD")
    if base:
        names = git_capture(project,"diff","--name-only",base)
        stat = git_capture(project,"diff","--stat",base)
    else:
        names = git_capture(project,"diff","--name-only")
        stat = git_capture(project,"diff","--stat")
    return {
        "base_commit": base,
        "changed_files": [x for x in (names or "").splitlines() if x],
        "diff_stat": stat or None,
    }

def status_path(run_dir):
    return pathlib.Path(run_dir) / "status.json"

def init_status(run_dir, project, workstream_id, generation, session_id, next_action="worker_running"):
    p = status_path(run_dir)
    obj = {
        "schema_version": 1,
        "state": "running",
        "workstream_id": workstream_id or None,
        "generation": int(generation or 0),
        "session_id": session_id or None,
        "started_at": iso(),
        "last_activity": iso(),
        "stopped_at": None,
        "exit_code": None,
        "termination_reason": None,
        "next_action": next_action,
        "tests_run": [],
    }
    obj.update(git_metadata(project))
    atomic_json(p, obj)
    return obj

def update_status(run_dir, **changes):
    p = status_path(run_dir)
    obj = read_json(p, {})
    if not obj:
        raise SystemExit(f"missing status.json: {p}")
    obj.update({k:v for k,v in changes.items() if v is not _UNSET})
    obj["last_activity"] = iso()
    atomic_json(p, obj)
    return obj

def refresh_git(run_dir, project):
    obj = read_json(status_path(run_dir), {})
    obj.update(git_metadata(project, obj.get("base_commit")))
    obj["last_activity"] = iso()
    atomic_json(status_path(run_dir), obj)
    return obj

def mark_worker_done(run_dir, project, exit_code, reason=None):
    code = int(exit_code)
    if reason is None:
        reason = "completed" if code == 0 else ("budget_exhausted" if code == 87 else "worker_failed")
    if reason not in TERMINATION_REASONS:
        raise SystemExit(f"invalid termination_reason: {reason}")
    obj = read_json(status_path(run_dir), {})
    obj.update(git_metadata(project, obj.get("base_commit")))
    obj.update({
        "state":"worker_done",
        "exit_code":code,
        "termination_reason":reason,
        "stopped_at":iso(),
        "last_activity":iso(),
        "next_action":"controller_review",
    })
    atomic_json(status_path(run_dir), obj)
    return obj

def mark_stopped(run_dir, reason):
    if reason not in TERMINATION_REASONS:
        raise SystemExit(f"invalid termination_reason: {reason}")
    obj = read_json(status_path(run_dir), {})
    if not obj:
        raise SystemExit("missing status.json")
    obj.update({"state":"worker_done","stopped_at":iso(),"termination_reason":reason,
                "last_activity":iso(),"next_action":"controller_review"})
    atomic_json(status_path(run_dir), obj)
    return obj

def mark_review_required(run_dir):
    current = read_json(status_path(run_dir), {})
    if current.get("state") != "worker_done":
        raise SystemExit(f"run is not ready for review: {current.get('state')}")
    obj = update_status(run_dir, state="review_required", next_action="controller_acceptance_required")
    return obj

def accept_run(run_dir, source_checked, diff_checked, tests_checked, runtime_checked):
    checks = {
        "source_checked": bool(source_checked),
        "diff_checked": bool(diff_checked),
        "tests_checked": bool(tests_checked),
        "runtime_checked": bool(runtime_checked),
    }
    if not all(checks.values()):
        raise SystemExit("all acceptance checks must be true")
    rd = pathlib.Path(run_dir)
    st = read_json(rd/"status.json", {})
    if st.get("state") != "review_required":
        raise SystemExit(f"run is not ready for acceptance: {st.get('state')}")
    acc = {
        "schema_version":1, **checks,
        "accepted_at":iso(),
        "accepted_by":"controller",
    }
    atomic_json(rd/"acceptance.json", acc)
    st.update({"state":"accepted","last_activity":iso(),"next_action":None})
    atomic_json(rd/"status.json", st)
    return acc

def validate_handoff(path, mutation_targets=None, expected_generation=None):
    obj = read_json(path, {})
    required = ["changed_files","verified_facts","tests","known_failures","next_action"]
    missing = [k for k in required if k not in obj]
    if missing:
        raise SystemExit("handoff missing fields: "+",".join(missing))
    if expected_generation is not None and int(obj.get("to_generation") or -1) != int(expected_generation):
        raise SystemExit("handoff generation does not match target generation")
    for k in required[:-1]:
        if not isinstance(obj.get(k), list):
            raise SystemExit(f"handoff {k} must be a list")
    if not isinstance(obj.get("next_action"), str):
        raise SystemExit("handoff next_action must be a string")
    mutates = False
    if mutation_targets:
        try:
            mutates = any(x.strip() for x in pathlib.Path(mutation_targets).read_text(encoding="utf-8").splitlines())
        except Exception:
            pass
    if mutates and not obj.get("changed_files") and not str(obj.get("changed_files_reason") or "").strip():
        raise SystemExit("mutation exists but changed_files is empty without changed_files_reason")
    return obj

def lease_path(task_id):
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in task_id)
    return LEASES / f"{safe}.json"

def _lease_lock():
    LEASES.mkdir(parents=True, exist_ok=True)
    fh = open(LEASES/".lock","a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh

def _parse_time(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z","+00:00"))
    except Exception:
        return None

def lease_process_alive(obj):
    group=obj.get('process_group')
    if group:
        if not isinstance(group,int) or isinstance(group,bool) or group<=0:return True
        try:os.killpg(group,0);return True
        except ProcessLookupError:pass
        except OSError:return True
    if pid_alive(obj.get("pid")):return True
    if obj.get("run_dir"):
        child=pathlib.Path(obj["run_dir"])/"child_pid"
        if child.exists():
            try:return pid_alive(child.read_text().strip())
            except OSError:return True
    return False

def _active_leases(exclude=None):
    rows=[]
    nowv=now()
    for p in LEASES.glob("*.json"):
        obj=read_json(p,{})
        if not obj:
            continue
        if exclude and obj.get("task_id")==exclude:
            continue
        alive=lease_process_alive(obj)
        exp=_parse_time(obj.get("expires_at"))
        if alive or (exp and exp > nowv):
            rows.append((p,obj))
        else:
            try:p.unlink()
            except FileNotFoundError:pass
    return rows

def lease_acquire(task_id, workstream_id, generation, pid, project=None, run_dir=None):
    fh=_lease_lock()
    try:
        rows=_active_leases()
        if any(p == lease_path(task_id) for p, _ in rows):
            raise SystemExit("task already holds an active lease")
        if len(rows) >= MAX_ACTIVE:
            raise SystemExit(75)
        # Optional resource guard. Existing standalone installations keep their
        # original policy when no shared-task-state config is present.
        config_path=pathlib.Path(os.environ.get("SHARED_TASK_STATE_CONFIG",str(pathlib.Path.home()/".config/shared-task-state/config.json")))
        guarded_resources=None
        if config_path.exists():
            config=json.loads(config_path.read_text())
            guard=config.get("legacy_resource_guard")
            if guard:
                if not project or not pathlib.Path(project).is_absolute():
                    raise SystemExit("local target declaration required")
                request={"task_id":task_id,"resources":[{"kind":"path","path":str(pathlib.Path(project).resolve()),"mode":"write"}]}
                proc=subprocess.run([sys.executable,"-B",guard],input=json.dumps(request),capture_output=True,text=True,timeout=15)
                response=json.loads(proc.stdout or "{}")
                if proc.returncode or not response.get("ok"):
                    raise SystemExit(response.get("error","local resource guard failed"))
                guarded_resources=response["resources"]
        obj={
            "task_id":task_id,
            "workstream_id":workstream_id or task_id,
            "generation":int(generation or 0),
            "pid":int(pid),
            "heartbeat":iso(),
            "expires_at":iso(now()+dt.timedelta(seconds=LEASE_TTL)),
        }
        if guarded_resources is not None: obj.update(resources=guarded_resources,owner_token=uuid.uuid4().hex,run_dir=run_dir)
        atomic_json(lease_path(task_id),obj)
        return obj
    finally:
        fh.close()

def lease_heartbeat(task_id, pid=None, token=None):
    fh=_lease_lock()
    try:
        p=lease_path(task_id)
        obj=read_json(p,{})
        if not obj:
            raise SystemExit("lease missing")
        if obj.get("owner_token") and obj["owner_token"] != token:
            raise SystemExit("lease owner token mismatch")
        if pid is not None:
            obj["pid"]=int(pid)
        obj["heartbeat"]=iso()
        obj["expires_at"]=iso(now()+dt.timedelta(seconds=LEASE_TTL))
        atomic_json(p,obj)
        return obj
    finally:
        fh.close()

def lease_release(task_id, token=None):
    fh=_lease_lock()
    try:
        obj=read_json(lease_path(task_id),{})
        if obj.get("owner_token") and obj["owner_token"] != token:
            raise SystemExit("lease owner token mismatch")
        if obj.get("owner_token") and lease_process_alive(obj):
            raise SystemExit("lease process still alive; reservation retained")
        try: lease_path(task_id).unlink()
        except FileNotFoundError: pass
    finally:
        fh.close()

_UNSET = object()

def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("init-status"); p.add_argument("run_dir"); p.add_argument("project"); p.add_argument("workstream_id"); p.add_argument("generation"); p.add_argument("session_id")
    p=sub.add_parser("heartbeat"); p.add_argument("run_dir")
    p=sub.add_parser("worker-done"); p.add_argument("run_dir"); p.add_argument("project"); p.add_argument("exit_code",type=int); p.add_argument("--reason")
    p=sub.add_parser("stop"); p.add_argument("run_dir"); p.add_argument("reason")
    p=sub.add_parser("review-required"); p.add_argument("run_dir")
    p=sub.add_parser("accept"); p.add_argument("run_dir"); p.add_argument("--source-checked",action="store_true"); p.add_argument("--diff-checked",action="store_true"); p.add_argument("--tests-checked",action="store_true"); p.add_argument("--runtime-checked",action="store_true")
    p=sub.add_parser("validate-handoff"); p.add_argument("path"); p.add_argument("--mutation-targets"); p.add_argument("--expected-generation",type=int)
    p=sub.add_parser("lease-acquire"); p.add_argument("task_id"); p.add_argument("workstream_id"); p.add_argument("generation"); p.add_argument("pid",type=int); p.add_argument("--project"); p.add_argument("--run-dir")
    p=sub.add_parser("lease-heartbeat"); p.add_argument("task_id"); p.add_argument("--pid",type=int); p.add_argument("--token")
    p=sub.add_parser("lease-release"); p.add_argument("task_id"); p.add_argument("--token")
    p=sub.add_parser("lease-list")
    args=ap.parse_args()
    if args.cmd=="init-status": out=init_status(args.run_dir,args.project,args.workstream_id,args.generation,args.session_id)
    elif args.cmd=="heartbeat": out=update_status(args.run_dir)
    elif args.cmd=="worker-done": out=mark_worker_done(args.run_dir,args.project,args.exit_code,args.reason)
    elif args.cmd=="stop": out=mark_stopped(args.run_dir,args.reason)
    elif args.cmd=="review-required": out=mark_review_required(args.run_dir)
    elif args.cmd=="accept": out=accept_run(args.run_dir,args.source_checked,args.diff_checked,args.tests_checked,args.runtime_checked)
    elif args.cmd=="validate-handoff": out=validate_handoff(args.path,args.mutation_targets,args.expected_generation)
    elif args.cmd=="lease-acquire": out=lease_acquire(args.task_id,args.workstream_id,args.generation,args.pid,args.project,args.run_dir)
    elif args.cmd=="lease-heartbeat": out=lease_heartbeat(args.task_id,args.pid,args.token)
    elif args.cmd=="lease-release": lease_release(args.task_id,args.token); out={"released":True,"task_id":args.task_id}
    elif args.cmd=="lease-list": out={"leases":[x[1] for x in _active_leases()]}
    print(json.dumps(out,ensure_ascii=False))
if __name__=="__main__":
    main()
