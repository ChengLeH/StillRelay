#!/usr/bin/env python3
"""Local, transactional task continuity. Python standard library only."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from types import SimpleNamespace
import unicodedata
sys.path.insert(0, str(Path(__file__).resolve().parent))
from privacy_guard import PrivacyError, scrub as privacy_scrub, check_metadata, guard
import evidence

try:
    import fcntl
except ImportError:
    fcntl = None

_CASE_CAPS = {}
VERSION = 1
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ACTIVE = {"running"}
EDITABLE = {"title", "goal", "constraints", "phase", "summary", "last_confirmed_step",
            "verified_facts", "failed_attempts", "next_step", "artifacts", "backups", "blockers"}
LIST_FIELDS = {"constraints", "verified_facts", "failed_attempts", "artifacts", "backups", "blockers"}



class StateError(Exception):
    pass


evidence.StateError = StateError


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise StateError("invalid identifier")
    metadata(value)
    return value


def scrub(value):
    try:
        return privacy_scrub(value)
    except PrivacyError:
        raise StateError("privacy guard rejected content") from None


def metadata(value):
    try:
        return check_metadata(value)
    except PrivacyError:
        raise StateError("credentials are forbidden in identifiers and resource metadata") from None


def protected(value, scanner=None):
    try:
        return guard(value, scanner)
    except PrivacyError:
        raise StateError("privacy guard rejected content or scanner unavailable") from None


def checked_fields(fields):
    if not isinstance(fields, dict) or set(fields) - EDITABLE:
        raise StateError("unknown task fields")
    for k, v in fields.items():
        if k in LIST_FIELDS:
            expected_type = dict if k == "verified_facts" else str
            if not isinstance(v, list) or len(v) > 100 or any(not isinstance(x, expected_type) for x in v):
                raise StateError(("list of sourced fact objects required: " if k == "verified_facts" else "list of strings required: ") + k)
        elif not isinstance(v, str):
            raise StateError("string required: " + k)
    if len(json.dumps(fields, ensure_ascii=False).encode()) > 32768:
        raise StateError("task update exceeds 32 KiB")
    return scrub(fields)


PROGRESS_FIELDS = ("phase", "summary", "last_confirmed_step", "next_step", "blockers")
EXECUTION_FIELDS = {"executor", "run_id", "status_ref", "report_ref"}

def checked_execution(execution):
    if not isinstance(execution, dict) or not {"executor", "run_id"} <= set(execution) or set(execution) - EXECUTION_FIELDS:
        raise StateError("execution requires executor and run_id, with optional status_ref and report_ref")
    result = {"executor": safe_id(execution["executor"]), "run_id": safe_id(execution["run_id"])}
    for key in ("status_ref", "report_ref"):
        if key in execution:
            value = execution[key]
            if not isinstance(value, str):
                raise StateError(key + " must be a bounded string")
            metadata(value)
            result[key] = value
    return result

def delegation_for(task, execution):
    result = {"controller": safe_id(task["created_by"]), "worker": safe_id(task["worker"]),
              "generation": task["generation"], **execution}
    metadata(result)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 4096:
        raise StateError("delegation metadata exceeds 4 KiB")
    return result

def output_fingerprint(task):
    """Fingerprint only current-generation artifacts verified from local file bytes."""
    artifacts = sorted((entry['path'], entry['sha256'], entry['size_bytes'])
                       for entry in task.get('artifact_evidence', [])
                       if entry.get('generation') == task.get('generation')
                       and entry.get('verification') == 'local_sha256_match')
    if not artifacts:
        return None
    payload = {'artifacts': artifacts}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()

def current_review_result(task, review):
    worker_result = task.get('worker_result')
    return (isinstance(review, dict) and isinstance(worker_result, dict)
            and review.get('worker_generation') == task.get('generation') == worker_result.get('generation')
            and review.get('worker_revision') == worker_result.get('completed_revision')
            and review.get('output_fingerprint') == worker_result.get('output_fingerprint')
            and task.get('result_fingerprint') == worker_result.get('output_fingerprint'))

def supervise(store, task_ids, after_seq=0):
    """Read a compact supervision snapshot inside the caller's read transaction."""
    if not isinstance(task_ids, list) or len(task_ids) > 100:
        raise StateError("task_ids must be a list of at most 100 exact task IDs")
    if any(not isinstance(task_id, str) for task_id in task_ids):
        raise StateError("task_ids must contain exact task IDs")
    checked_ids = [safe_id(task_id) for task_id in task_ids]
    if len(set(checked_ids)) != len(checked_ids):
        raise StateError("task_ids must be unique")
    if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
        raise StateError("after_seq must be a nonnegative integer")

    snapshot_seq = store.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
    rows = []
    if checked_ids:
        placeholders = ",".join("?" for _ in checked_ids)
        rows = store.db.execute("SELECT id,data FROM tasks WHERE id IN (" + placeholders + ")",
                                checked_ids).fetchall()
    by_id = {row[0]: json.loads(row[1]) for row in rows}
    missing = [task_id for task_id in checked_ids if task_id not in by_id]
    if missing:
        raise StateError("task not found")

    latest_progress = {}
    if checked_ids:
        placeholders = ",".join("?" for _ in checked_ids)
        rows = store.db.execute(
            "SELECT e.task_id,e.seq,e.at,e.actor,e.snapshot FROM events e "
            "JOIN (SELECT task_id,MAX(seq) AS seq FROM events WHERE task_id IN (" + placeholders + ") "
            "AND action='update' AND seq>? AND seq<=? GROUP BY task_id) latest "
            "ON e.task_id=latest.task_id AND e.seq=latest.seq",
            (*checked_ids, after_seq, snapshot_seq)).fetchall()
        for task_id, seq, at, actor, snapshot in rows:
            progress = json.loads(snapshot)
            latest_progress[task_id] = {"seq": seq, "at": at, "actor": actor,
                "generation": progress.get("generation"),
                "progress": {key: progress.get(key) for key in PROGRESS_FIELDS}}

    observed_at = time.time()
    summaries = []
    for task_id in checked_ids:
        task = by_id[task_id]
        heartbeat_epoch = task.get("heartbeat_epoch", 0)
        heartbeat_age = max(0.0, observed_at - heartbeat_epoch) if heartbeat_epoch else None
        recorded_binding = task.get("delegation")
        binding_current = (isinstance(recorded_binding, dict) and
                           recorded_binding.get("worker") == task.get("worker") and
                           recorded_binding.get("generation") == task.get("generation"))
        summaries.append({
            "task_id": task_id,
            "title": task.get("title"),
            "status": task.get("status"),
            "revision": task.get("revision"),
            "controller": task.get("created_by"),
            "worker": task.get("worker"),
            "generation": task.get("generation"),
            "binding_status": "current" if binding_current else "stale" if recorded_binding is not None else "unbound",
            "delegation": recorded_binding if binding_current else None,
            "heartbeat_age_seconds": round(heartbeat_age, 3) if heartbeat_age is not None else None,
            "heartbeat_fresh": bool(heartbeat_age is not None and heartbeat_age <= store.ttl),
            "progress": {key: task.get(key) for key in PROGRESS_FIELDS},
            "last_reported_progress_event": latest_progress.get(task_id),
            "review_needed": task.get("status") in {"worker_done", "blocked", "interrupted"},
            "review_policy": task.get("review_policy", "legacy"),
            "review_status": task.get("review", {}).get("status", "not_started")
                if isinstance(task.get("review"), dict) else "not_started",
        })
    return {"ok": True, "after_seq": after_seq,
            "snapshot_seq": snapshot_seq, "tasks": summaries}

