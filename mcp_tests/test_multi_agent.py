"""Installed-transport role reversal and configurable participant cohorts."""
import asyncio
from contextlib import AsyncExitStack
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from shared_task_state import state


class MultiAgentWireTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, participants):
        with tempfile.TemporaryDirectory(prefix='stillrelay cohort ') as temporary:
            base = Path(temporary)
            config = base / 'config.json'
            state.bootstrap(config, base / 'db', base / 'exchange')
            # A→B, B→A, A→C directly exercises the requested reciprocal roles.
            edges = [(0, 1), (1, 0), (0, 2)] if participants == 3 else [
                (index, (index + 1) % participants) for index in range(participants)]
            parameters = StdioServerParameters(command=sys.executable,
                args=['-m', 'shared_task_state_mcp', '--config', str(config)], cwd=temporary)
            async with AsyncExitStack() as stack:
                clients = []
                for _ in range(participants):
                    streams = await stack.enter_async_context(stdio_client(parameters))
                    client = await stack.enter_async_context(ClientSession(*streams))
                    await client.initialize()
                    clients.append(client)

                async def call(client, tool, arguments):
                    result = await clients[client].call_tool(tool, arguments)
                    self.assertFalse(result.is_error, result)
                    return result.structured_content

                async def apply(client, operation, task=None, **values):
                    request = {'schema_version': 1, 'request_id': uuid.uuid4().hex,
                               'actor': f'agent-{client}', 'op': operation, **values}
                    if task is not None:
                        request.update(task_id=task['task_id'], expected_revision=task['revision'])
                        if task.get('lease_id'):
                            request['lease_id'] = task['lease_id']
                    result = await call(client, 'shared_task_apply', {'request': request})
                    self.assertTrue(result['committed'])
                    return result['receipt']['task']

                tasks = []
                for index, (controller, _) in enumerate(edges):
                    tasks.append(await apply(controller, 'create', task_id=f'task-{index}',
                        execution_pool='cloud', resources=[], read_only=True,
                        fields={'title': f'Independent check {index}',
                                'goal': 'Preserve independent checked progress across participants'}))

                async def start(index):
                    _, worker = edges[index]
                    task = await apply(worker, 'claim', tasks[index])
                    return await apply(worker, 'bind', task,
                        execution={'executor': 'fixture-command', 'run_id': f'run-{index}'})

                tasks = list(await asyncio.gather(*(start(i) for i in range(len(tasks)))))
                initial = await call(0, 'shared_task_supervise', {
                    'task_ids': [task['task_id'] for task in tasks], 'after_seq': 0})
                self.assertEqual(len(initial['tasks']), len(edges))
                self.assertEqual({item['delegation']['worker'] for item in initial['tasks']},
                                 {f'agent-{worker}' for _, worker in edges})

                outputs = {}

                async def progress(index):
                    _, worker = edges[index]
                    completed = await asyncio.to_thread(subprocess.run,
                        [sys.executable, '-c', f'print({index} * {index})'],
                        cwd=temporary, capture_output=True, text=True, timeout=20)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(completed.stdout.strip(), str(index * index))
                    outputs[index] = completed.stdout.strip()
                    task = await apply(worker, 'update', tasks[index], fields={
                        'phase': 'verification', 'last_confirmed_step': f'Checked fixture {index}',
                        'summary': f'Reported command result: {outputs[index]}',
                        'next_step': 'Controller checks result evidence'})
                    return await apply(worker, 'heartbeat', task)

                tasks = list(await asyncio.gather(*(progress(i) for i in range(len(tasks)))))
                observed = await call(0, 'shared_task_supervise', {
                    'task_ids': [task['task_id'] for task in tasks],
                    'after_seq': initial['snapshot_seq']})
                self.assertGreater(observed['snapshot_seq'], initial['snapshot_seq'])
                self.assertTrue(all(item['last_reported_progress_event'] for item in observed['tasks']))
                self.assertTrue(all(item['heartbeat_fresh'] for item in observed['tasks']))
                self.assertTrue(all(item['binding_status'] == 'current' for item in observed['tasks']))

                for index, (controller, worker) in enumerate(edges):
                    self.assertEqual(outputs[index], str(index * index))
                    task = await apply(worker, 'finish', tasks[index], exit_code=0)
                    self.assertEqual(task['status'], 'worker_done')
                    reviewer = (controller + 1) % participants
                    if reviewer == worker:
                        reviewer = (reviewer + 1) % participants
                    task = await apply(reviewer, 'review_start', task)
                    task = await apply(reviewer, 'review_bind', task,
                        review_lease_id=task['review']['lease_id'],
                        execution={'executor': 'fixture-native-review',
                                   'run_id': f'review-{index}-{reviewer}'})
                    task = await apply(reviewer, 'review_result', task,
                        review_lease_id=task['review']['lease_id'], decision='pass',
                        checks={
                            'source': 'Fixture task and run identity checked',
                            'diff': 'No source mutation; read-only fixture',
                            'tests': f'Independent arithmetic result checked: {index * index}',
                            'runtime': 'Worker completion receipt checked'})
                    task = await apply(controller, 'accept', task)
                    self.assertEqual(task['status'], 'accepted')
                    history = await call(controller, 'shared_task_history', {
                        'task_id': task['task_id'], 'after_seq': 0, 'limit': 100})
                    self.assertIn('bind', [event['action'] for event in history['events']])
                    self.assertIn('update', [event['action'] for event in history['events']])
                    self.assertEqual(history['events'][-1]['task']['status'], 'accepted')

    async def test_three_agents_reverse_roles_and_supervise_independent_tasks(self):
        await self.exercise(3)

    async def test_six_participants_have_no_fixed_three_agent_ceiling(self):
        await self.exercise(6)

    async def test_seven_participants_have_no_fixed_three_agent_ceiling(self):
        await self.exercise(7)


if __name__ == '__main__':
    unittest.main()
