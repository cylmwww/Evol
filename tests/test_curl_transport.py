import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from scripts.curl_transport import post_json


class CurlTransportTests(unittest.TestCase):
    def client(self):
        return SimpleNamespace(base_url='https://example.invalid/v1', api_key='secret-test',
                               extra_headers={}, timeout=30)

    def test_credentials_only_in_stdin_and_body_round_trips(self):
        proc = subprocess.CompletedProcess([], 0, b'{"ok":true}\nEVOL_HTTP:200|application/json', b'')
        with patch('shutil.which', return_value='curl'), patch('subprocess.run', return_value=proc) as run:
            raw, status, content_type = post_json(self.client(), {'message': 'hello'})
        self.assertEqual(b'{"ok":true}', raw)
        self.assertEqual(200, status)
        self.assertNotIn('secret-test', str(run.call_args.args))
        self.assertIn(b'secret-test', run.call_args.kwargs['input'])
        self.assertNotIn('--location', run.call_args.args[0])

    def test_http_rejection_is_not_returned_as_success(self):
        import urllib.error
        proc = subprocess.CompletedProcess([], 0, b'denied\nEVOL_HTTP:403|application/json', b'')
        with patch('shutil.which', return_value='curl'), patch('subprocess.run', return_value=proc):
            with self.assertRaises(urllib.error.HTTPError):
                post_json(self.client(), {})

    def test_stderr_redacts_secret(self):
        proc = subprocess.CompletedProcess([], 1, b'', b'failure secret-test')
        with patch('shutil.which', return_value='curl'), patch('subprocess.run', return_value=proc):
            with self.assertRaisesRegex(RuntimeError, r'\[REDACTED\]'):
                post_json(self.client(), {})