def path_key(path):
    p=Path(path)
    ancestor=p
    while not ancestor.exists():ancestor=ancestor.parent
    if sys.platform != 'darwin':return str(p)
    dev=ancestor.stat().st_dev
    if dev not in _CASE_CAPS:
        # Native volume capability flags, from Darwin sys/attr.h. This reads
        # filesystem metadata without creating probe files in user targets.
        import ctypes, struct
        attributes=struct.pack('=HHIIIII',5,0,0,0x80020000,0,0,0)
        buf=ctypes.create_string_buffer(36)
        libc=ctypes.CDLL(None,use_errno=True)
        getter=libc.getattrlist
        getter.argtypes=[ctypes.c_char_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_ulong]
        attrbuf=ctypes.create_string_buffer(attributes)
        if getter(os.fsencode(ancestor),attrbuf,buf,len(buf),0)!=0:
            raise StateError('cannot verify resource filesystem case sensitivity')
        values=struct.unpack('=I8I',buf.raw)
        if not values[5]&0x100:raise StateError('filesystem case capability unavailable')
        _CASE_CAPS[dev]=bool(values[1]&0x100)
    text=unicodedata.normalize('NFC',str(p))
    return text if _CASE_CAPS[dev] else text.casefold()


def resources_checked(resources, refresh=False):
    metadata(resources)
    if not isinstance(resources, list) or len(resources) > 100:
        raise StateError("resources must be a bounded list")
    out = []
    for r in resources:
        if not isinstance(r, dict) or r.get('mode') not in {'read','write'}:
            raise StateError('resource mode must be read or write')
        if r.get('kind') == 'path':
            raw = r.get('requested_path', r.get('path'))
            if not isinstance(raw,str) or not Path(raw).is_absolute():
                raise StateError('absolute resource path required')
            p = Path(raw).resolve()
            if refresh and r.get('path') != str(p):
                raise StateError('resource path changed; re-declare scope before claim')
            item = {'kind':'path','mode':r['mode'],'path':str(p),'requested_path':raw,'path_key':path_key(p)}
            if p.exists():
                st = p.stat()
                item['inode'] = [st.st_dev,st.st_ino]
        elif r.get('kind') == 'unscoped':
            item = {'kind':'unscoped','mode':r['mode']}
        elif r.get('kind') == 'service':
            name = r.get('name')
            if not isinstance(name,str) or not name.strip() or len(name)>256:
                raise StateError('stable service resource name required')
            item = {'kind':'service','mode':r['mode'],'name':name}
        else:
            raise StateError('unknown resource kind')
        if item not in out:out.append(item)
    return out


def resources_conflict(a,b):
    if a['mode'] == b['mode'] == 'read':return False
    if 'unscoped' in {a['kind'],b['kind']}:return True
    if a['kind'] != b['kind']:return False
    if a['kind'] == 'service':return a['name'] == b['name']
    x,y = Path(a.get('path_key',a['path'])),Path(b.get('path_key',b['path']))
    return x == y or x in y.parents or y in x.parents or bool(a.get('inode') and a.get('inode') == b.get('inode'))


