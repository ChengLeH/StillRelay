"""The installed MCP transport must enforce the same evidence rules as the CLI."""
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from shared_task_state import state


class EvidenceWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_evidence_roundtrip_rejection_restart_and_cli_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            config = base / 'config.json'
            state.bootstrap(config, base / 'db', base / 'exchange')
            output = base / 'output'
            output.mkdir()
            options = json.loads(config.read_text())
            options['artifact_roots'] = [str(output)]
            config.write_text(json.dumps(options))
            params = StdioServerParameters(command=sys.executable, args=[
                '-m', 'shared_task_state_mcp', '--config', str(config)])
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    status = await client.call_tool('shared_task_status', {})
                    self.assertEqual(status.structured_content['evidence_contract_version'], 1)
                    self.assertTrue(status.structured_content['artifact_checks_enabled'])

                    async def apply(op, task=None, **values):
                        req = dict(schema_version=1, request_id=uuid.uuid4().hex, actor='worker', op=op, **values)
                        if task:
                            req.update(task_id=task['task_id'], expected_revision=task['revision'],
                                       lease_id=task.get('lease_id'))
                        result = await client.call_tool('shared_task_apply', {'request': req})
                        return result

                    response = await apply('create', execution_pool='cloud', read_only=True,
                                           fields={'title': 'Wire evidence', 'goal': 'Check actual protocol'})
                    task = response.structured_content['receipt']['task']
                    task = (await apply('claim', task)).structured_content['receipt']['task']
                    bad = await apply('update', task, fields={'verified_facts': ['no source']})
                    self.assertTrue(bad.is_error)
                    file = output / 'result.txt'
                    file.write_bytes(b'wire evidence')
                    check = {'path': str(file), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
                    rejected = await apply('finish', task, exit_code=0,
                                           artifact_checks=[dict(check, sha256='0' * 64)])
                    self.assertTrue(rejected.is_error)
                    current = await client.call_tool('shared_task_read', {'task_id': task['task_id']})
                    self.assertEqual(current.structured_content['task'], task)
                    fact = {'id': 'wire-check', 'statement': 'Output bytes match the fixture',
                            'method': 'SHA-256 comparison', 'sources': [dict(kind='artifact', **check)]}
                    done = await apply('finish', task, exit_code=0, artifact_checks=[check],
                                       fields={'verified_facts': [fact], 'failed_attempts': ['first hash was wrong']})
                    self.assertFalse(done.is_error, done)
                    expected = done.structured_content['receipt']['task']
                    self.assertEqual(expected['unverified_artifacts'], [])
                    self.assertEqual(expected['verified_facts'][0]['status'], 'reported_with_source')
            # Restart the actual server and verify durable views through both clients.
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    current = await client.call_tool('shared_task_read', {'task_id': expected['task_id']})
                    self.assertEqual(current.structured_content['task'], expected)
                    history = await client.call_tool('shared_task_history', {'task_id': expected['task_id']})
                    self.assertEqual(history.structured_content['events'][-1]['task'], expected)
            for operation in ('read', 'history'):
                cli = subprocess.run([sys.executable, '-m', 'shared_task_state.state',
                                      '--config', str(config), operation, expected['task_id']],
                                     cwd=tmp, capture_output=True, text=True, timeout=20)
                self.assertEqual(cli.returncode, 0, cli.stderr)
                view = json.loads(cli.stdout)
                self.assertEqual(view if operation == 'read' else view[-1]['task'], expected)


if __name__ == '__main__':
    unittest.main()
