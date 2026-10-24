from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from shared_task_state import state


class Backend:
    def __init__(self, config, expected_state_id=None):
        self.config = Path(config).expanduser().resolve()
        joined = state.join(self.config, expected_state_id)
        self.state_id = joined['state_id']
        initial = state._read_config(self.config)
        self.locations = {key: str(Path(initial[key]).resolve()) for key in ('root', 'exchange')}
        self.scanner_required = bool(initial.get('privacy_scanner'))

    @contextmanager
    def store(self, readonly=True):
        config = state._read_config(self.config)
        if config['state_id'] != self.state_id or any(
                str(Path(config[key]).resolve()) != path for key, path in self.locations.items()):
            raise state.StateError('state identity or location changed; reconnect after reviewing configuration')
        if self.scanner_required and not config.get('privacy_scanner'):
            raise state.StateError('required privacy scanner was removed')
        store = state.Store(config, readonly=readonly)
        try:
            if readonly:
                store.db.execute('BEGIN')
            yield store
        finally:
            store.db.close()

    @staticmethod
    def page_limit(limit):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise state.StateError('limit must be an integer from 1 to 100')

    @staticmethod
    def protect(store, value):
        result = store.protect(value)
        if len(json.dumps(result, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
            raise state.StateError('result exceeds 2 MiB; use a smaller page')
        return result

    def status(self):
        with self.store() as store:
            return self.protect(store, {'ok': True, 'state_id': self.state_id,
                'config_schema': store.config['schema_version'],
                'total_tasks': store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],
                'latest_event': store.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0],
                'privacy_scanner_configured': bool(store.config.get('privacy_scanner')),
                'evidence_contract_version': 1,
                'review_contract_version': 1,
                'review_capabilities': ['required_by_default', 'review_start', 'review_bind',
                    'review_checkpoint', 'review_resume', 'review_result', 'accept_requires_pass',
                    'verified_artifact_fingerprints'],
                'review_execution': 'host_supplied_native_subagent',
                'fact_writes_require_sources': True,
                'artifact_checks_enabled': bool(store.config.get('artifact_roots')),
                'initialization_allowed': False,
                'liveness': 'Recorded state only; silence is not proof of termination.'})

    def overview(self, after_task_id='', limit=20):
        self.page_limit(limit)
        if after_task_id:
            state.safe_id(after_task_id)
        with self.store() as store:
            rows = store.db.execute('SELECT data FROM tasks WHERE id>? ORDER BY id LIMIT ?',
                                    (after_task_id, limit + 1)).fetchall()
            keys = ('task_id', 'title', 'revision', 'status', 'reservation_active', 'worker',
                    'phase', 'summary', 'last_confirmed_step', 'next_step', 'blockers', 'updated_at',
                    'execution_pool', 'resources', 'read_only')
            tasks = [{key: task.get(key) for key in keys}
                     for task in (json.loads(row[0]) for row in rows[:limit])]
            return self.protect(store, {'ok': True, 'state_id': self.state_id, 'tasks': tasks,
                'has_more': len(rows) > limit,
                'next_after_task_id': tasks[-1]['task_id'] if tasks else after_task_id})

    def read(self, task_id):
        with self.store() as store:
            return self.protect(store, {'ok': True, 'state_id': self.state_id, 'task': store.get(task_id)})

    def history(self, task_id, after_seq=0, limit=20):
        self.page_limit(limit)
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
            raise state.StateError('after_seq must be a nonnegative integer')
        with self.store() as store:
            store.get(task_id)
            rows = store.db.execute('SELECT seq,at,actor,action,snapshot FROM events '
                'WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?', (task_id, after_seq, limit + 1)).fetchall()
            events = [{'seq': row[0], 'at': row[1], 'actor': row[2], 'action': row[3],
                       'task': state.evidence.task_view(json.loads(row[4]))} for row in rows[:limit]]
            return self.protect(store, {'ok': True, 'state_id': self.state_id, 'events': events,
                'has_more': len(rows) > limit, 'next_after_seq': events[-1]['seq'] if events else after_seq})

    def supervise(self, task_ids, after_seq=0):
        with self.store() as store:
            return self.protect(store, state.supervise(store, task_ids, after_seq))

    def apply(self, request):
        if not isinstance(request, dict) or 'pid' in request:
            raise state.StateError('request object required; worker PIDs are not accepted through MCP')
        with self.store(readonly=False) as store:
            receipt = store.apply(request)
            try:
                store.export()
            except (state.StateError, OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
                return {'ok': False, 'committed': True, 'projection_updated': False, 'receipt': receipt,
                        'error': 'Request committed but projection failed. Retry the identical request with the same request_id.'}
            return {'ok': True, 'committed': True, 'projection_updated': True, 'receipt': receipt}
