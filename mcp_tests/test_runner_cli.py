"""Installed entry point: durable evidence, state pinning, and honest exit status."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from shared_task_state import state
from shared_task_state_mcp.backend import Backend
from shared_task_state_mcp.runner import run


class InstalledRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='stillrelay installed runner ')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = self.base / 'config.json'
        state.bootstrap(self.config, self.base / 'db', self.base / 'exchange')
        self.backend = Backend(self.config)
        self.task = self.backend.apply({'schema_version': 1, 'request_id': uuid.uuid4().hex,
            'actor': 'controller', 'op': 'create', 'execution_pool': 'cloud', 'read_only': True,
            'resources': [], 'fields': {'title': 'Installed command', 'goal': 'Check the real entry point'}})['receipt']['task']

    def cli(self, *arguments):
        return subprocess.run([sys.executable, '-m', 'shared_task_state_mcp', '--config',
            str(self.config), *arguments], cwd=self.base, capture_output=True, text=True, timeout=20)

    def test_reopened_task_discovers_durable_owner_only_logs(self):
        result = self.cli('run', '--task-id', self.task['task_id'], '--actor', 'worker', '--',
                          sys.executable, '-c', "print('local fixture output')")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        reopened = Backend(self.config).read(self.task['task_id'])['task']
        directory = Path(reopened['delegation']['report_ref'])
        self.assertTrue(directory.is_relative_to((self.base / 'db' / 'runs').resolve()))
        self.assertEqual(directory, Path(receipt['logs']['stdout']).parent)
        self.assertEqual((directory / 'stdout.log').read_text().strip(), 'local fixture output')
        self.assertNotIn('local fixture output', json.dumps(reopened))
        if os.name != 'nt':
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / 'stdout.log').stat().st_mode & 0o777, 0o600)

    def test_wrong_state_pin_never_launches_command(self):
        marker = self.base / 'must-not-exist'
        result = self.cli('--state-id', str(uuid.uuid4()), 'run', '--task-id', self.task['task_id'],
                          '--actor', 'worker', '--', sys.executable, '-c',
                          f"from pathlib import Path; Path({str(marker)!r}).touch()")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertEqual(self.backend.read(self.task['task_id'])['task']['status'], 'queued')
        outcome = run(self.config, self.task['task_id'], 'worker', [sys.executable, '-c', 'pass'],
                      expected_state_id=str(uuid.uuid4()))
        self.assertFalse(outcome['ok'])
        self.assertEqual(self.backend.read(self.task['task_id'])['task']['status'], 'queued')

    def test_readonly_flag_refuses_launch_and_child_failure_has_nonzero_cli_status(self):
        result = self.cli('--read-only', 'run', '--task-id', self.task['task_id'],
                          '--actor', 'worker', '--', sys.executable, '-c', 'pass')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.backend.read(self.task['task_id'])['task']['status'], 'queued')
        result = self.cli('run', '--task-id', self.task['task_id'], '--actor', 'worker', '--',
                          sys.executable, '-c', 'raise SystemExit(7)')
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome['exit_code'], 7)
        self.assertEqual(outcome['status'], 'blocked')
        self.assertFalse(outcome['reservation_active'])
        self.assertEqual(result.returncode, 7)


if __name__ == '__main__':
    unittest.main()
