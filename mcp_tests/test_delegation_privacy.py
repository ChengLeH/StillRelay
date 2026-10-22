"""No external scanner is required for the credential boundary on new tools."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from shared_task_state import state
from shared_task_state_mcp.backend import Backend


class DelegationPrivacyTests(unittest.TestCase):
    def test_no_scanner_masks_progress_and_reports_and_rejects_sensitive_binding(self):
        secret = 'ghp_' + 'Z9' * 18
        pem = '-----BEGIN PRIVATE KEY-----\n' + 'K' * 64 + '\n-----END PRIVATE KEY-----'
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / 'config.json'
            state.bootstrap(config, base / 'db', base / 'exchange')
            self.assertNotIn('privacy_scanner', json.loads(config.read_text()))
            backend = Backend(config)

            def apply(op, task=None, **values):
                request = {'schema_version': 1, 'request_id': uuid.uuid4().hex,
                           'actor': 'worker' if task is not None else 'controller', 'op': op, **values}
                if task is not None:
                    request.update(task_id=task['task_id'], expected_revision=task['revision'])
                    if task.get('lease_id'):
                        request['lease_id'] = task['lease_id']
                return backend.apply(request)['receipt']['task']

            task = apply('create', execution_pool='cloud', read_only=True, resources=[],
                         fields={'title': 'Privacy fixture', 'goal': 'Check ' + secret})
            task = apply('claim', task)
            revision = task['revision']
            with self.assertRaises(state.StateError):
                apply('bind', task, execution={'executor': 'command', 'run_id': 'fixture-run',
                                              'report_ref': '/local/' + secret})
            self.assertEqual(backend.read(task['task_id'])['task']['revision'], revision)
            task = apply('bind', task, execution={'executor': 'command', 'run_id': 'fixture-run'})
            task = apply('update', task, fields={'summary': secret, 'verified_facts': [pem],
                                                'next_step': 'password=fixture-password'})
            view = backend.supervise([task['task_id']])
            task = apply('finish', task, exit_code=0, report=secret + '\n' + pem)
            records = [backend.read(task['task_id']), backend.history(task['task_id']), view]
            serialized = json.dumps(records)
            self.assertIn('[REDACTED]', serialized)
            for raw in (secret, 'K' * 64, 'fixture-password'):
                self.assertNotIn(raw, serialized)
            db = sqlite3.connect(base / 'db' / 'state.sqlite')
            try:
                persisted = repr(db.execute('SELECT data FROM tasks').fetchall())
                persisted += repr(db.execute('SELECT snapshot FROM events').fetchall())
                persisted += repr(db.execute('SELECT result FROM requests').fetchall())
                for raw in (secret, 'K' * 64, 'fixture-password'):
                    self.assertNotIn(raw, persisted)
            finally:
                db.close()
            for path in (base / 'exchange').rglob('*'):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes())
            for path in (base / 'db' / 'reports').glob('*'):
                self.assertNotIn(secret.encode(), path.read_bytes())


if __name__ == '__main__':
    unittest.main()
