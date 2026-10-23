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
import unicodedata
sys.path.insert(0, str(Path(__file__).resolve().parent))
from privacy_guard import PrivacyError, scrub as privacy_scrub, check_metadata, guard
import evidence
from evidence import StateError


_CASE_CAPS = {}
VERSION = 1
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EDITABLE = {"title", "goal", "constraints", "phase", "summary", "last_confirmed_step",
            "verified_facts", "failed_attempts", "next_step", "artifacts", "backups", "blockers"}
LIST_FIELDS = {"constraints", "verified_facts", "failed_attempts", "artifacts", "backups", "blockers"}
PROGRESS_FIELDS = ("phase", "summary", "last_confirmed_step", "next_step", "blockers")
EXECUTION_FIELDS = {"executor", "run_id", "status_ref", "report_ref"}



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
            expected_type = dict if k == 'verified_facts' else str
            if not isinstance(v, list) or len(v) > 100 or any(not isinstance(x, expected_type) for x in v):
                raise StateError(('list of sourced fact objects required: ' if k == 'verified_facts'
                                  else 'list of strings required: ') + k)
        elif not isinstance(v, str):
            raise StateError("string required: " + k)
    if len(json.dumps(fields, ensure_ascii=False).encode()) > 32768:
        raise StateError("task update exceeds 32 KiB")
    return scrub(fields)


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
    """Fingerprint only digest-backed outputs observed for the current worker generation."""
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
        review = task.get('review') if isinstance(task.get('review'), dict) else {}
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
            "review_policy": task.get('review_policy', 'legacy'),
            "review_status": review.get('status', 'not_started'),
        })
    return {"ok": True, "state_id": store.state_id, "after_seq": after_seq,
            "snapshot_seq": snapshot_seq, "tasks": summaries}


def path_key(path):
    p=Path(path)
    ancestor=p
    while not ancestor.exists():ancestor=ancestor.parent
    if sys.platform == 'win32':return os.path.normcase(str(p))
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


