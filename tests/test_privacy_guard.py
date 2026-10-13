import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))

from privacy_guard import PrivacyError, check_metadata, guard, scrub, scanner_environment


class PrivacyTests(unittest.TestCase):
    def test_provider_secrets_and_keys(self):
        secrets = ['ghp_' + 'aB3' * 12, 'github_pat_' + 'A1_' * 30,
                   'sk-proj-' + 'aB3_' * 20, 'AIza' + 'a' * 35,
                   'AKIA' + 'A1' * 8, 'xoxb-' + '1234567890-' * 2 + 'a' * 24,
                   'eyJ' + 'a' * 20 + '.' + 'b' * 24 + '.' + 'c' * 24]
        for secret in secrets:
            with self.subTest(prefix=secret[:5]):
                self.assertNotIn(secret, json.dumps(scrub({'nested': [secret], secret: 'ok'})))
                with self.assertRaises(PrivacyError):
                    check_metadata({'resources': [secret]})

    def test_context_and_url(self):
        fake = '-'.join(['violet', 'raven', 'secret'])
        for text in [json.dumps({'password': fake}),
                     "api_key=" + repr(fake), 'Bearer ' + fake,
                     'https://' + 'alice:' + fake + '@example.test/path',
                     json.dumps({'token': fake})]:
            self.assertNotIn('violet', scrub(text))
        self.assertEqual(scrub({'password': fake, 'TOKEN': fake}),
                         {'password': '[REDACTED]', 'TOKEN': '[REDACTED]'})

    def test_short_bearer_and_nested_sensitive_values(self):
        self.assertEqual(scrub('Authorization: Bearer short'), 'Authorization: [REDACTED]')
        self.assertEqual(scrub('Bearer short'), 'Bearer [REDACTED]')
        self.assertNotIn('secret', scrub('Authorization: Basic secret'))
        self.assertEqual(scrub({'password': {'value': 'violet-raven-secret'}}), {'password': '[REDACTED]'})

    def test_unsupported_and_cyclic_inputs_fail_safely(self):
        circular = []
        circular.append(circular)
        for value in (circular, b'original', {'password': object()}):
            with self.assertRaises(PrivacyError) as error:
                scrub(value)
            self.assertNotIn('original', str(error.exception))

    def test_pem_complete_and_truncated(self):
        def block(kind, body, end=True):
            return '-----BEGIN ' + kind + '-----\n' + body + ('\n-----END ' + kind + '-----' if end else '')
        for text in ['before\n' + block('PRIVATE KEY', 'QUJD1234') + '\nafter',
                     'before\n' + block('RSA PRIVATE KEY', 'QUJD1234\ntruncated body', False)]:
            self.assertNotIn('QUJD1234', scrub(text))
            self.assertNotIn('truncated body', scrub(text))
        self.assertTrue(scrub(block('PRIVATE KEY', 'x') + '\nafter').endswith('after'))
        certificate = block('CERTIFICATE', 'public')
        self.assertEqual(scrub(certificate), certificate)

    def test_short_legacy_and_slack_app_tokens(self):
        for text in ('sk-' + 'a' * 16, 'xapp-' + '1-a' * 10, 'xoxb-' + 'a' * 8):
            self.assertEqual(scrub(text), '[REDACTED]')

    def test_scanner_has_no_inherited_credentials(self):
        import os
        from unittest.mock import patch
        code = 'import os,sys; sys.exit(1 if "PRIVACY_TEST_CREDENTIAL" in os.environ or os.environ.get("BETTERLEAKS_VALIDATE")!="false" else 0)'
        with patch.dict(os.environ, {'PRIVACY_TEST_CREDENTIAL': 'synthetic-secret'}):
            self.assertEqual(guard('safe', [sys.executable, '-c', code]), 'safe')

    def test_windows_environment_preserves_only_system_root(self):
        from unittest.mock import patch
        with patch('privacy_guard.os.name', 'nt'), patch('privacy_guard.os.environ', {'SystemRoot':'system-directory','PRIVACY_TEST_CREDENTIAL':'excluded'}):
            env=scanner_environment()
            self.assertEqual(env['SystemRoot'],'system-directory')
            self.assertNotIn('PRIVACY_TEST_CREDENTIAL',env)
            self.assertEqual(set(env),{'SystemRoot','PATH','HOME','BETTERLEAKS_VALIDATE','BETTERLEAKS_ANALYZE'})
        with patch('privacy_guard.os.name','nt'), patch('privacy_guard.os.environ',{}):
            with self.assertRaises(PrivacyError):scanner_environment()

    def test_safe_metadata_and_placeholders(self):
        safe = {'task_id': '019c6e27-e55b-73d1-87d8-4e01f1f75043',
                'resources': ['/home/example/project/api', 'api_key documentation'],
                'note': 'OpenAI API keys and Bearer authentication discussed here',
                'password': '<PASSWORD>', 'token': '${TOKEN}'}
        self.assertEqual(check_metadata(safe), safe)
        self.assertEqual(scrub(safe), safe)

    def test_recursive_shapes_and_collision(self):
        value = {'a': ('ghp_' + 'a' * 36, 7, None), 'b': {'password': ''.join(['se', 'cret'])}}
        self.assertEqual(scrub(value), {'a': ('[REDACTED]', 7, None), 'b': {'password': '[REDACTED]'}})
        with self.assertRaises(PrivacyError):
            scrub({'ghp_' + 'a' * 36: 1, 'ghp_' + 'b' * 36: 2})

    def test_idempotent_and_text_placeholders(self):
        for text in ('Authorization: Bearer short', 'Bearer short',
                     'password=short', 'https://' + 'alice:' + 'secret' + '@example.test'):
            self.assertEqual(scrub(scrub(text)), scrub(text))
        for text in ('password=${PASSWORD}', 'api_key=<API_KEY>', 'token=[REDACTED]'):
            self.assertEqual(scrub(text), text)

    def test_scanner_real_process_safe_and_reject(self):
        safe_scanner = [sys.executable, '-c',
                        'import json,sys; v=json.load(sys.stdin); sys.exit(0 if v["password"]=="[REDACTED]" else 1)']
        self.assertEqual(guard({'password': ''.join(['ori', 'ginal'])}, scanner=safe_scanner), {'password': '[REDACTED]'})
        for code in (1, 2):
            with self.assertRaises(PrivacyError) as error:
                guard({'note': 'original'}, scanner=[sys.executable, '-c',
                      'import sys; print("original"); print("original",file=sys.stderr); sys.exit(' + str(code) + ')'])
            self.assertNotIn('original', str(error.exception))
        with self.assertRaises(PrivacyError):
            guard('safe', scanner=['/nonexistent/privacy-scanner'])
        with self.assertRaises(PrivacyError):
            guard('safe', scanner='echo safe')


if __name__ == '__main__':
    unittest.main()
