"""Exercise the installed package and real MCP transport against disposable state."""
import json
from contextlib import closing
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from shared_task_state import state
from shared_task_state_mcp.backend import Backend


def request(op='create', **kwargs):
    return {'schema_version': 1, 'request_id': uuid.uuid4().hex, 'actor': 'test-agent',
            'op': op, **kwargs}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shared state ')
        self.base = Path(self.temp.name)
        self.config = self.base / 'config.json'
        state.bootstrap(self.config, self.base / 'db', self.base / 'exchange')
        self.backend = Backend(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **kwargs):
        return self.backend.apply(request(fields={'title': '測試', 'goal': '續接'},
                                          execution_pool='cloud', read_only=True, **kwargs))

    def test_read_does_not_reconcile_and_pagination(self):
        first = self.create(task_id='a')['receipt']['task']
        self.create(task_id='b')
        self.backend.apply(request('claim', task_id='a', expected_revision=first['revision']))
        with closing(state.Store(state._read_config(self.config)).db) as db:
            task = json.loads(db.execute("SELECT data FROM tasks WHERE id='a'").fetchone()[0])
            task['heartbeat_epoch'] = 0
            db.execute("UPDATE tasks SET data=? WHERE id='a'", (json.dumps(task),))
            db.commit()
        before = self.backend.status()['latest_event']
        page = self.backend.overview('', 1)
        self.assertEqual([t['task_id'] for t in page['tasks']], ['a'])
        self.assertTrue(page['has_more'])
        self.assertEqual(self.backend.overview(page['next_after_task_id'], 1)['tasks'][0]['task_id'], 'b')
        self.assertEqual(self.backend.read('a')['task']['status'], 'running')
        self.assertEqual(self.backend.status()['latest_event'], before)
        history = self.backend.history('a', 0, 1)
        self.assertTrue(history['has_more'])
        self.assertGreater(self.backend.history('a', history['next_after_seq'], 1)['events'][0]['seq'], history['next_after_seq'])

    def test_replay_privacy_and_conflict(self):
        req = request(fields={'title': 'token=secret-test-value', 'goal': '續接'},
                      execution_pool='cloud', read_only=True)
        first = self.backend.apply(req)
        self.assertTrue(first['committed'])
        self.assertEqual(first['receipt'], self.backend.apply(req)['receipt'])
        self.assertNotIn('secret-test-value', json.dumps(self.backend.read(first['receipt']['task']['task_id'])))
        with self.assertRaises(state.StateError):
            self.backend.apply(dict(req, fields={'title': 'changed', 'goal': '續接'}))
        task = first['receipt']['task']
        with self.assertRaises(state.StateError):
            self.backend.apply(request('plan', task_id=task['task_id'], expected_revision=0))

    def test_identity_scanner_removal_and_missing_database_fail_closed(self):
        config = json.loads(self.config.read_text())
        config['privacy_scanner'] = [sys.executable, '-c', 'import sys;sys.exit(0)']
        self.config.write_text(json.dumps(config))
        guarded = Backend(self.config)
        config.pop('privacy_scanner')
        self.config.write_text(json.dumps(config))
        with self.assertRaises(state.StateError):
            guarded.status()
        config['state_id'] = str(uuid.uuid4())
        self.config.write_text(json.dumps(config))
        with self.assertRaises(state.StateError):
            self.backend.status()
        database = self.base / 'db' / 'state.sqlite'
        database.unlink()
        with self.assertRaises(state.StateError):
            Backend(self.config)
        self.assertFalse(database.exists())

    def test_commit_survives_projection_failure_and_identical_retry(self):
        target = self.base / 'exchange' / 'tasks'
        target.rmdir()
        target.write_text('blocked projection')
        req = request(fields={'title': 'durable commit', 'goal': 'retry export'},
                      execution_pool='cloud', read_only=True)
        outcome = self.backend.apply(req)
        self.assertTrue(outcome['committed'])
        self.assertFalse(outcome['ok'])
        self.assertFalse(outcome['projection_updated'])
        task_id = outcome['receipt']['task']['task_id']
        self.assertEqual(self.backend.read(task_id)['task'], outcome['receipt']['task'])
        target.unlink()
        replay = self.backend.apply(req)
        self.assertTrue(replay['ok'])
        self.assertEqual(replay['receipt'], outcome['receipt'])
        self.assertEqual(self.backend.status()['total_tasks'], 1)

    def test_scanner_failure_blocks_return_and_mutation(self):
        config = json.loads(self.config.read_text())
        config['privacy_scanner'] = [sys.executable, '-c', 'import sys;sys.exit(1)']
        self.config.write_text(json.dumps(config))
        backend = Backend(self.config)
        with self.assertRaises(state.StateError):
            backend.status()
        with self.assertRaises(state.StateError):
            backend.apply(request(fields={'title': 'blocked', 'goal': 'scanner'}))
        config.pop('privacy_scanner')
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.backend.status()['total_tasks'], 0)


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_entry_lifecycle_and_two_clients(self):
        with tempfile.TemporaryDirectory(prefix='mcp user ') as tmp:
            base = Path(tmp)
            config = base / 'config.json'
            command = [sys.executable, '-m', 'shared_task_state_mcp', '--config', str(config)]
            # Serving never initializes state as a side effect.
            missing = subprocess.run(command, cwd=tmp, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(missing.returncode, 0)
            self.assertEqual(missing.stdout, '')
            self.assertFalse(config.exists())
            init = subprocess.run(command + ['init'], cwd=tmp, capture_output=True, text=True, timeout=20)
            self.assertEqual(init.returncode, 0, init.stderr)
            state_id = json.loads(init.stdout)['state_id']
            generated = subprocess.run(command + ['client-config'], cwd=tmp, capture_output=True, text=True, timeout=20)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            entry = json.loads(generated.stdout)['mcpServers']['shared-task-state']
            self.assertIn(state_id, entry['args'])
            params = StdioServerParameters(**entry)
            req = request(fields={'title': 'MCP wire', 'goal': 'CLI interoperability'},
                          execution_pool='cloud', read_only=True)
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    tools = (await client.list_tools()).tools
                    self.assertEqual({t.name for t in tools}, {'shared_task_status', 'shared_task_overview',
                                     'shared_task_read', 'shared_task_history', 'shared_task_supervise',
                                     'shared_task_apply'})
                    response = await client.call_tool('shared_task_apply', {'request': req})
                    self.assertFalse(response.is_error, response)
                    task = response.structured_content['receipt']['task']
                    cli = subprocess.run([sys.executable, '-m', 'shared_task_state.state', '--config', str(config),
                                          'read', task['task_id']], cwd=tmp, capture_output=True, text=True, timeout=20)
                    self.assertEqual(cli.returncode, 0, cli.stderr)
                    self.assertEqual(json.loads(cli.stdout), task)
                    async with stdio_client(params) as other_streams:
                        async with ClientSession(*other_streams) as other:
                            await other.initialize()
                            replay = await other.call_tool('shared_task_apply', {'request': req})
                            self.assertEqual(replay.structured_content['receipt']['task'], task)
                    invalid = await client.call_tool('shared_task_read', {'task_id': '../config.json'})
                    self.assertTrue(invalid.is_error)
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    read = await client.call_tool('shared_task_read', {'task_id': task['task_id']})
                    self.assertEqual(read.structured_content['task'], task)
            readonly = StdioServerParameters(command=entry['command'], args=entry['args'] + ['--read-only'])
            async with stdio_client(readonly) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    self.assertNotIn('shared_task_apply', {t.name for t in (await client.list_tools()).tools})
                    denied = await client.call_tool('shared_task_apply', {'request': req})
                    self.assertTrue(denied.is_error)


if __name__ == '__main__':
    unittest.main()