def process_probe(pid):
    if not isinstance(pid,int) or isinstance(pid,bool) or pid<=0:return 'unknown',None
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.WaitForSingleObject.restype=wintypes.DWORD
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        handle=kernel.OpenProcess(0x1000|0x100000,False,pid)
        if not handle:return ('stopped',None) if ctypes.get_last_error()==87 else ('unknown',None)
        try:
            wait=kernel.WaitForSingleObject(handle,0)
            if wait==0:return 'stopped',None
            if wait!=258:return 'unknown',None
            times=[wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*[ctypes.byref(v) for v in times]):return 'unknown',None
            return 'alive',str((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime)
        finally:kernel.CloseHandle(handle)
    import subprocess,errno
    try:os.kill(pid,0)
    except ProcessLookupError:return 'stopped',None
    except OSError:return 'unknown',None
    try:
        proc=subprocess.run(['ps','-p',str(pid),'-o','lstart=','-o','stat='],capture_output=True,text=True,timeout=2)
        if proc.returncode or not proc.stdout.strip():return 'unknown',None
        line=proc.stdout.strip();birth,status=line.rsplit(None,1)
        return ('stopped',None) if status.startswith('Z') else ('alive',birth)
    except (OSError,ValueError,subprocess.SubprocessError):return 'unknown',None


def pid_identity(pid):
    state,birth=process_probe(pid)
    return birth if state=='alive' else None


def process_state(pid,birth):
    if not pid or not birth:return 'unknown'
    state,current=process_probe(pid)
    if state=='alive' and current!=birth:return 'stopped'
    return state


class Store:
    def __init__(self, config, initialize=False, readonly=False):
        validate_config(config)
        if not isinstance(config.get("allow_file_requests", False), bool):
            raise StateError("allow_file_requests must be boolean")
        self.config = config
        self.state_id = config['state_id']
        self.root = Path(config['root']).expanduser().resolve()
        self.exchange = Path(config['exchange']).expanduser().resolve()
        self.pool_limits = config['pool_limits']
        self.ttl = config['heartbeat_seconds']
        database = self.root / 'state.sqlite'
        if initialize:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif not database.is_file():
            raise StateError('registered database missing; refusing to create another state')
        self.db = sqlite3.connect(database.as_uri() + ('?mode=rwc' if initialize else '?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=15)
        try:
            if initialize:
                existing = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if existing and not any(row[0]=='identity' for row in existing):
                    raise StateError('existing database has no state identity; explicit migration required')
                self.db.execute('PRAGMA journal_mode=WAL')
                self.db.execute('PRAGMA synchronous=FULL')
                self.db.executescript("""
                    CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY CHECK(id=1), state_id TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, source_key TEXT UNIQUE, data TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, at TEXT, actor TEXT, action TEXT, revision INTEGER, snapshot TEXT);
                """)
                self.db.execute('INSERT OR IGNORE INTO identity VALUES(1,?)', (self.state_id,))
                self.db.commit()
            identity = self.db.execute('SELECT state_id FROM identity WHERE id=1').fetchone()
            if not identity or identity[0] != self.state_id:
                raise StateError('database state identity mismatch; refusing access')
            marker = self.exchange/'state.json'
            if marker.exists():
                if json.loads(marker.read_text(encoding='utf-8')).get('state_id') != self.state_id:
                    raise StateError('exchange state identity mismatch; refusing access')
            elif not initialize:
                raise StateError('exchange state marker missing; refusing another state')
        except BaseException:
            self.db.close()
            raise

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

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
        evidence.merge_fields(task, checked_fields(request.get('fields', {})), actor, self.db)
        evidence.retract(task, request.get('retractions', []), actor)

    def save(self, task, actor, action):
        task.update(evidence.task_view(task))
        evidence.bounded({k: task[k] for k in EDITABLE | {'artifact_evidence', 'retractions'} if k in task})
        if isinstance(task.get('review'), dict):
            evidence.bounded({'review': task['review']})
        if isinstance(task.get('worker_result'), dict):
            evidence.bounded({'worker_result': task['worker_result']})
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
        """Recheck checked artifact bytes at lifecycle boundaries; reports/references are excluded."""
        expected = output_fingerprint(task)
        result = task.get('worker_result')
        if not isinstance(result, dict) or result.get('output_fingerprint') != expected:
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

    def check_resources(self, resources, ignore_task=None):
        if not resources:return
        for other in self.all():
            if not other.get('reservation_active') or other['task_id']==ignore_task:continue
            if any(resources_conflict(a,b) for a in resources for b in other['resources']):
                raise StateError('resource conflict with task '+other['task_id'])

    def confirm_stopped(self, task, request):
        evidence = request.get('stop_evidence')
        if request.get('stop_confirmed') is not True or not isinstance(evidence,str) or not evidence.strip():
            raise StateError('explicit stop_confirmed and stop_evidence required; silence is not proof')
        if process_state(task.get('pid'),task.get('pid_birth'))=='alive':
            raise StateError('original worker still alive; reservation retained')
        task['stop_evidence']=scrub(evidence)
        task['reservation_active']=False
        task['retired_lease_id']=task.get('lease_id')
        task.update(lease_id=None,pid=None,pid_birth=None)

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

    def reconcile(self):
        changed=[]
        with self.transaction():
            for task in self.all():
                if task['status']!='running':continue
                fresh=time.time()-task.get('heartbeat_epoch',0)<=self.ttl
                if not fresh and process_state(task.get('pid'),task.get('pid_birth'))!='alive':
                    task.update(status='interrupted',outcome_unknown=True,reservation_active=True,
                                interruption='heartbeat expired; reservation retained until confirmed stopped')
                    self.save(task,'runtime','interrupted');changed.append(task['task_id'])
        return changed

    def apply(self, request):
        self.reconcile()
        return self._apply_locked(request)

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
        if 'artifact_checks' in request:
            metadata(request['artifact_checks'])
        fields = request.get('fields')
        if isinstance(fields, dict) and isinstance(fields.get('verified_facts'), list):
            for fact in fields['verified_facts']:
                if isinstance(fact, dict):
                    metadata({k: fact[k] for k in ('id', 'sources') if k in fact})
        request = self.protect(request)
        with self.transaction():
            prior = self.db.execute("SELECT digest,result FROM requests WHERE id=?", (request_id,)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise StateError("request_id reused with different content")
                return self.protect(json.loads(prior[1]))
            op = request.get("op")
            if 'artifact_checks' in request and op != 'finish':
                raise StateError('artifact_checks are accepted only on finish')
            if 'retractions' in request and op not in {'update', 'finish', 'plan'}:
                raise StateError('retractions require update, finish or plan')
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
                    if origin is not None and (not isinstance(origin, dict) or len(origin)>20 or any(not isinstance(k,str) or not ID.fullmatch(k) or not isinstance(v,str) or len(v)>256 for k,v in origin.items())):
                        raise StateError("invalid origin metadata")
                    task = {"schema_version": VERSION, "task_id": task_id, "revision": 0,
                            "status": "queued", "generation": 0, "worker": None, "lease_id": None,
                            "pid": None, "pid_birth": None, "heartbeat_epoch": 0,
                            "created_by": actor, "created_at": now(), "updated_at": now(),
                            "phase": "prepare", "summary": "", "last_confirmed_step": "", "next_step": "",
                            'execution_pool':None,'resources':[],'read_only':False,'reservation_active':False,
                            'review_policy':'required',
                            "origin": scrub(origin), **{k: [] for k in LIST_FIELDS}}
                    self.fields(task, request, actor)
                    task.update(self.declaration(request,task))
                    self.db.execute("INSERT INTO tasks(id,source_key,data) VALUES(?,?,?)",
                                    (task_id, source_key, json.dumps(task, ensure_ascii=False)))
                    self.save(task, actor, op)
            else:
                task = self.get(request.get("task_id"))
                self.revision(task, request)
                if op == "claim":
                    if task["status"] != "queued":
                        raise StateError("only queued tasks may be claimed")
                    pool=task.get('execution_pool')
                    if pool not in self.pool_limits or not task.get('resources') and not task.get('read_only'):
                        raise StateError('execution pool and resource declaration required')
                    resources=resources_checked(task.get('resources',[]),refresh=True)
                    self.check_resources(resources,task['task_id'])
                    count=sum(t.get('reservation_active') and t.get('execution_pool')==pool for t in self.all())
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
                    task.update(status="running", reservation_active=True, worker=actor, lease_id=str(uuid.uuid4()),
                                generation=task["generation"] + 1, heartbeat_epoch=time.time(),
                                pid=pid, pid_birth=birth, outcome_unknown=False)
                    save_task = True
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
                        save_task = True
                elif op in {"update", "heartbeat", "finish", "release"}:
                    save_task = True
                    self.owner(task, request)
                    if task["status"] != "running" and not (op == "release" and task["status"] == "interrupted"):
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
                    task["heartbeat_epoch"] = time.time()
                    if op in {"finish", "release"}:
                        if op == "release":self.confirm_stopped(task,request)
                        if op == "finish":
                            task["reservation_active"]=False
                            observed = evidence.inspect_files(request.get('artifact_checks', []),
                                                              self.config.get('artifact_roots', []), task, actor)
                            task['artifact_evidence'].extend(observed)
                            for item in observed:
                                if item['path'] not in task['artifacts']:
                                    task['artifacts'].append(item['path'])
                            self.fields(task, request, actor)
                            exit_code = request.get("exit_code")
                            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                                raise StateError("integer exit_code required")
                            if request.get("report") is not None:
                                report = request["report"]
                                if not isinstance(report, str):
                                    raise StateError("report must be text")
                                if len(report) > 262144:
                                    report = report[:262144] + "\n[報告摘錄已截斷，完整輸出仍由呼叫端保存]"
                                # Ownership was checked inside this transaction.
                                # Each lease owns an immutable report namespace.
                                report_path = self.root / "reports" / (task["task_id"] + "-" + task["lease_id"] + ".md")
                                task["artifacts"] = list(dict.fromkeys(task["artifacts"] + [str(report_path)]))
                                # Validate the final merged projection before filesystem side effects.
                                evidence.bounded({k: task[k] for k in EDITABLE | {'artifact_evidence', 'retractions'} if k in task})
                                report_content = scrub(report)
                                write_atomic(report_path, report_content)
                            task['result_fingerprint'] = output_fingerprint(task)
                            task['worker_result'] = {
                                'generation': task['generation'],
                                'completed_revision': task['revision'] + 1,
                                'output_fingerprint': task['result_fingerprint'],
                            }
                            task.update(status="worker_done" if exit_code == 0 else "blocked", exit_code=exit_code,
                                        outcome_unknown=exit_code != 0)
                        else:
                            task.update(status="interrupted", outcome_unknown=True)
                        task.update(retired_lease_id=task.get("lease_id") or task.get("retired_lease_id"), lease_id=None, pid=None, pid_birth=None)
                elif op == "plan":
                    save_task = True
                    if task.get("reservation_active"):
                        raise StateError("planning changes require confirmed stop and release first")
                    if task.get('status') == 'worker_done' and task.get('review_policy', 'legacy') == 'required':
                        raise StateError('requeue and finish a current generation before changing a review-required result')
                    self.fields(task, request, actor)
                    task.update(self.declaration(request,task))
                elif op == "requeue":
                    save_task = True
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
                    save_task = True
                    if task.get('status') != 'worker_done' or actor == task.get('worker'):
                        raise StateError('independent review requires a completed worker result and a different actor')
                    policy = task.get('review_policy', 'legacy')
                    if policy not in {'legacy', 'required'}:
                        raise StateError('invalid review policy')
                    worker_result = task.get('worker_result')
                    if not isinstance(worker_result, dict):
                        # Older task rows have no completion snapshot. Explicit review_start
                        # binds the currently observed legacy result without rewriting history.
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
                    previous = task.get('review')
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
                    task['review'] = {
                        'id': str(uuid.uuid4()), 'lease_id': str(uuid.uuid4()),
                        'attempt': attempt, 'status': 'in_progress', 'actor': actor,
                        'worker_generation': task['generation'],
                        'worker_revision': worker_result['completed_revision'],
                        'worker_execution': task.get('delegation', {}).get('run_id'),
                        'output_fingerprint': worker_result.get('output_fingerprint'),
                        'checkpoint': '', 'next_step': 'Bind the independent reviewer execution',
                        'evidence': [], 'previous_run_ids': previous_run_ids, 'started_at': now(),
                    }
                elif op == 'review_bind':
                    save_task = True
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
                    save_task = True
                    review = self.review_owner(task, request)
                    if review.get('execution') is None:
                        raise StateError('bind the reviewer execution before checkpointing')
                    checkpoint = request.get('checkpoint')
                    next_step = request.get('next_step')
                    items = request.get('evidence', [])
                    if (not isinstance(checkpoint, str) or not checkpoint.strip() or len(checkpoint) > 4096
                            or not isinstance(next_step, str) or not next_step.strip() or len(next_step) > 2048
                            or not isinstance(items, list) or len(items) > 100
                            or any(not isinstance(item, str) or len(item) > 2048 for item in items)):
                        raise StateError('review checkpoint, next_step and bounded evidence references required')
                    review.update(checkpoint=scrub(checkpoint), next_step=scrub(next_step),
                                  evidence=scrub(items), checkpoint_at=now())
                elif op == 'review_resume':
                    save_task = True
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
                    stop_evidence = request.get('stop_evidence')
                    resume_evidence = request.get('resume_evidence')
                    if (request.get('stop_confirmed') is not True
                            or not isinstance(stop_evidence, str) or not stop_evidence.strip()
                            or not isinstance(resume_evidence, str) or not resume_evidence.strip()):
                        raise StateError('review resume requires stop confirmation, stop evidence and resume evidence')
                    previous_run = review.get('execution', {}).get('run_id')
                    previous_ids = list(review.get('previous_run_ids', []))
                    if previous_run and previous_run not in previous_ids:
                        previous_ids.append(previous_run)
                    review.update(actor=actor, lease_id=str(uuid.uuid4()), execution=None,
                                  attempt=review.get('attempt', 1) + 1,
                                  status='in_progress', previous_run_ids=previous_ids,
                                  stop_confirmed=True, stop_evidence=scrub(stop_evidence),
                                  resume_evidence=scrub(resume_evidence), resumed_at=now(),
                                  next_step='Bind the resumed reviewer execution')
                elif op == 'review_result':
                    save_task = True
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
                                  result={'decision': decision, 'actor': actor,
                                          'execution': review['execution'], 'at': now(),
                                          'checks': scrub(checks)})
                    review['result_task_revision'] = task['revision'] + 1
                    if decision == 'changes_requested':
                        task['status'] = 'needs_revision'
                elif op == "accept":
                    save_task = True
                    if task["status"] != "worker_done":
                        raise StateError("only a completed worker result can be accepted")
                    if task.get('review_policy', 'legacy') == 'required':
                        review = task.get('review')
                        self.verify_current_outputs(task)
                        if (not isinstance(review, dict) or review.get('status') != 'passed'
                                or actor == review.get('actor') or not current_review_result(task, review)
                                or not isinstance(review.get('execution'), dict)
                                or review.get('result_task_revision') != task['revision']):
                            raise StateError('current independent review result required; the reviewer cannot accept its own review')
                        acceptance = {'actor': actor, 'at': now(), 'review': {
                            'id': review['id'], 'actor': review['actor'],
                            'execution': review['execution'],
                            'worker_generation': review['worker_generation'],
                            'worker_revision': review['worker_revision'],
                            'output_fingerprint': review['output_fingerprint'],
                            'checks': review['result']['checks'],
                        }}
                    else:
                        if actor == task.get("worker"):
                            raise StateError("legacy acceptance requires a different controller")
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
        return result

    def export(self):
        marker=self.exchange/'state.json'
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')).get('state_id')!=self.state_id:
            raise StateError('exchange missing or identity changed; refusing to recreate it')
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
            rest = [t for t in visible if t["status"] != "running"]
            table = active + rest[:20]
            projection = {"schema_version": 1, "state_id": self.state_id, "projection_revision": seq, "generated_at": now(),
                          'pool_limits':self.pool_limits,'running_by_pool':{pool:sum(t.get('execution_pool')==pool for t in active) for pool in self.pool_limits},
                          "running": len(active), "reserved":sum(bool(t.get("reservation_active")) for t in tasks),
                          "total_tasks": len(tasks),
                          "tasks": [{k: t.get(k) for k in ["task_id", "title", "revision", "status", "reservation_active", "worker", "phase", "summary", "last_confirmed_step", "next_step", "blockers", "updated_at","execution_pool","resources","read_only"]} for t in table]}
            for task in tasks:
                write_atomic(self.exchange / "tasks" / (task["task_id"] + ".json"), task)
            write_atomic(self.exchange / "overview.json", projection)
            lines = ["# 共用任務總覽", "", f"產生時間：{projection['generated_at']}；版本：{seq}",
                     f"執行中：{len(active)}；仍保留占用：{projection['reserved']}；執行名額：{json.dumps(self.pool_limits)}",
                     f"總任務：{len(tasks)}", "",
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
                        unchanged = json.loads(receipt.read_text(encoding='utf-8')) == outcome
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
            if path.is_file() and not path.is_symlink() and path.read_bytes() == raw:
                path.unlink()
        return results

CONFIG_VERSION=3


def default_layout(platform=None, home=None, env=None):
    platform=platform or sys.platform
    env=dict(os.environ if env is None else env)
    home=str(Path.home()) if home is None else str(home)
    if platform=='win32':
        from pathlib import PureWindowsPath
        candidate=PureWindowsPath(env.get('LOCALAPPDATA') or '')
        base=(candidate if candidate.is_absolute() else PureWindowsPath(home)/'AppData'/'Local')/'shared-task-state'
        return {'config':str(base/'config.json'),'root':str(base/'data'),'exchange':str(base/'exchange')}
    from pathlib import PurePosixPath
    h=PurePosixPath(home)
    if platform=='darwin':
        base=h/'Library'/'Application Support'/'shared-task-state'
        return {'config':str(base/'config.json'),'root':str(base/'data'),'exchange':str(base/'exchange')}
    cfg=PurePosixPath(env.get('XDG_CONFIG_HOME') or h/'.config');data=PurePosixPath(env.get('XDG_DATA_HOME') or h/'.local'/'share')
    config=(cfg if cfg.is_absolute() else h/'.config')/'shared-task-state'/'config.json'
    root=(data if data.is_absolute() else h/'.local'/'share')/'shared-task-state'
    return {'config':str(config),'root':str(root),'exchange':str(root/'exchange')}


def default_config():
    if os.environ.get('SHARED_TASK_STATE_CONFIG'):
        p=Path(os.environ['SHARED_TASK_STATE_CONFIG']).expanduser()
        if not p.is_absolute():raise StateError('SHARED_TASK_STATE_CONFIG must be an absolute shared location')
        return p
    default=Path(default_layout()['config'])
    # Recognize a previous installation instead of silently initializing a
    # second state. A legacy schema then produces an explicit migration error.
    previous=Path.home()/'.config'/'shared-task-state'/'config.json'
    return previous if sys.platform=='darwin' and not default.exists() and previous.exists() else default


def validate_config(config):
    if not isinstance(config,dict) or config.get('schema_version')!=CONFIG_VERSION:
        raise StateError('unsupported configuration; explicit migration required, never initialize another state')
    try:uuid.UUID(config['state_id'])
    except (ValueError,TypeError,KeyError,AttributeError):raise StateError('invalid state identity')
    for key in ['root','exchange']:
        if not isinstance(config.get(key),str) or not Path(config[key]).is_absolute():raise StateError('absolute '+key+' path required')
    roots = config.get('artifact_roots', [])
    if not isinstance(roots, list) or len(roots) > 100 or any(not isinstance(p, str) or '\0' in p or not Path(p).is_absolute() for p in roots):
        raise StateError('artifact_roots must be a bounded list of absolute directories')
    metadata(roots)
    limits=config.get('pool_limits')
    if not isinstance(limits,dict) or not limits or any(not isinstance(k,str) or not ID.fullmatch(k) or v is not None and (not isinstance(v,int) or isinstance(v,bool) or v<1) for k,v in limits.items()):
        raise StateError('invalid execution pool limits')
    ttl=config.get('heartbeat_seconds')
    if not isinstance(ttl,int) or isinstance(ttl,bool) or ttl<5:raise StateError('invalid heartbeat timeout')
    for anchor in config.get('mount_anchors',[]):
        if not os.path.ismount(anchor):raise StateError('configured storage volume is not mounted')


@contextlib.contextmanager
def initialization_lock(config_path, timeout=15):
    config_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock_path=config_path.with_name('.bootstrap.lock')
    if lock_path.is_symlink():raise StateError('refusing symlink bootstrap lock')
    with open(lock_path,'a+b') as file:
        if file.seek(0,os.SEEK_END)==0:file.write(b'\0');file.flush()
        file.seek(0)
        if sys.platform=='win32':
            import msvcrt
            deadline=time.monotonic()+timeout
            while True:
                try:msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1);break
                except OSError:
                    if time.monotonic()>=deadline:raise StateError('initialization busy; retry joining the same configuration')
                    time.sleep(.05)
            try:yield
            finally:file.seek(0);msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            deadline=time.monotonic()+timeout
            while True:
                try:fcntl.flock(file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise StateError('initialization busy; retry joining the same configuration')
                    time.sleep(.05)
            try:yield
            finally:fcntl.flock(file.fileno(),fcntl.LOCK_UN)


def _read_config(path):
    path=Path(path).expanduser().resolve()
    if not path.is_file():raise StateError('shared configuration not found; primary agent must bootstrap once')
    config=json.loads(path.read_text(encoding='utf-8'));validate_config(config)
    return config


def join(config_path, expected_state_id=None):
    path=Path(config_path).expanduser().resolve();config=_read_config(path)
    if expected_state_id is not None and config['state_id']!=expected_state_id:raise StateError('expected state identity mismatch')
    store=Store(config,readonly=True)
    try:
        return {'ok':True,'joined':True,'created':False,'schema_version':CONFIG_VERSION,'state_id':config['state_id'],
                'config':str(path),'root':config['root'],'exchange':config['exchange'],'pool_limits':config['pool_limits']}
    finally:store.db.close()


def publish_marker_once(path, value):
    # A second configuration targeting the same exchange cannot overwrite its
    # identity. Publish a complete file with an atomic, no-clobber hard link.
    path=Path(path)
    fd,tmp=tempfile.mkstemp(prefix='.identity-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as file:
            json.dump(value,file,ensure_ascii=False);file.write('\n');file.flush();os.fsync(file.fileno())
        try:os.link(tmp,path)
        except FileExistsError:
            if json.loads(path.read_text(encoding='utf-8')).get('state_id')!=value['state_id']:
                raise StateError('exchange already registered to another state')
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def bootstrap(config_path=None, root=None, exchange=None, local_capacity=None):
    config_path=Path(config_path or default_config()).expanduser().resolve()
    with initialization_lock(config_path):
        if config_path.exists():
            config=_read_config(config_path)
            for key,requested in [('root',root),('exchange',exchange)]:
                if requested and Path(requested).expanduser().resolve()!=Path(config[key]).resolve():
                    raise StateError('existing '+key+' differs; refusing another state')
            if local_capacity is not None and config['pool_limits'].get('local')!=local_capacity:
                raise StateError('existing capacity differs; bootstrap never changes an existing state')
            return join(config_path)
        pending=config_path.with_name('.bootstrap.pending.json')
        if pending.is_symlink():raise StateError('refusing symlink initialization manifest')
        if pending.exists():
            plan=json.loads(pending.read_text(encoding='utf-8'))
            if plan.get('config')!=str(config_path):raise StateError('initialization manifest belongs to another configuration')
            config=plan['settings'];validate_config(config)
            for key,requested in [('root',root),('exchange',exchange)]:
                if requested and Path(requested).expanduser().resolve()!=Path(config[key]).resolve():raise StateError('interrupted initialization target differs')
            if local_capacity is not None and config['pool_limits'].get('local')!=local_capacity:raise StateError('interrupted initialization capacity differs')
        else:
            layout=default_layout()
            explicit_config=config_path!=Path(layout['config']).expanduser().resolve()
            default_root=config_path.parent/'data' if explicit_config else Path(layout['root'])
            data=Path(root or default_root).expanduser().resolve()
            shared=Path(exchange or (config_path.parent/'exchange' if explicit_config else layout['exchange'])).expanduser().resolve()
            if (data/'state.sqlite').exists() or (shared.exists() and any(shared.iterdir())):
                raise StateError('unregistered existing state found; recover its configuration, refusing overwrite')
            anchors=[]
            if sys.platform=='darwin':
                for p in [data,shared]:
                    if p.parts[:2]==('/','Volumes') and len(p.parts)>2:
                        anchors.append(str(Path(*p.parts[:3])))
            config={'schema_version':CONFIG_VERSION,'state_id':str(uuid.uuid4()),'root':str(data),'exchange':str(shared),
                    'pool_limits':{'local':local_capacity,'cloud':None},'heartbeat_seconds':180,'allow_file_requests':False,'mount_anchors':list(dict.fromkeys(anchors))}
            validate_config(config);write_atomic(pending,{'config':str(config_path),'settings':config})
        shared=Path(config['exchange']);shared.mkdir(parents=True,exist_ok=True,mode=0o700)
        marker=shared/'state.json'
        if marker.exists():
            if json.loads(marker.read_text(encoding='utf-8')).get('state_id')!=config['state_id']:raise StateError('existing exchange belongs to another state')
        else:publish_marker_once(marker,{'schema_version':CONFIG_VERSION,'state_id':config['state_id']})
        store=Store(config,initialize=True)
        try:store.export()
        finally:store.db.close()
        write_atomic(config_path,config);pending.unlink()
        result=join(config_path);result.update(created=True,joined=False)
        return result


def doctor(config_path):
    result=join(config_path);config=_read_config(config_path)
    processor=Path(config['root'])/'processor.json';fresh=False
    if processor.is_file():
        try:
            record=json.loads(processor.read_text(encoding='utf-8'))
            fresh=record.get('state_id')==config['state_id'] and time.time()-record.get('at_epoch',0)<30
        except (OSError,ValueError,TypeError):pass
    result['capabilities']={'direct_requests':True,'file_requests_need_tick_or_watch':True,'recent_file_processor_activity':fresh}
    result['file_processor_note']='Recent activity is not proof a background process is still running; require a successful receipt.'
    return result


def processor_tick(store):
    store.reconcile();results=store.process_inbox();projection=store.export()
    write_atomic(store.root/'processor.json',{'state_id':store.state_id,'at_epoch':time.time(),'pid':os.getpid()})
    return {'processed':len(results),'overview':projection}


def main():
    for stream in [sys.stdout,sys.stderr]:
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    ap=argparse.ArgumentParser(description=__doc__)
    try:config_default=str(default_config())
    except StateError as exc:
        print(json.dumps({'ok':False,'error':str(exc)}),file=sys.stderr);return 1
    ap.add_argument('--config',default=config_default)
    sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('bootstrap');p.add_argument('--root');p.add_argument('--exchange');p.add_argument('--local-capacity',type=int)
    p=sub.add_parser('join');p.add_argument('--state-id')
    sub.add_parser('doctor')
    p=sub.add_parser('request');g=p.add_mutually_exclusive_group(required=True);g.add_argument('--file');g.add_argument('--stdin',action='store_true')
    p=sub.add_parser('read');p.add_argument('task_id')
    p=sub.add_parser('history');p.add_argument('task_id')
    p=sub.add_parser('supervise');p.add_argument('--task-id',action='append',required=True);p.add_argument('--after-seq',type=int,default=0)
    p=sub.add_parser('run');p.add_argument('--task-id',required=True);p.add_argument('--actor',required=True)
    p.add_argument('--executor',default='command');p.add_argument('--cwd');p.add_argument('--heartbeat-seconds',type=float);p.add_argument('--run-id')
    p.add_argument('command',nargs=argparse.REMAINDER)
    sub.add_parser('overview');sub.add_parser('tick')
    p=sub.add_parser('watch');p.add_argument('--interval',type=float,default=2)
    args=ap.parse_args();store=None
    try:
        if args.cmd=='bootstrap':result=bootstrap(args.config,args.root,args.exchange,args.local_capacity)
        elif args.cmd=='join':result=join(args.config,args.state_id)
        elif args.cmd=='doctor':result=doctor(args.config)
        elif args.cmd=='run':
            command=args.command
            if command and command[0]=='--':command=command[1:]
            if not command:raise StateError('command argv required after --')
            from shared_task_state_mcp.runner import run, exit_status
            result=run(args.config,args.task_id,args.actor,command,cwd=args.cwd,executor=args.executor,
                       heartbeat_seconds=args.heartbeat_seconds,run_id=args.run_id)
        else:
            config=_read_config(args.config);store=Store(config,readonly=args.cmd=='supervise')
            if args.cmd=='request':
                result=store.apply(json.loads(sys.stdin.read(1048577) if args.stdin else Path(args.file).read_text(encoding='utf-8')));store.export()
            elif args.cmd=='read':result=store.get(args.task_id)
            elif args.cmd=='history':
                store.get(args.task_id)
                result=[{'seq':r[0],'at':r[1],'actor':r[2],'action':r[3],'task':evidence.task_view(json.loads(r[4]))} for r in store.db.execute('SELECT seq,at,actor,action,snapshot FROM events WHERE task_id=? ORDER BY seq',(args.task_id,))]
            elif args.cmd=='supervise':
                store.db.execute('BEGIN')
                result=supervise(store,args.task_id,args.after_seq)
            elif args.cmd=='overview':store.reconcile();result=store.export()
            elif args.cmd=='tick':result=processor_tick(store)
            else:
                if args.interval<.5:raise StateError('watch interval must be at least 0.5 seconds')
                while True:
                    try:processor_tick(store)
                    except (StateError,OSError,sqlite3.Error) as exc:print(json.dumps({'ok':False,'error':scrub(str(exc))}),file=sys.stderr,flush=True)
                    time.sleep(args.interval)
        print(json.dumps(store.protect(result) if store else protected(result),ensure_ascii=False,indent=2));return exit_status(result) if args.cmd=='run' else 0
    except (StateError,OSError,sqlite3.Error,ValueError,TypeError,RecursionError,KeyError) as exc:
        print(json.dumps({'ok':False,'error':scrub(str(exc))},ensure_ascii=False),file=sys.stderr);return 1
    finally:
        if store is not None:store.db.close()


if __name__=='__main__':raise SystemExit(main())
