"""确认新准入边界，测试全程不发网络请求。"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from scripts.resume_gpt_comparison_600s import patched_method, ROOT
from scripts.run_protocol_pilot import ObservedClient


class AdmissionTests(unittest.TestCase):
    def client(self, folder):
        config = {'provider': 'custom', 'base_url': 'https://api.joyehuang.dev/v1',
                  'api_key': 'fixture-not-a-real-key', 'model': 'fixture'}
        return ObservedClient(config, Path(folder), threading.BoundedSemaphore(1), 180)

    def test_request_after_350_seconds_is_admitted(self):
        out = ROOT / 'docs/local/artifacts/full-comparison-100'
        with tempfile.TemporaryDirectory(dir=out) as folder:
            client = self.client(folder)
            client.started = time.monotonic() - 350
            method, _ = patched_method()
            data = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}], 'usage': {}}
            method.__globals__['post_json'] = lambda *args: (json.dumps(data).encode(), 200, 'application/json')
            self.assertTrue(method(client, 'semantic-adjudicator', 'fixture', 'fixture')['ok'])
            self.assertEqual(len(client.calls), 1)

    def test_request_after_601_seconds_is_rejected_without_transport(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'docs/local/artifacts/full-comparison-100') as folder:
            client = self.client(folder)
            client.started = time.monotonic() - 601
            method, _ = patched_method()
            with self.assertRaisesRegex(RuntimeError, 'ceiling exceeded'):
                method(client, 'semantic-adjudicator', 'fixture', 'fixture')
            self.assertEqual(client.calls, [])

    def test_request_count_ceiling_remains_16(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'docs/local/artifacts/full-comparison-100') as folder:
            client = self.client(folder)
            client.calls = [{}] * 16
            method, _ = patched_method()
            with self.assertRaisesRegex(RuntimeError, 'ceiling exceeded'):
                method(client, 'semantic-adjudicator', 'fixture', 'fixture')


if __name__ == '__main__':
    unittest.main()
