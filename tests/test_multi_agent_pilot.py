"""评测样本隔离、失败计数和标签泄露保护。"""
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.run_multi_agent_pilot import select_cases, ObservedClient, metrics, ROOT


class PilotTests(unittest.TestCase):
    def test_sample_only_validation_balanced_and_deterministic(self):
        rows=[json.loads(x) for x in (ROOT/'pr_diff_100.jsonl').read_text().splitlines()]
        selected=select_cases(rows,20261013)
        self.assertEqual(selected,select_cases(rows,20261013))
        self.assertEqual(20,len({r['id'] for r in selected}))
        self.assertTrue(all(r['split']=='validation' for r in selected))
        self.assertEqual(10,sum(bool(r['expected_findings']) for r in selected))

    def test_failed_json_keeps_usage_and_does_not_expose_key(self):
        config={'base_url':'https://example.invalid/v1','api_key':'private-test-key','model':'test','provider':'custom'}
        body={'choices':[{'message':{'content':'not-json'},'finish_reason':'stop'}], 'usage':{'prompt_tokens':10,'completion_tokens':2}}
        class Response(io.BytesIO):
            status=200
            headers={'Content-Type':'application/json'}
        with tempfile.TemporaryDirectory(dir=ROOT/'docs/local/artifacts') as d:
            client=ObservedClient(config,Path(d),threading.Semaphore(1),1)
            with patch('urllib.request.urlopen',return_value=Response(json.dumps(body).encode())):
                with self.assertRaises(RuntimeError):client.complete_json('single-reviewer','system','unlabelled diff')
            self.assertFalse(client.calls[0]['ok'])
            self.assertTrue(client.calls[0]['usage_known'])
            self.assertEqual(10,client.calls[0]['usage']['prompt_tokens'])
            self.assertTrue(all('private-test-key' not in p.read_text() for p in Path(d).glob('*.json')))

    def test_failure_is_not_scored_as_clean_success(self):
        r={'tp':0,'fp':0,'fn':1,'expected':[{}],'findings':[],'status':'failed','wall_ms':10,'calls':[]}
        m=metrics([r]);self.assertEqual(0,m['recall']);self.assertEqual({'failed':1},m['status_counts'])

class UsageRecoveryTests(unittest.TestCase):
    def test_sse_cumulative_usage_uses_last_chunk_once(self):
        from scripts.summarize_multi_agent_pilot import recover_calls
        with tempfile.TemporaryDirectory(dir=ROOT/'docs/local/artifacts') as d:
            folder=Path(d)
            raw='data: '+json.dumps({'usage':{'prompt_tokens':10,'completion_tokens':1}})+'\n\ndata: '+json.dumps({'usage':{'prompt_tokens':10,'completion_tokens':3}})+'\n\ndata: [DONE]\n'
            (folder/'call-01.json').write_text(json.dumps({'response':raw}),encoding='utf-8')
            result={'calls':[{'ok':False,'usage_known':False,'usage':{}}]}
            call=recover_calls(folder,result)[0]
            self.assertEqual({'prompt_tokens':10,'completion_tokens':3},call['usage'])
            self.assertFalse(call['ok'])
            self.assertFalse(result['calls'][0]['usage_known'])
