"""Bounded local-file observations and source-backed reports, not truth certification."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat


class StateError(Exception):
    pass


CUMULATIVE = {'verified_facts', 'failed_attempts', 'artifacts', 'backups'}
FACT_INPUT = {'id', 'statement', 'method', 'sources'}
FACT_STAMP = {'status', 'reported_by', 'reported_at', 'generation', 'run_id'}
MAX_BYTES = 64 * 1024 * 1024
SHA256 = re.compile(r'^[0-9a-f]{64}$')


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def bounded(value):
    if len(json.dumps(value, ensure_ascii=False).encode()) > 32768:
        raise StateError('merged evidence or task fields exceed 32 KiB')
    for items in value.values():
        if isinstance(items, list) and len(items) > 100:
            raise StateError('merged list exceeds 100 entries')


def legacy_fact(statement):
    return {'id': 'legacy-' + hashlib.sha256(statement.encode()).hexdigest(),
            'statement': statement, 'method': '', 'sources': [], 'status': 'legacy_unverified'}


def task_view(task):
    """Annotate old records on read; never rewrite historical rows or certify them."""
    task = dict(task)
    task['verified_facts'] = [legacy_fact(x) if isinstance(x, str) else x
                              for x in task.get('verified_facts', [])]
    task.setdefault('artifact_evidence', [])
    task.setdefault('retractions', [])
    checked = {entry['path'] for entry in task['artifact_evidence']}
    task['unverified_artifacts'] = [ref for ref in task.get('artifacts', []) if ref not in checked]
    task['evidence_contract_version'] = 1
    if 'exit_code' in task:
        task['exit_code_trust'] = 'reported_by_worker; runner observes its own direct child separately'
    return task


def fact_shape(fact):
    if not isinstance(fact, dict) or set(fact) - FACT_INPUT - FACT_STAMP:
        raise StateError('fact requires id, statement, method and sources')
    if not FACT_INPUT <= set(fact):
        raise StateError('fact requires id, statement, method and sources')
    if any(not isinstance(fact[k], str) or not fact[k].strip() for k in ('id', 'statement', 'method')):
        raise StateError('fact id, statement and method must be nonempty text')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', fact['id']):
        raise StateError('invalid fact id')
    sources = fact['sources']
    if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
        raise StateError('fact requires 1–20 sources')
    for source in sources:
        if not isinstance(source, dict):
            raise StateError('invalid fact source')
        if source.get('kind') == 'event':
            seq = source.get('seq')
            if (set(source) != {'kind', 'seq'} or not isinstance(seq, int) or isinstance(seq, bool)
                    or not 1 <= seq <= 2**63 - 1):
                raise StateError('positive SQLite event sequence required')
        elif source.get('kind') == 'artifact':
            if set(source) != {'kind', 'path', 'sha256'}:
                raise StateError('artifact source requires path and sha256')
            check_shape({k: source[k] for k in ('path', 'sha256')})
        else:
            raise StateError('source kind must be event or artifact')


def check_shape(check):
    if not isinstance(check, dict) or set(check) != {'path', 'sha256'}:
        raise StateError('artifact check requires path and sha256')
    if not isinstance(check['path'], str) or '\0' in check['path'] or not Path(check['path']).is_absolute():
        raise StateError('absolute artifact path required')
    if not isinstance(check['sha256'], str) or not SHA256.fullmatch(check['sha256']):
        raise StateError('lowercase SHA-256 required')


def inspect_files(checks, roots, task, actor):
    if not isinstance(checks, list) or len(checks) > 100:
        raise StateError('artifact_checks must be a list of at most 100 checks')
    if checks and not roots:
        raise StateError('artifact checks require administrator-configured artifact_roots')
    if not checks:
        return []
    try:
        allowed = [Path(root).resolve(strict=True) for root in roots]
        if any(not root.is_dir() for root in allowed):
            raise StateError('artifact_roots must refer to directories')
    except (OSError, RuntimeError):
        raise StateError('configured artifact root unavailable') from None
    results, remaining, seen = [], MAX_BYTES, set()
    for check in checks:
        check_shape(check)
        raw = Path(check['path'])
        try:
            path = raw.resolve(strict=True)
            if not any(root in path.parents for root in allowed):
                raise StateError('artifact outside configured roots')
            if any(part.is_symlink() for part in (raw, *raw.parents)):
                raise StateError('symlink artifact paths are not accepted')
            key = os.path.normcase(str(path))
            if key in seen:
                raise StateError('duplicate artifact check path')
            seen.add(key)
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > remaining:
                    raise StateError('regular artifact files within 64 MiB total required')
                digest, size = hashlib.sha256(), 0
                while True:
                    chunk = stream.read(min(1024 * 1024, remaining - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > remaining:
                        raise StateError('artifact checks exceed 64 MiB total')
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                current = path.stat()
                identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                path_identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
                if (identity(before) != identity(after) or path_identity(after) != path_identity(current)
                        or size != after.st_size):
                    raise StateError('artifact changed during verification')
                if raw.resolve(strict=True) != path or any(part.is_symlink() for part in (raw, *raw.parents)):
                    raise StateError('artifact path changed during verification')
                if digest.hexdigest() != check['sha256']:
                    raise StateError('artifact SHA-256 mismatch')
                remaining -= size
        except (OSError, RuntimeError):
            raise StateError('artifact unavailable for local verification') from None
        results.append(dict(check, size_bytes=size, verified_at=timestamp(),
                            observed_by=actor, generation=task['generation'],
                            run_id=task.get('delegation', {}).get('run_id'),
                            verification='local_sha256_match'))
    return results


def merge_fields(task, fields, actor, db):
    """All create/update/finish/plan paths share accumulation and source checks."""
    for key, incoming in fields.items():
        if key not in CUMULATIVE:
            task[key] = incoming
            continue
        current = list(task.get(key, []))
        if key == 'verified_facts':
            current = [legacy_fact(x) if isinstance(x, str) else x for x in current]
            by_id = {x['id']: x for x in current}
            for fact in incoming:
                identifier = fact.get('id') if isinstance(fact, dict) else None
                existing = by_id.get(identifier) if isinstance(identifier, str) else None
                # A read snapshot may be resubmitted verbatim; never accept forged stamps.
                if existing is not None and fact == existing:
                    continue
                fact_shape(fact)
                core = {k: fact[k] for k in FACT_INPUT}
                if existing is not None:
                    if core == {k: existing[k] for k in FACT_INPUT} and not set(fact) & FACT_STAMP:
                        continue
                    raise StateError('fact id already exists; retract and submit a new id')
                if set(fact) & FACT_STAMP:
                    raise StateError('fact verification metadata is server-owned')
                for source in fact['sources']:
                    if source['kind'] == 'event':
                        if not db.execute('SELECT 1 FROM events WHERE seq=? AND task_id=?',
                                          (source['seq'], task['task_id'])).fetchone():
                            raise StateError('fact source event must exist in this task')
                    elif not any(e['path'] == source['path'] and e['sha256'] == source['sha256']
                                 for e in task.get('artifact_evidence', [])):
                        raise StateError('fact artifact source must have a matching local verification')
                record = dict(core, status='reported_with_source', reported_by=actor,
                              reported_at=timestamp(), generation=task['generation'],
                              run_id=task.get('delegation', {}).get('run_id'))
                current.append(record)
                by_id[record['id']] = record
        else:
            for item in incoming:
                if item not in current:
                    current.append(item)
        task[key] = current


def retract(task, entries, actor):
    if not isinstance(entries, list) or len(entries) > 100:
        raise StateError('retractions must be a bounded list')
    records = list(task.get('retractions', []))
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'field', 'value', 'reason'}:
            raise StateError('retraction requires field, value and reason')
        if not isinstance(entry['field'], str) or entry['field'] not in CUMULATIVE or any(not isinstance(entry[k], str) or not entry[k].strip()
                                                 for k in ('value', 'reason')):
            raise StateError('invalid retraction')
        items = task.get(entry['field'], [])
        identities = [x['id'] if isinstance(x, dict) else x for x in items]
        if entry['value'] not in identities:
            raise StateError('retraction target not found')
        if not any(r['field'] == entry['field'] and r['value'] == entry['value'] for r in records):
            records.append(dict(entry, retracted_by=actor, retracted_at=timestamp(), generation=task['generation']))
    task['retractions'] = records
