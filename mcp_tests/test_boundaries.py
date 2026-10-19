import json
from pathlib import Path
import tempfile
import unittest
import uuid

from shared_task_state import state
from shared_task_state_mcp.backend import Backend


class IdentityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        state.bootstrap(self.config)
        self.settings = json.loads(self.config.read_text())
        self.backend = Backend(self.config, self.settings['state_id'])

    def test_missing_database_is_not_created(self):
        database = Path(self.settings['root']) / 'state.sqlite'
        database.unlink()
        with self.assertRaisesRegex(state.StateError, 'database missing'):
            self.backend.status()
        self.assertFalse(database.exists())

    def test_expected_identity_rejects_other_valid_state(self):
        other = self.root / 'other' / 'config.json'
        state.bootstrap(other)
        with self.assertRaisesRegex(state.StateError, 'identity mismatch'):
            Backend(other, self.settings['state_id'])
        self.assertEqual(self.backend.status()['state_id'], self.settings['state_id'])

    def test_running_server_rejects_location_redirection(self):
        self.settings['root'] = str(self.root / 'different-database')
        self.config.write_text(json.dumps(self.settings))
        with self.assertRaisesRegex(state.StateError, 'location changed'):
            self.backend.status()
        self.assertFalse((self.root / 'different-database').exists())

    def test_exchange_identity_is_checked_on_each_call(self):
        marker = Path(self.settings['exchange']) / 'state.json'
        marker.write_text(json.dumps({'state_id': str(uuid.uuid4())}))
        with self.assertRaisesRegex(state.StateError, 'exchange state identity mismatch'):
            self.backend.status()

    def test_mcp_never_binds_a_callers_pid(self):
        with self.assertRaisesRegex(state.StateError, 'PIDs are not accepted'):
            self.backend.apply({'pid': 123})
        self.assertEqual(self.backend.status()['total_tasks'], 0)
