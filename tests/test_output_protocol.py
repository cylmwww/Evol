import io
import json
import unittest
from unittest.mock import patch

from evol.llm import JsonChatClient, OutputProtocolError, decode_output
from evol.runtime import RuntimeBudgetExceeded
from evol.telemetry import ExecutionLedger


def response(content):
    return io.BytesIO(json.dumps({'choices': [{'message': {'content': content}}],
                                 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode())


class OutputProtocolTests(unittest.TestCase):
    def test_deepseek_action_mode_is_explicit_and_endpoint_scoped(self):
        client = JsonChatClient('https://api.deepseek.com/v1', 'test', 'deepseek-flash')
        with patch('urllib.request.urlopen', return_value=response('{"action":"final","findings":[]}')) as transport:
            client.complete_json('security', '', '{}')
        self.assertEqual({'type': 'disabled'}, json.loads(transport.call_args.args[0].data)['thinking'])
        self.assertEqual({}, client.request_options('other'))
        self.assertEqual({}, JsonChatClient('https://example.invalid', 'test', 'deepseek-flash').request_options('security'))

    def test_unambiguous_aliases_are_normalized_and_traced(self):
        ledger = ExecutionLedger('test')
        for value in [
            {'tool': 'read_file', 'path': 'a.py', 'start_line': 1},
            {'action': 'tool', 'tool': 'read_file', 'parameters': {'path': 'a.py'}},
        ]:
            result = decode_output({'message': {'content': json.dumps(value)}}, 'security', ledger)
            self.assertEqual('tool', result['action'])
            self.assertEqual('a.py', result['arguments']['path'])
        result = decode_output({'message': {'content': '{"revision_requests":[]}'}}, 'lead', ledger)
        self.assertEqual('final', result['action'])
        self.assertEqual(3, len(ledger.agent_traces['security']) + len(ledger.agent_traces['lead']))

    def test_ambiguous_or_truncated_output_never_salvaged(self):
        for value in [
            {'action': 'tool', 'tool': 'read_file', 'arguments': {}, 'parameters': {}},
            {'tool': 'read_file', 'findings': []},
            {'delegations': [], 'revision_requests': []},
        ]:
            with self.assertRaises(OutputProtocolError):
                decode_output({'message': {'content': json.dumps(value)}}, 'lead')
        with self.assertRaises(OutputProtocolError):
            decode_output({'finish_reason': 'length', 'message': {'content': '{"action":"final","findings":[]}'}}, 'security')

    def test_tiny_remaining_budget_does_not_send_truncated_request(self):
        with patch('urllib.request.urlopen') as transport:
            with self.assertRaises(RuntimeBudgetExceeded):
                JsonChatClient('https://example.invalid', 'test', 'test').complete_json(
                    'security', '', '{"remaining_token_budget":331}', ExecutionLedger('test'), 331)
        transport.assert_not_called()

    def test_retry_accounts_for_first_attempt_input_and_output(self):
        body = {'choices': [{'message': {'content': '{}'}}],
                'usage': {'prompt_tokens': 2000, 'completion_tokens': 1500}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(body).encode())) as transport:
            with self.assertRaises(RuntimeBudgetExceeded):
                JsonChatClient('https://example.invalid', 'test', 'test').complete_json(
                    'security', '', '{"remaining_token_budget":4000}', ExecutionLedger('test'), 4000)
        self.assertEqual(1, transport.call_count)

    def test_retry_rejects_concatenated_actions_and_preserves_usage(self):
        ledger = ExecutionLedger('test')
        with patch('urllib.request.urlopen', side_effect=[
            response('{"action":"final","findings":[]} {}'),
            response('{"action":"final","findings":[]}'),
        ]) as transport:
            result = JsonChatClient('https://example.invalid', 'test', 'test').complete_json(
                'security', 'Return JSON', '{}', ledger)
        self.assertEqual([], result['findings'])
        self.assertEqual(2, transport.call_count)
        self.assertEqual([False, True], [call.ok for call in ledger.model_calls])
        self.assertEqual(30, sum(call.input_tokens + call.output_tokens for call in ledger.model_calls))
        payload = json.loads(transport.call_args.args[0].data)
        self.assertIn('previous response failed', payload['messages'][0]['content'])

    def test_retry_is_bounded_and_invalid_schema_never_returned(self):
        with patch('urllib.request.urlopen', side_effect=[response('{"content":{}}'), response('{"action":"tool","tool":"read_file","arguments":[]}')]) as transport:
            with self.assertRaises(OutputProtocolError):
                JsonChatClient('https://example.invalid', 'test', 'test').complete_json('security', '', '{}')
        self.assertEqual(2, transport.call_count)

    def test_transport_failure_not_retried(self):
        import urllib.error
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('offline')) as transport:
            with self.assertRaises(RuntimeError):
                JsonChatClient('https://example.invalid', 'test', 'test').complete_json('security', '', '{}')
        self.assertEqual(1, transport.call_count)

    def test_other_json_clients_keep_their_schema(self):
        with patch('urllib.request.urlopen', return_value=response('{"summary":"ok"}')):
            self.assertEqual({'summary': 'ok'}, JsonChatClient('https://example.invalid', 'test', 'test').complete_json('other', '', '{}'))
