"""改名后的配置优先级及历史内容兼容性。"""
import os
import unittest
from unittest.mock import mock_open, patch
from evol.config import load_dotenv, Settings
from evol.github import GitHubClient
from evol.skill_evolution import SkillEvolutionEngine


class RenameCompatibilityTests(unittest.TestCase):
    def test_legacy_process_environment_precedes_new_dotenv(self):
        with patch.dict(os.environ, {'EVOAGENT_LLM_MODEL': 'process-model'}, clear=True):
            with patch('builtins.open', mock_open(read_data='EVOL_LLM_MODEL=file-model\n')):
                load_dotenv(['unused.env'])
            self.assertEqual('process-model', Settings.from_env().llm_model)

    def test_new_prefix_wins_in_same_source(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch('builtins.open', mock_open(read_data='EVOAGENT_LLM_MODEL=old\nEVOL_LLM_MODEL=new\n')):
                load_dotenv(['unused.env'])
            self.assertEqual('new', Settings.from_env().llm_model)
        with patch.dict(os.environ, {'EVOL_LLM_MODEL': 'new', 'EVOAGENT_LLM_MODEL': 'old'}, clear=True):
            load_dotenv([])
            self.assertEqual('new', Settings.from_env().llm_model)

    def test_legacy_file_maps_to_new_prefix(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch('builtins.open', mock_open(read_data='EVOAGENT_LLM_MODEL=legacy\n')):
                load_dotenv(['unused.env'])
            self.assertEqual('legacy', Settings.from_env().llm_model)
            self.assertEqual('evol.db', Settings.from_env().db_path)

    def test_old_comment_is_updated_not_duplicated(self):
        client = GitHubClient('test-only')
        with patch.object(client, '_json', return_value=[{'body':'<!-- evoagent-review:task -->','url':'https://example.test/comment'}]) as call:
            client.upsert_comment('https://example.test/pr', 'report', '<!-- evol-review:task -->')
            self.assertEqual('PATCH',call.call_args.args[0])
            self.assertEqual('<!-- evol-review:task -->\nreport',call.call_args.args[2]['body'])

    def test_old_and_new_learned_blocks_can_be_removed(self):
        for name in ['evoagent', 'evol']:
            content=f'Keep\n<!-- {name}:learned:RULE:start -->\nold\n<!-- {name}:learned:RULE:end -->\n'
            self.assertEqual('Keep\n',SkillEvolutionEngine._remove_learned_block(content,'RULE'))
