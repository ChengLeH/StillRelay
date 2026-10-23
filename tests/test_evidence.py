"""Evidence integrity on the real transactional state entry point."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import state


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.config_path = self.base / 'config.json'
        state.bootstrap(self.config_path, self.base / 'db', self.base / 'exchange')
        self.config = json.loads(self.config_path.read_text())
        self.output = self.base / 'output'
        self.output.mkdir()
        self.config['artifact_roots'] = [str(self.output)]
        self.store = state.Store(self.config)
        self.task = self.apply('create', execution_pool='cloud', read_only=True,
                               fields={'title': 'Evidence', 'goal': 'Check integrity'})
        self.task = self.apply('claim')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def request(self, op, **values):
        r = dict(schema_version=1, request_id=uuid.uuid4().hex, actor='worker', op=op, **values)
        if hasattr(self, 'task'):
            r.update(task_id=self.task['task_id'], expected_revision=self.task['revision'])
            if self.task.get('lease_id'):
                r['lease_id'] = self.task['lease_id']
        return r

    def apply(self, op, **values):
        return self.store.apply(self.request(op, **values))['task']

    def file_check(self, content=b'fixture'):
        path = self.output / 'result.txt'
        path.write_bytes(content)
        return {'path': str(path), 'sha256': hashlib.sha256(content).hexdigest()}

    def fact(self, identifier='fact-1'):
        seq = self.store.db.execute('SELECT MAX(seq) FROM events').fetchone()[0]
        return {'id': identifier, 'statement': 'Fixture was inspected',
                'method': 'Read the referenced event; this is a reported observation',
                'sources': [{'kind': 'event', 'seq': seq}]}

    def test_missing_artifact_rejects_finish_without_consuming_lease(self):
        check = self.file_check()
        Path(check['path']).unlink()
        before = self.store.get(self.task['task_id'])
        with self.assertRaises(state.StateError):
            self.apply('finish', exit_code=0, artifact_checks=[check], report='must not be written')
        self.assertEqual(self.store.get(self.task['task_id']), before)
        self.assertFalse(list((self.base / 'db' / 'reports').glob('*')))

    def test_hash_mismatch_rejects_finish(self):
        check = self.file_check()
        check['sha256'] = '0' * 64
        with self.assertRaises(state.StateError):
            self.apply('finish', exit_code=0, artifact_checks=[check])

    def test_verified_artifact_and_idempotent_replay_do_not_reopen_file(self):
        check = self.file_check()
        request = self.request('finish', exit_code=0, artifact_checks=[check])
        result = self.store.apply(request)
        t = result['task']
        self.assertEqual(t['status'], 'worker_done')
        self.assertEqual(t['artifact_evidence'][0]['sha256'], check['sha256'])
        self.assertEqual(t['artifact_evidence'][0]['size_bytes'], 7)
        self.assertIn(check['path'], t['artifacts'])
        Path(check['path']).unlink()
        self.assertEqual(self.store.apply(request), result)

    def test_path_and_open_file_ctime_differences_do_not_reject_matching_artifact(self):
        check = self.file_check()
        original_stat = Path.stat
        target = Path(check['path'])

        def path_stat_with_different_ctime(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == target:
                return SimpleNamespace(
                    st_dev=result.st_dev, st_ino=result.st_ino, st_size=result.st_size,
                    st_mtime_ns=result.st_mtime_ns, st_ctime_ns=result.st_ctime_ns + 1)
            return result

        with mock.patch.object(Path, 'stat', new=path_stat_with_different_ctime):
            result = state.evidence.inspect_files(
                [check], self.config['artifact_roots'], self.task, 'worker')

        self.assertEqual(result[0]['sha256'], check['sha256'])

    def test_file_access_requires_opt_in_roots_and_regular_file(self):
        check = self.file_check()
        for root in ([], [str(self.base / 'other')]):
            with self.subTest(root=root):
                self.store.config['artifact_roots'] = root
                with self.assertRaises(state.StateError):
                    self.apply('finish', exit_code=0, artifact_checks=[check])
        self.store.config['artifact_roots'] = [str(self.output)]
        with self.assertRaises(state.StateError):
            self.apply('finish', exit_code=0,
                       artifact_checks=[{'path': str(self.output), 'sha256': '0' * 64}])

    def test_unverified_reference_is_explicit(self):
        t = self.apply('finish', exit_code=0, fields={'artifacts': ['remote://worker/result']})
        self.assertEqual(t['artifact_evidence'], [])
        self.assertEqual(t['evidence_contract_version'], 1)
        self.assertEqual(t['unverified_artifacts'], ['remote://worker/result'])

    def test_no_directory_escape_or_special_files(self):
        outside = self.base / 'outside.txt'
        outside.write_bytes(b'fixture')
        check = {'path': str(outside), 'sha256': hashlib.sha256(b'fixture').hexdigest()}
        with self.assertRaises(state.StateError):
            self.apply('finish', exit_code=0, artifact_checks=[check])
        link = self.output / 'link'
        try:
            link.symlink_to(outside)
        except OSError:
            if sys.platform == 'win32':
                self.skipTest('Windows account lacks symbolic-link privilege')
            raise
        with self.assertRaises(state.StateError):
            self.apply('finish', exit_code=0, artifact_checks=[dict(check, path=str(link))])

    def test_changed_or_oversized_files_reject(self):
        check = self.file_check()
        original_sha = hashlib.sha256
        replacement = b'content changed to a different length'

        def changing_digest():
            inner = original_sha()
            class Digest:
                def update(self, chunk):
                    inner.update(chunk)
                    Path(check['path']).write_bytes(replacement)
                def hexdigest(self):
                    return inner.hexdigest()
            return Digest()
        # Patch only the file-inspection call: request-id hashing retains the real constructor.
        with mock.patch.object(state.evidence.hashlib, 'sha256', side_effect=changing_digest):
            with self.assertRaises(state.StateError):
                state.evidence.inspect_files([check], self.config['artifact_roots'], self.task, 'worker')
        self.assertEqual(Path(check['path']).read_bytes(), replacement)
        with open(check['path'], 'wb') as stream:
            stream.truncate(state.evidence.MAX_BYTES + 1)
        with self.assertRaises(state.StateError):
            self.apply('finish', exit_code=0, artifact_checks=[check])

    def test_cumulative_lists_survive_update_finish_and_plan(self):
        self.task = self.apply('update', fields={'failed_attempts': ['a'], 'artifacts': ['ref-a'],
                                                'backups': ['backup-a'], 'blockers': ['pending']})
        self.task = self.apply('update', fields={'failed_attempts': ['b'], 'artifacts': [],
                                                'backups': [], 'blockers': []})
        self.assertEqual(self.task['failed_attempts'], ['a', 'b'])
        self.assertEqual(self.task['blockers'], [])
        self.task = self.apply('finish', exit_code=0, fields={'failed_attempts': [], 'artifacts': []}, report='report')
        artifacts = self.task['artifacts'][:]
        self.task = self.apply('plan', fields={'failed_attempts': [], 'artifacts': [], 'backups': []})
        self.assertEqual(self.task['failed_attempts'], ['a', 'b'])
        self.assertEqual(self.task['artifacts'], artifacts)
        self.assertEqual(self.task['backups'], ['backup-a'])

    def test_merged_total_limit_is_enforced(self):
        self.task = self.apply('update', fields={'failed_attempts': [str(i) for i in range(100)]})
        with self.assertRaises(state.StateError):
            self.apply('update', fields={'failed_attempts': ['one-too-many']})
        self.task = self.apply('update', fields={'summary': 'x' * 24000})
        with self.assertRaises(state.StateError):
            self.apply('update', fields={'next_step': 'y' * 24000})
        self.assertEqual(self.store.get(self.task['task_id']), self.task)
        self.task = self.apply('update', fields={'artifacts': ['ref-' + str(i) for i in range(100)]})
        with self.assertRaises(state.StateError):
            self.apply('finish', exit_code=0, report='cannot append the 101st reference')
        self.assertFalse(list((self.base / 'db' / 'reports').glob('*')))

    def test_unsourced_fact_and_forged_verification_rejected(self):
        for value in ('trust me', {'id': 'x', 'statement': 'claim', 'method': 'guess', 'sources': []},
                      dict(self.fact(), status='independently_verified')):
            with self.subTest(value=value), self.assertRaises(state.StateError):
                self.apply('update', fields={'verified_facts': [value]})

    def test_event_source_must_exist_in_same_task(self):
        bad = self.fact()
        bad['sources'] = [{'kind': 'event', 'seq': 999999}]
        with self.assertRaises(state.StateError):
            self.apply('update', fields={'verified_facts': [bad]})
        bad['sources'][0]['seq'] = 10**100
        with self.assertRaises(state.StateError):
            self.apply('update', fields={'verified_facts': [bad]})
        another = self.request('create', execution_pool='cloud', read_only=True,
                               fields={'title': 'Other task', 'goal': 'Foreign source'})
        another.pop('task_id')
        self.store.apply(another)
        bad['sources'][0]['seq'] = self.store.db.execute('SELECT MAX(seq) FROM events').fetchone()[0]
        with self.assertRaises(state.StateError):
            self.apply('update', fields={'verified_facts': [bad]})

    def test_source_backed_fact_is_not_independently_verified(self):
        fact = self.fact()
        self.task = self.apply('update', fields={'verified_facts': [fact]})
        saved = self.task['verified_facts'][0]
        self.assertEqual(saved['status'], 'reported_with_source')
        self.assertEqual(saved['reported_by'], 'worker')
        self.task = self.apply('update', fields={'verified_facts': []})
        self.assertEqual(self.task['verified_facts'], [saved])
        # Copying a read snapshot back is supported, without trusting its server metadata.
        self.task = self.apply('update', fields={'verified_facts': [saved]})
        self.assertEqual(self.task['verified_facts'], [saved])
        changed = dict(fact, statement='different claim')
        with self.assertRaises(state.StateError):
            self.apply('update', fields={'verified_facts': [changed]})

    def test_artifact_source_requires_actual_hash_check(self):
        check = self.file_check()
        fact = dict(self.fact(), sources=[dict(kind='artifact', **check)])
        with self.assertRaises(state.StateError):
            self.apply('update', fields={'verified_facts': [fact]})
        self.task = self.apply('finish', exit_code=0, artifact_checks=[check],
                               fields={'verified_facts': [fact]})
        self.assertEqual(self.task['verified_facts'][0]['status'], 'reported_with_source')

    def test_retraction_preserves_original_and_reason(self):
        self.task = self.apply('update', fields={'failed_attempts': ['incorrect record']})
        self.task = self.apply('update', retractions=[{'field': 'failed_attempts',
                                'value': 'incorrect record', 'reason': 'Wrong task attribution'}])
        self.assertEqual(self.task['failed_attempts'], ['incorrect record'])
        self.assertEqual(self.task['retractions'][0]['reason'], 'Wrong task attribution')
        with self.assertRaises(state.StateError):
            self.apply('update', retractions=[{'field': 'failed_attempts', 'value': 'missing', 'reason': 'x'}])

    def test_legacy_strings_are_marked_without_rewriting_old_events(self):
        t = dict(self.task)
        t['verified_facts'] = ['old claim']
        self.store.db.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(t), t['task_id']))
        self.store.db.commit()
        view = self.store.get(t['task_id'])
        self.assertEqual(view['verified_facts'][0]['status'], 'legacy_unverified')
        self.assertEqual(view['verified_facts'][0]['statement'], 'old claim')
        self.task = view
        self.task = self.apply('update', fields={'summary': 'resume'})
        self.assertEqual(self.task['verified_facts'][0]['status'], 'legacy_unverified')


if __name__ == '__main__':
    unittest.main()