def write_atomic(path, value):
    path = Path(path)
    if path.parent.is_symlink():
        raise StateError("refusing symlink parent directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise StateError("refusing symlink destination")
    value = scrub(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def pid_identity(pid):
    """Never accept an arbitrary PID as proof it is our original worker."""
    if not pid:
        return None
    import subprocess
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        os.kill(pid, 0)
        p = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                           text=True, timeout=2)
        return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None

def process_group_state(group):
    if not isinstance(group,int) or isinstance(group,bool) or group<=0 or os.name!='posix':return 'unknown'
    try:os.killpg(group,0);return 'alive'
    except ProcessLookupError:return 'stopped'
    except OSError:return 'unknown'


def default_config():
    return Path(os.environ.get("SHARED_TASK_STATE_CONFIG", str(Path.home() / ".config/shared-task-state/config.json")))


def legacy_process_alive(data):
    if data.get('process_group') and process_group_state(data['process_group'])!='stopped':return True
    if pid_identity(data.get('pid')):return True
    if data.get('run_dir'):
        child=Path(data['run_dir'])/'child_pid'
        try:
            if child.exists():return bool(pid_identity(child.read_text().strip()))
        except OSError:return True
    return False


class Store:
    def __init__(self, config):
        if not isinstance(config.get("allow_file_requests", False), bool):
            raise StateError("allow_file_requests must be boolean")
        self.config = config
        if config.get("mount_anchor") and not os.path.ismount(config["mount_anchor"]):
            raise StateError("shared exchange volume is not mounted")
        self.root = Path(config["root"]).expanduser().resolve()
        self.exchange = Path(config["exchange"]).expanduser().resolve()
        self.pool_limits = config.get('pool_limits', {'local':int(config.get('max_active',2)),'cloud':None})
        if not isinstance(self.pool_limits,dict) or any(v is not None and (not isinstance(v,int) or isinstance(v,bool) or v<1) for v in self.pool_limits.values()):
            raise StateError('invalid pool limits')
        legacy_review_actors = config.get('legacy_review_actors', [])
        if (not isinstance(legacy_review_actors, list) or len(legacy_review_actors) > 100
                or any(not isinstance(actor, str) for actor in legacy_review_actors)
                or len(set(legacy_review_actors)) != len(legacy_review_actors)):
            raise StateError('legacy_review_actors must be a unique bounded list of actor IDs')
        self.legacy_review_actors = {safe_id(actor) for actor in legacy_review_actors}
        self.ttl = int(config.get("heartbeat_seconds", 180))
        if self.ttl < 5:
            raise StateError("invalid capacity or heartbeat timeout")
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite", timeout=15)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, source_key TEXT UNIQUE, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, at TEXT, actor TEXT, action TEXT, revision INTEGER, snapshot TEXT);
        """)
        self.legacy_leases = Path(config["legacy_leases"]).expanduser().resolve() if config.get("legacy_leases") else None

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @contextlib.contextmanager
    def capacity_lock(self):
        # Cooperates with the existing lifecycle runtime; Windows works standalone
        # through SQLite, but cannot share a POSIX legacy lease directory.
        if self.legacy_leases:
            if fcntl is None:
                raise StateError("legacy leases require POSIX file locking")
            self.legacy_leases.mkdir(parents=True, exist_ok=True)
            with open(self.legacy_leases / ".lock", "a+") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                yield
        else:
            yield

    def protect(self, value):
        return protected(value, self.config.get("privacy_scanner"))

    def all(self):
        return self.protect([evidence.task_view(json.loads(r[0])) for r in self.db.execute("SELECT data FROM tasks ORDER BY id")])

    def get(self, task_id):
        safe_id(task_id)
        row = self.db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise StateError("task not found")
        return self.protect(evidence.task_view(json.loads(row[0])))

    def fields(self, task, request, actor):
        evidence.merge_fields(task, checked_fields(request.get("fields", {})), actor, self.db)
        evidence.retract(task, request.get("retractions", []), actor)

    def save(self, task, actor, action):
        task.update(evidence.task_view(task))
        evidence.bounded({k: task[k] for k in EDITABLE | {"artifact_evidence", "retractions"} if k in task})
        for key in ('review', 'worker_result'):
            if isinstance(task.get(key), dict):
                if len(json.dumps(task[key], ensure_ascii=False).encode()) > 32768:
                    raise StateError(key + ' metadata exceeds 32 KiB')
                metadata(task[key])
        metadata(actor)
        metadata(action)
        self.protect({"actor": actor, "action": action})
        for key in ("task_id", "worker", "lease_id", "retired_lease_id", "origin", "resources", "created_by"):
            if key in task: metadata(task[key])
        if "delegation" in task:
            metadata(task["delegation"])
        safe = self.protect(task)
        task.clear()
        task.update(safe)
        task["updated_at"] = now()
        task["revision"] += 1
        self.db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task, ensure_ascii=False), task["task_id"]))
        self.db.execute("INSERT INTO events(task_id,at,actor,action,revision,snapshot) VALUES(?,?,?,?,?,?)",
                        (task["task_id"], now(), actor, action, task["revision"], json.dumps(task, ensure_ascii=False)))
        return task

    def revision(self, task, request):
        revision = request.get("expected_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision != task["revision"]:
            raise StateError("revision conflict; read current task before retry")

    def owner(self, task, request):
        if task.get("worker") != request.get("actor") or not task.get("lease_id") or task["lease_id"] != request.get("lease_id"):
            raise StateError("worker ownership or generation mismatch")

    def confirm_stopped(self, task, request):
        stop_evidence = request.get('stop_evidence')
        if request.get('stop_confirmed') is not True or not isinstance(stop_evidence, str) or not stop_evidence.strip():
            raise StateError('explicit stop_confirmed and stop_evidence required; silence is not proof')
        if task.get('pid_birth') and pid_identity(task.get('pid')) == task['pid_birth']:
            raise StateError('original worker still alive; reservation retained')
        if task.get('process_group') and process_group_state(task['process_group'])!='stopped':
            raise StateError('worker process group still alive or unknown; reservation retained')
        task['stop_evidence'] = scrub(stop_evidence)
        task['retired_lease_id'] = task.get('lease_id') or task.get('retired_lease_id')
        task.update(lease_id=None, pid=None, pid_birth=None,process_group=None)

    def review_owner(self, task, request):
        review = task.get('review')
        if (task.get('status') != 'worker_done' or not isinstance(review, dict)
                or review.get('status') != 'in_progress'
                or review.get('actor') != request.get('actor')
                or review.get('lease_id') != request.get('review_lease_id')):
            raise StateError('review ownership or generation mismatch')
        if not current_review_result(task, review):
            raise StateError('review is stale for the current worker result')
        return review

    def verify_current_outputs(self, task):
        """Re-hash only checked artifact bytes at lifecycle boundaries; no lock spans acceptance."""
        expected = output_fingerprint(task)
        worker_result = task.get('worker_result')
        if not isinstance(worker_result, dict) or worker_result.get('output_fingerprint') != expected:
            raise StateError('worker output fingerprint is stale')
        checks = [{'path': entry['path'], 'sha256': entry['sha256']}
                  for entry in task.get('artifact_evidence', [])
                  if entry.get('generation') == task.get('generation')
                  and entry.get('verification') == 'local_sha256_match']
        if checks:
            observed = evidence.inspect_files(checks, self.config.get('artifact_roots', []), task,
                                              'stillrelay-review-check')
            if output_fingerprint({**task, 'artifact_evidence': observed}) != expected:
                raise StateError('worker output fingerprint changed')
        return expected

    def external_count(self):
        if not self.legacy_leases:
            return 0
        count = 0
        own_ids = {"shared-" + t["task_id"] for t in self.all() if t["status"] == "running"}
        for path in self.legacy_leases.glob("*.json"):
            if path.stem in own_ids:
                continue
            try:
                data = json.loads(path.read_text())
                expires = dt.datetime.fromisoformat(str(data.get("expires_at", "")).replace("Z", "+00:00")).timestamp()
                if legacy_process_alive(data) or expires > time.time():
                    count += 1
            except (OSError, ValueError, TypeError):
                # Unknown lease files consume a slot rather than silently exceeding capacity.
                count += 1
        return count

    def check_resources(self, resources, ignore_task=None, ignore_legacy=None):
        if not resources:return
        for other in self.all():
            if other['status'] != 'running' or other['task_id'] == ignore_task:continue
            held = other.get('resources')
            if held is None:
                raise StateError('resource conflict: legacy running scope unknown')
            if any(resources_conflict(a,b) for a in resources for b in held):
                raise StateError('resource conflict with task ' + other['task_id'])
        if not self.legacy_leases:return
        own_ids = {'shared-'+t['task_id'] for t in self.all()}
        for p in self.legacy_leases.glob('*.json'):
            if p.stem in own_ids or p.stem == ignore_legacy:continue
            try:
                d=json.loads(p.read_text())
                expires=dt.datetime.fromisoformat(str(d.get('expires_at','')).replace('Z','+00:00')).timestamp()
                if not legacy_process_alive(d) and expires<=time.time():continue
                held=d.get('resources')
                if held is None:raise StateError('resource conflict: existing local worker scope unknown')
                if any(resources_conflict(a,b) for a in resources for b in resources_checked(held)):
                    raise StateError('resource conflict with local worker '+p.stem)
            except (ValueError,TypeError,OSError):
                raise StateError('resource conflict: unreadable local reservation')

    def declaration(self, request, task=None):
        result={}
        if 'execution_pool' in request:
            pool=request['execution_pool']
            if pool is not None and pool not in self.pool_limits:raise StateError('unknown execution pool')
            result['execution_pool']=pool
        if 'resources' in request:result['resources']=resources_checked(request['resources'])
        if 'read_only' in request:
            if not isinstance(request['read_only'],bool):raise StateError('read_only must be boolean')
            result['read_only']=request['read_only']
        merged={**(task or {}),**result}
        if merged.get('read_only') and any(r['mode']=='write' for r in merged.get('resources',[])):
            raise StateError('read_only task cannot declare writes')
        return result

    def lease_file(self, task):
        if self.legacy_leases:
            return self.legacy_leases / ("shared-" + safe_id(task["task_id"]) + ".json")
        return None

    def sync_lease(self, task):
        p = self.lease_file(task)
        if not p:
            return
        existing = None
        if p.exists():
            if p.is_symlink():
                raise StateError("refusing symlink lease")
            existing = json.loads(p.read_text())
            if existing.get("shared_lease_id") not in {task.get("lease_id"), task.get("retired_lease_id")}:
                raise StateError("legacy reservation belongs to a different owner")
        if task["status"] == "running" and task.get('execution_pool') == 'local':
            expires = dt.datetime.fromtimestamp(time.time() + self.ttl, dt.timezone.utc).isoformat()
            write_atomic(p, {"task_id": "shared-" + task["task_id"], "workstream_id": task["task_id"],
                             "generation": task["generation"], "pid": task.get("pid") or 0,
                             "heartbeat": now(), "expires_at": expires, "shared_lease_id": task["lease_id"],
                             'execution_pool':'local','resources':task.get('resources',[]),
                             **({'process_group':task['process_group']} if task.get('process_group') else {})})
        elif existing and existing.get("shared_lease_id") == task.get("retired_lease_id"):
            p.unlink()

    def reconcile(self):
        with self.capacity_lock():
            changed = self._reconcile_locked()
            for task in self.all():
                self.sync_lease(task)
            return changed

    def _reconcile_locked(self):
        changed = []
        with self.transaction():
            for task in self.all():
                if task["status"] != "running":
                    continue
                fresh = time.time() - task.get("heartbeat_epoch", 0) <= self.ttl
                original_pid_alive = task.get("pid_birth") and pid_identity(task.get("pid")) == task["pid_birth"]
                group=task.get('process_group')
                if not fresh and not original_pid_alive and (not group or process_group_state(group)=='stopped'):
                    task.update(status="interrupted", outcome_unknown=True,
                                interruption="heartbeat expired; inspect side effects before resuming")
                    task["retired_lease_id"] = task["lease_id"]
                    task["lease_id"] = None
                    self.save(task, "runtime", "interrupted")
                    changed.append(task["task_id"])
        return changed

    def apply(self, request):
        with self.capacity_lock():
            result = self._apply_locked(request)
            if result.get("task"):
                # Use committed current state even when returning an old
                # idempotency receipt, never resurrect an old lease.
                self.sync_lease(self.get(result["task"]["task_id"]))
            return result

    def _apply_locked(self, request):
        if not isinstance(request, dict) or request.get("schema_version") != VERSION:
            raise StateError("schema_version=1 object required")
        if len(json.dumps(request, ensure_ascii=False).encode()) > 1048576:
            raise StateError("request exceeds 1 MiB")
        request_id = safe_id(request.get("request_id"))
        actor = safe_id(request.get("actor"))
        execution = checked_execution(request.get("execution")) if request.get("op") in {"bind", "review_bind"} else None
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        for key in ("request_id", "actor", "task_id", "lease_id", "review_lease_id", "source_key", "origin", "resources", "execution_pool"):
            if key in request:
                metadata(request[key])
        if "artifact_checks" in request:
            metadata(request["artifact_checks"])
        fields_input = request.get("fields")
        if isinstance(fields_input, dict) and isinstance(fields_input.get("verified_facts"), list):
            for fact in fields_input["verified_facts"]:
                if isinstance(fact, dict):
                    metadata({k: fact[k] for k in ("id", "sources") if k in fact})
        request = self.protect(request)
        with self.transaction():
            prior = self.db.execute("SELECT digest,result FROM requests WHERE id=?", (request_id,)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise StateError("request_id reused with different content")
                return self.protect(json.loads(prior[1]))
            op = request.get("op")
            if "artifact_checks" in request and op != "finish":
                raise StateError("artifact_checks are accepted only on finish")
            if "retractions" in request and op not in {"update", "finish", "plan"}:
                raise StateError("retractions require update, finish or plan")
            if op == "create":
                fields = checked_fields(request.get("fields", {}))
                if not fields.get("title") or not fields.get("goal"):
                    raise StateError("title and goal required")
                task_id = safe_id(request.get("task_id") or str(uuid.uuid4()))
                source_key = request.get("source_key")
                if source_key is not None and (not isinstance(source_key, str) or len(source_key) > 256):
                    raise StateError("invalid source_key")
                existing = self.db.execute("SELECT data FROM tasks WHERE source_key=?", (source_key,)).fetchone() if source_key else None
                if existing:
                    task = json.loads(existing[0])
                    if task["goal"] != fields["goal"]:
                        raise StateError("source_key already belongs to a different task")
                else:
                    origin = request.get("origin")
                    if origin is not None and (not isinstance(origin, dict) or set(origin) - {"kind", "channel", "message_ts", "thread_ts", "target"} or any(not isinstance(v, str) or len(v) > 256 for v in origin.values())):
                        raise StateError("invalid origin metadata")
                    task = {"schema_version": VERSION, "task_id": task_id, "revision": 0,
                            "status": "queued", "generation": 0, "worker": None, "lease_id": None,
                            "pid": None, "pid_birth": None, "heartbeat_epoch": 0,
                            "created_by": actor, "created_at": now(), "updated_at": now(),
                            "phase": "prepare", "summary": "", "last_confirmed_step": "", "next_step": "",
                            'execution_pool':None,'resources':[],'read_only':False,
                            "review_policy": "legacy" if actor in self.legacy_review_actors else "required",
                            "origin": scrub(origin), **{k: [] for k in LIST_FIELDS},
                            "artifact_evidence": [], "retractions": []}
                    self.fields(task, request, actor)
                    task.update(self.declaration(request,task))
                    self.db.execute("INSERT INTO tasks(id,source_key,data) VALUES(?,?,?)",
                                    (task_id, source_key, json.dumps(task, ensure_ascii=False)))
                    self.save(task, actor, op)
            else:
                save_task = True
                task = self.get(request.get("task_id"))
                self.revision(task, request)
                if task.get("legacy_readonly"):
                    raise StateError("legacy snapshot is read-only; create a linked continuation task")
                if op == "claim":
                    if task["status"] != "queued":
                        raise StateError("only queued tasks may be claimed")
                    pool=task.get('execution_pool')
                    if pool not in self.pool_limits or not task.get('resources') and not task.get('read_only'):
                        raise StateError('execution pool and resource declaration required')
                    resources=resources_checked(task.get('resources',[]),refresh=True)
                    self.check_resources(resources,task['task_id'])
                    count=sum(t['status']=='running' and t.get('execution_pool')==pool for t in self.all())
                    if pool=='local':count+=self.external_count()
                    limit=self.pool_limits[pool]
                    if limit is not None and count>=limit:raise StateError('execution pool capacity reached: '+pool)
                    task['resources']=resources
                    pid = request.get("pid")
                    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
                        raise StateError("positive integer worker PID required")
                    birth = pid_identity(pid) if pid else None
                    if pid and not birth:
                        raise StateError("worker PID is not alive")
                    task.pop("delegation", None)
                    task.update(status="running", worker=actor, lease_id=str(uuid.uuid4()),
                                generation=task["generation"] + 1, heartbeat_epoch=time.time(),
                                pid=pid, pid_birth=birth, outcome_unknown=False)
                elif op == "bind":
                    self.owner(task, request)
                    if task["status"] != "running":
                        raise StateError("worker is not running")
                    binding = delegation_for(task, execution)
                    current = task.get("delegation")
                    if current is not None:
                        if current != binding:
                            raise StateError("task is already bound for this lease")
                        save_task = False
                    else:
                        task["delegation"] = binding
                elif op in {"update", "heartbeat", "finish", "release"}:
                    self.owner(task, request)
                    if task["status"] != "running":
                        raise StateError("worker is not running")
                    if op == "update":
                        self.fields(task, request, actor)
                    if op == "heartbeat" and request.get("pid"):
                        if not isinstance(request["pid"], int) or isinstance(request["pid"], bool):
                            raise StateError("positive integer worker PID required")
                        birth = pid_identity(request["pid"])
                        if not birth:
                            raise StateError("worker PID is not alive")
                        if task.get("pid") and (task["pid"] != request["pid"] or task["pid_birth"] != birth):
                            raise StateError("cannot replace a bound worker PID")
                        task.update(pid=request["pid"], pid_birth=birth)
                    if 'process_group' in request:
                        group=request['process_group']
                        if op!='heartbeat' or not isinstance(group,int) or isinstance(group,bool) or group<=0 or group!=task.get('pid') or os.name!='posix':
                            raise StateError('process group must match the bound POSIX worker PID')
                        try:actual=os.getpgid(group)
                        except OSError:raise StateError('worker process group unavailable')
                        if actual!=group or task.get('process_group') not in (None,group):raise StateError('worker process group mismatch')
                        task['process_group']=group
                    task["heartbeat_epoch"] = time.time()
                    if op in {"finish", "release"}:
                        if op == "release":
                            self.confirm_stopped(task, request)
                        if op == "finish":
                            if task.get('process_group') and process_group_state(task['process_group'])!='stopped':
                                raise StateError('worker process group still alive or unknown; reservation retained')
                            observed = evidence.inspect_files(request.get("artifact_checks", []),
                                                              self.config.get("artifact_roots", []), task, actor)
                            task.setdefault("artifact_evidence", []).extend(observed)
                            for item in observed:
                                if item["path"] not in task["artifacts"]:
                                    task["artifacts"].append(item["path"])
                            self.fields(task, request, actor)
                            exit_code = request.get("exit_code")
                            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                                raise StateError("integer exit_code required")
                            if request.get("report") is not None:
                                report = request["report"]
                                if not isinstance(report, str):
                                    raise StateError("report must be text")
                                if len(report) > 262144:
                                    report = report[:262144] + "\n[報告摘錄已截斷，完整輸出仍由原橋接保存]"
                                # Ownership was checked inside this transaction.
                                # Each lease owns an immutable report namespace.
                                report_path = self.root / "reports" / (task["task_id"] + "-" + task["lease_id"] + ".md")
                                write_atomic(report_path, scrub(report))
                                task["artifacts"] = list(dict.fromkeys(task["artifacts"] + [str(report_path)]))
                            task.update(status="worker_done" if exit_code == 0 else "blocked", exit_code=exit_code,
                                        outcome_unknown=exit_code != 0)
                            task['result_fingerprint'] = output_fingerprint(task)
                            task['worker_result'] = {'generation': task['generation'],
                                'completed_revision': task['revision'] + 1,
                                'output_fingerprint': task['result_fingerprint']}
                        else:
                            task.update(status="interrupted", outcome_unknown=True)
                        task.update(retired_lease_id=task["lease_id"], lease_id=None, pid=None, pid_birth=None,process_group=None)
                elif op == "plan":
                    if task["status"] == "running":
                        raise StateError("planning changes require worker to stop first")
                    if task.get('status') == 'worker_done' and task.get('review_policy', 'legacy') == 'required':
                        raise StateError('requeue and finish a current generation before changing a review-required result')
                    self.fields(task, request, actor)
                    task.update(self.declaration(request,task))
                elif op == "requeue":
                    if task["status"] not in {"interrupted", "blocked", "worker_done", "needs_revision", "accepted"}:
                        raise StateError("task cannot be requeued from this state")
                    resume_evidence = request.get("resume_evidence")
                    if not isinstance(resume_evidence, str) or not resume_evidence.strip():
                        raise StateError("resume_evidence required; never replay unknown side effects blindly")
                    self.confirm_stopped(task,request)
                    if task['status'] == 'accepted':
                        acceptance = task.get('acceptance')
                        if isinstance(acceptance, dict):
                            acceptance['superseded_at'] = now()
                            acceptance['superseded_by_generation'] = task['generation'] + 1
                        if isinstance(task.get('review'), dict):
                            task['review']['superseded_by_generation'] = task['generation'] + 1
                        task['review_policy'] = 'required'
                    task.update(status="queued", resume_evidence=scrub(resume_evidence), worker=None)
                elif op == 'review_start':
                    if task.get('status') != 'worker_done' or actor == task.get('worker'):
                        raise StateError('independent review requires a completed worker result and a different actor')
                    policy = task.get('review_policy', 'legacy')
                    if policy not in {'legacy', 'required'}:
                        raise StateError('invalid review policy')
                    worker_result = task.get('worker_result')
                    if not isinstance(worker_result, dict):
                        # Explicit review_start upgrades a legacy completion without rewriting its history.
                        worker_result = {'generation': task['generation'],
                                         'completed_revision': task['revision'],
                                         'output_fingerprint': output_fingerprint(task)}
                        task['result_fingerprint'] = worker_result['output_fingerprint']
                        task['worker_result'] = worker_result
                    if worker_result.get('generation') != task['generation']:
                        raise StateError('worker result is stale; resume and finish a current generation first')
                    self.verify_current_outputs(task)
                    previous = task.get('review')
                    if isinstance(previous, dict) and previous.get('status') == 'in_progress':
                        raise StateError('review already in progress; confirm stop and use review_resume')
                    same_result_review = (isinstance(previous, dict)
                        and current_review_result(task, previous)
                        and previous.get('status') in {'passed', 'inconclusive'}
                        and previous.get('result_task_revision') == task['revision'])
                    if worker_result.get('completed_revision') != task['revision'] and not same_result_review:
                        raise StateError('task changed after worker completion; resume and finish a current generation first')
                    attempt = previous.get('attempt', 0) + 1 if isinstance(previous, dict) else 1
                    previous_run_ids = list(previous.get('previous_run_ids', [])) if isinstance(previous, dict) else []
                    previous_execution = previous.get('execution') if isinstance(previous, dict) else None
                    if isinstance(previous_execution, dict):
                        previous_run = previous_execution.get('run_id')
                        if previous_run and previous_run not in previous_run_ids:
                            previous_run_ids.append(previous_run)
                    task['review_policy'] = 'required'
                    task['review'] = {'id': str(uuid.uuid4()), 'lease_id': str(uuid.uuid4()),
                        'attempt': attempt, 'status': 'in_progress', 'actor': actor,
                        'worker_generation': task['generation'],
                        'worker_revision': worker_result['completed_revision'],
                        'worker_execution': task.get('delegation', {}).get('run_id'),
                        'output_fingerprint': worker_result.get('output_fingerprint'),
                        'checkpoint': '', 'next_step': 'Bind the independent reviewer execution',
                        'evidence': [], 'previous_run_ids': previous_run_ids, 'started_at': now()}
                elif op == 'review_bind':
                    review = self.review_owner(task, request)
                    if review.get('execution') is not None:
                        if execution != review['execution']:
                            raise StateError('review is already bound for this lease')
                        save_task = False
                    else:
                        worker_run = task.get('delegation', {}).get('run_id')
                        if execution['run_id'] == worker_run or execution['run_id'] in review.get('previous_run_ids', []):
                            raise StateError('review execution must be a distinct run from the worker and prior reviewers')
                        review['execution'] = execution
                        review['bound_at'] = now()
                elif op == 'review_checkpoint':
                    review = self.review_owner(task, request)
                    if review.get('execution') is None:
                        raise StateError('bind the reviewer execution before checkpointing')
                    checkpoint, next_step, items = (request.get('checkpoint'), request.get('next_step'), request.get('evidence', []))
                    if (not isinstance(checkpoint, str) or not checkpoint.strip() or len(checkpoint) > 4096
                            or not isinstance(next_step, str) or not next_step.strip() or len(next_step) > 2048
                            or not isinstance(items, list) or len(items) > 100
                            or any(not isinstance(item, str) or len(item) > 2048 for item in items)):
                        raise StateError('review checkpoint, next_step and bounded evidence references required')
                    review.update(checkpoint=scrub(checkpoint), next_step=scrub(next_step),
                                  evidence=scrub(items), checkpoint_at=now())
                elif op == 'review_resume':
                    review = task.get('review')
                    if (task.get('status') != 'worker_done' or not isinstance(review, dict)
                            or review.get('status') != 'in_progress'
                            or review.get('lease_id') != request.get('review_lease_id')):
                        raise StateError('no matching interrupted review to resume')
                    if not current_review_result(task, review):
                        raise StateError('review is stale for the current worker result')
                    if actor == task.get('worker'):
                        raise StateError('reviewer must differ from the worker')
                    self.verify_current_outputs(task)
                    stop_evidence, resume_evidence = request.get('stop_evidence'), request.get('resume_evidence')
                    if (request.get('stop_confirmed') is not True or not isinstance(stop_evidence, str)
                            or not stop_evidence.strip() or not isinstance(resume_evidence, str) or not resume_evidence.strip()):
                        raise StateError('review resume requires stop confirmation, stop evidence and resume evidence')
                    previous_run = review.get('execution', {}).get('run_id')
                    previous_ids = list(review.get('previous_run_ids', []))
                    if previous_run and previous_run not in previous_ids:
                        previous_ids.append(previous_run)
                    review.update(actor=actor, lease_id=str(uuid.uuid4()), execution=None,
                        attempt=review.get('attempt', 1) + 1, status='in_progress', previous_run_ids=previous_ids,
                        stop_confirmed=True, stop_evidence=scrub(stop_evidence),
                        resume_evidence=scrub(resume_evidence), resumed_at=now(),
                        next_step='Bind the resumed reviewer execution')
                elif op == 'review_result':
                    review = self.review_owner(task, request)
                    if review.get('execution') is None:
                        raise StateError('bind the reviewer execution before recording a result')
                    self.verify_current_outputs(task)
                    decision = request.get('decision')
                    if not isinstance(decision, str) or decision not in {'pass', 'changes_requested', 'inconclusive'}:
                        raise StateError('review decision must be pass, changes_requested or inconclusive')
                    checks = request.get('checks', {})
                    required = {'source', 'diff', 'tests', 'runtime'}
                    if (not isinstance(checks, dict) or set(checks) != required
                            or any(not isinstance(value, str) or not value.strip() for value in checks.values())):
                        raise StateError('source/diff/tests/runtime review evidence required')
                    review.update(status='passed' if decision == 'pass' else decision,
                        result={'decision': decision, 'actor': actor, 'execution': review['execution'],
                                'at': now(), 'checks': scrub(checks)})
                    review['result_task_revision'] = task['revision'] + 1
                    if decision == 'changes_requested':
                        task['status'] = 'needs_revision'
                elif op == "accept":
                    if task["status"] != "worker_done":
                        raise StateError("only a completed worker result can be accepted")
                    if task.get('review_policy', 'legacy') == 'required':
                        review = task.get('review')
                        self.verify_current_outputs(task)
                        if (not isinstance(review, dict) or review.get('status') != 'passed'
                                or actor == review.get('actor') or not current_review_result(task, review)
                                or not isinstance(review.get('execution'), dict)
                                or review.get('result_task_revision') != task['revision']):
                            raise StateError('current independent review result required; reviewer cannot accept own review')
                        acceptance = {'actor': actor, 'at': now(), 'review': {
                            'id': review['id'], 'actor': review['actor'], 'execution': review['execution'],
                            'worker_generation': review['worker_generation'], 'worker_revision': review['worker_revision'],
                            'output_fingerprint': review['output_fingerprint'], 'checks': review['result']['checks']}}
                    else:
                        if actor == task.get('worker'):
                            raise StateError('legacy acceptance requires a different controller')
                        checks = request.get("checks", {})
                        required = {"source", "diff", "tests", "runtime"}
                        if not isinstance(checks, dict) or set(checks) != required or any(not isinstance(v, str) or not v.strip() for v in checks.values()):
                            raise StateError("source/diff/tests/runtime evidence required")
                        acceptance = {"actor": actor, "at": now(), "checks": scrub(checks)}
                    task.update(status="accepted", acceptance=acceptance)
                else:
                    raise StateError("unknown operation")
                if save_task:
                    self.save(task, actor, op)
            result = self.protect({"ok": True, "request_id": request_id, "task": task})
            self.db.execute("INSERT INTO requests VALUES(?,?,?)", (request_id, digest, json.dumps(result, ensure_ascii=False)))
            # Write lease while holding both locks. On a DB rollback a stranded
            # reservation conservatively consumes capacity until its TTL expires.
            if task["status"] == "running":
                self.sync_lease(task)
        return result

    def export(self):
        if self.config.get("mount_anchor") and not os.path.ismount(self.config["mount_anchor"]):
            raise StateError("shared exchange volume is not mounted")
        self.exchange.mkdir(parents=True, exist_ok=True)
        for directory in ["tasks", "inbox", "receipts"]:
            p = self.exchange / directory
            if p.is_symlink():
                raise StateError("refusing symlink exchange directory")
            p.mkdir(exist_ok=True)
        # One writer transaction serializes exports too: an older read cannot
        # overwrite a newer projection after another process has committed.
        with self.transaction():
            tasks = self.all()
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            visible = sorted(tasks, key=lambda x: x["updated_at"], reverse=True)
            active = [t for t in visible if t["status"] == "running"]
            rest = [t for t in visible if t["status"] != "running" and not t.get("legacy_readonly")]
            legacy = [t for t in visible if t.get("legacy_readonly")]
            table = active + rest[:20]
            projection = {"schema_version": 1, "projection_revision": seq, "generated_at": now(),
                          'pool_limits':self.pool_limits,'running_by_pool':{pool:sum(t.get('execution_pool')==pool for t in active) for pool in self.pool_limits},
                          "running": len(active), "external_active": self.external_count(),
                          "total_tasks": len(tasks), "legacy_snapshots": len(legacy),
                          "tasks": [{k: t.get(k) for k in ["task_id", "title", "revision", "status", "worker", "phase", "summary", "last_confirmed_step", "next_step", "blockers", "updated_at","execution_pool","resources","read_only", "review_policy", "review_status"]} for t in table]}
            for task in tasks:
                write_atomic(self.exchange / "tasks" / (task["task_id"] + ".json"), task)
            write_atomic(self.exchange / "overview.json", projection)
            lines = ["# 共用任務總覽", "", f"產生時間：{projection['generated_at']}；版本：{seq}",
                     f"執行中：{len(active)}；本地模型：{projection['running_by_pool'].get('local',0)}；其他本地租約：{projection['external_active']}；本地名額：{self.pool_limits.get('local')}；雲端無全局兩筆限制",
                     f"總任務：{len(tasks)}；唯讀舊紀錄：{len(legacy)}", "",
                     "狀態不等於已驗收。worker_done 表示工作者已結束，accepted 才表示控制者已驗收。",
                     "總覽最多顯示最近 20 筆非執行任務；完整紀錄在 tasks/。", ""]
            for t in table:
                lines += [f"## {t['title']}", f"- 任務：{t['task_id']}；版本：{t['revision']}；狀態：{t['status']}；工作者：{t.get('worker') or '未指派'}",
                          f"- 摘要：{t.get('summary') or '尚未提供'}",
                          f"- 執行池：{t.get('execution_pool') or '尚未申報'}；目標：{json.dumps(t.get('resources',[]),ensure_ascii=False)}",
                          f"- 最後確認完成：{t.get('last_confirmed_step') or '尚無確認步驟'}",
                          f"- 下一步：{t.get('next_step') or '尚未提供'}",
                          f"- 阻礙：{'；'.join(t.get('blockers', [])) or '未記錄'}", ""]
            write_atomic(self.exchange / "overview.md", "\n".join(lines))
        return projection

    def process_inbox(self):
        if not self.config.get("allow_file_requests", False):
            return []
        results = []
        pending_receipts = []
        consumed = []
        inbox = self.exchange / "inbox"
        if inbox.is_symlink() or (self.exchange / "receipts").is_symlink():
            raise StateError("refusing symlink file-channel directory")
        if not inbox.exists():
            return results
        for path in sorted(inbox.glob("*.json")):
            if path.is_symlink() or path.stat().st_size > 65536:
                continue
            request = None
            raw = None
            unchanged = False
            try:
                raw = path.read_bytes()
                request = json.loads(raw)
                rid = safe_id(request.get("request_id"))
                if path.name != rid + ".json":
                    raise StateError("filename must match request_id.json")
                receipt = self.exchange / "receipts" / path.name
                # Completed requests are immutable. Re-check the DB digest on
                # every replay so reusing an id with changed content is rejected.
                outcome = self.apply(request)
                if receipt.is_file() and not receipt.is_symlink():
                    try:
                        unchanged = json.loads(receipt.read_text()) == outcome
                    except (ValueError, OSError):
                        pass
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # Muse may still be writing; never consume partial JSON.
            except (StateError, sqlite3.Error, OSError, AttributeError, TypeError, RecursionError) as exc:
                outcome = self.protect({"ok": False, "error": scrub(str(exc)), "filename": path.name})
                receipt_name = path.name if scrub(path.name) == path.name else "rejected-" + hashlib.sha256(path.name.encode()).hexdigest() + ".json"
                receipt = self.exchange / "receipts" / receipt_name
                # A rejected file request stays rejected across restarts. The
                # controller must read the error and submit a new request id.
                if isinstance(request, dict) and ID.fullmatch(str(request.get("request_id", ""))) and scrub(request["request_id"]) == request["request_id"] and path.name == request["request_id"] + ".json":
                    digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    with self.transaction():
                        self.db.execute("INSERT OR IGNORE INTO requests VALUES(?,?,?)",
                                        (request["request_id"], digest, json.dumps(outcome, ensure_ascii=False)))
            if not unchanged:
                pending_receipts.append((receipt, outcome))
            consumed.append((path, raw))
            results.append(outcome)
        if pending_receipts:
            # Publish all projections before signalling completion to a
            # file-only client; it can immediately read the new task revision.
            self.export()
            for receipt, outcome in pending_receipts:
                write_atomic(receipt, outcome)
        for path, raw in consumed:
            # Never remove a replacement submitted during processing.
            if path.is_file() and not path.is_symlink() and path.read_bytes() == raw:
                path.unlink()
        return results

    def import_legacy(self, broker):
        import runpy
        module = runpy.run_path(str(Path(broker).resolve()))
        if not callable(module.get("all_tasks")):
            raise StateError("unsupported legacy broker")
        imported = 0
        with self.transaction():
            for item in module["all_tasks"]():
                ref = item["task_ref"]
                task_id = "legacy-" + hashlib.sha256(ref.encode()).hexdigest()[:24]
                snapshot = scrub({k: item.get(k) for k in ["task_ref", "kind", "state", "reported_state", "is_active",
                                                          "liveness_basis", "updated_at", "status_path", "checkpoint", "latest_run_id"]})
                existing = self.db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
                if existing and json.loads(existing[0]).get("legacy_snapshot") == snapshot:
                    continue
                task = {"schema_version": 1, "task_id": task_id, "revision": json.loads(existing[0])["revision"] if existing else 0,
                        "status": "historical", "legacy_readonly": True, "legacy_snapshot": snapshot,
                        "generation": 0, "worker": None, "lease_id": None, "title": item.get("title") or ref,
                        "goal": "既有 Tunnel 任務的唯讀索引；接續時讀原始檢查點並建立關聯任務。",
                        "phase": item.get("phase") or "legacy", "summary": "原始狀態：" + str(item.get("state")),
                        "last_confirmed_step": "", "next_step": item.get("next_step") or "",
                        "created_at": now(), "updated_at": now(), **{k: [] for k in LIST_FIELDS}}
                task = scrub(task)
                if not existing:
                    self.db.execute("INSERT INTO tasks VALUES(?,?,?)", (task_id, metadata("legacy:" + ref), json.dumps(self.protect(task), ensure_ascii=False)))
                self.save(task, "legacy-importer", "legacy-snapshot")
                imported += 1
        return {"imported_or_refreshed": imported}


def init(config_path, root, exchange, max_active=2, legacy_leases=None):
    config_path = Path(config_path)
    if config_path.exists():
        raise StateError("configuration already exists; refusing overwrite")
    exchange_path = Path(exchange).expanduser().resolve()
    anchor = str(Path(*exchange_path.parts[:3])) if exchange_path.parts[:2] == ('/', 'Volumes') else None
    config = {"schema_version": 2, "root": str(Path(root).expanduser().resolve()),
              "exchange": str(Path(exchange).expanduser().resolve()), "pool_limits": {"local": max_active, "cloud": None},
              "heartbeat_seconds": 180, "mount_anchor": anchor,
              "legacy_leases": str(Path(legacy_leases).expanduser().resolve()) if legacy_leases else None}
    store = Store(config)
    store.export()
    write_atomic(config_path, config)
    return config


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(default_config()))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--root", required=True); p.add_argument("--exchange", required=True)
    p.add_argument("--local-capacity", "--max-active", dest="max_active", type=int, default=2); p.add_argument("--legacy-leases")
    p = sub.add_parser("request"); g = p.add_mutually_exclusive_group(required=True); g.add_argument("--file"); g.add_argument("--stdin", action="store_true")
    p = sub.add_parser("read"); p.add_argument("task_id")
    p = sub.add_parser("import-legacy"); p.add_argument("--broker", required=True)
    p = sub.add_parser("history"); p.add_argument("task_id")
    p = sub.add_parser("supervise"); p.add_argument("--task-id", action="append", required=True); p.add_argument("--after-seq", type=int, default=0)
    sub.add_parser("overview"); sub.add_parser("tick")
    p = sub.add_parser("watch"); p.add_argument("--interval", type=float, default=2)
    args = ap.parse_args()
    store = None
    try:
        if args.cmd == "init":
            result = init(args.config, args.root, args.exchange, args.max_active, args.legacy_leases)
        elif args.cmd == "supervise":
            config = json.loads(Path(args.config).read_text())
            if config.get("mount_anchor") and not os.path.ismount(config["mount_anchor"]):
                raise StateError("shared exchange volume is not mounted")
            with contextlib.closing(sqlite3.connect((Path(config["root"])/"state.sqlite").as_uri()+"?mode=ro", uri=True)) as db:
                db.execute("BEGIN")
                result = protected(supervise(SimpleNamespace(db=db, ttl=int(config.get("heartbeat_seconds",180))),
                    args.task_id, args.after_seq), config.get("privacy_scanner"))
        else:
            store = Store(json.loads(Path(args.config).read_text()))
            if args.cmd == "read":
                result = store.get(args.task_id)
            elif args.cmd == "history":
                store.get(args.task_id)
                result = [{"seq": r[0], "at": r[1], "actor": r[2], "action": r[3], "task": evidence.task_view(json.loads(r[4]))}
                          for r in store.db.execute("SELECT seq,at,actor,action,snapshot FROM events WHERE task_id=? ORDER BY seq", (args.task_id,))]
            elif args.cmd == "import-legacy":
                result = store.import_legacy(args.broker)
                store.export()
            elif args.cmd == "request":
                result = store.apply(json.loads(sys.stdin.read(1048577) if args.stdin else Path(args.file).read_text()))
                store.export()
            elif args.cmd == "overview":
                store.reconcile()
                result = store.export()
            elif args.cmd == "tick":
                store.reconcile()
                results = store.process_inbox()
                result = {"processed": len(results), "overview": store.export()}
            else:
                if args.interval < 0.5:
                    raise StateError("watch interval must be at least 0.5 seconds")
                while True:
                    try:
                        store.reconcile()
                        store.process_inbox()
                        store.export()
                    except (StateError, OSError, sqlite3.Error) as exc:
                        print(json.dumps({"ok": False, "error": scrub(str(exc))}), file=sys.stderr, flush=True)
                    time.sleep(args.interval)
        print(json.dumps(store.protect(result) if store else protected(result), ensure_ascii=False, indent=2))
    except (StateError, OSError, sqlite3.Error, ValueError, TypeError, RecursionError) as exc:
        print(json.dumps({"ok": False, "error": scrub(str(exc))}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
