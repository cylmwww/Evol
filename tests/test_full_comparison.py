"""冻结划分和复核计分的关键边界。"""
import json
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from scripts import run_full_comparison as experiment


class FullComparisonTests(unittest.TestCase):
    def test_split_excludes_renamed_duplicate_templates(self):
        rows=[json.loads(x) for x in (experiment.ROOT/'pr_diff_100.jsonl').read_text(encoding='utf-8').splitlines()]
        splits=experiment.split_cases(rows)
        self.assertEqual([40,20,40],[len(splits[k]) for k in ['feedback','validation','test']])
        self.assertEqual(100,len(set().union(*map(set,splits.values()))))
        by_id={r['id']:r for r in rows}
        keys=[{experiment.template(by_id[c]) for c in splits[s]} for s in ['feedback','validation','test']]
        self.assertFalse(keys[0]&keys[1] or keys[0]&keys[2] or keys[1]&keys[2])
        self.assertEqual(splits,experiment.split_cases(rows))

    def test_failed_empty_trial_retains_all_false_negatives(self):
        trial={'arm':'D-full','id':'x','status':'failed','error':'timeout','findings':[], 'summary':{},'calls':[], 'wall_ms':1}
        adj={'items':[],'references':{}}
        truth={'x':[{'issue':'one'},{'issue':'two'}]}
        with patch.object(experiment,'read',side_effect=[trial,adj,truth]):
            result=experiment.scored('x','multi',1,'final')
        self.assertEqual((0,0,2),(result['tp'],result['fp'],result['fn']))
        self.assertFalse(result['final_workflow_completed'])

    def test_semantic_duplicates_merged_and_unknown_kept_false_positive(self):
        trial={'arm':'A-single','id':'x','status':'complete','error':'','findings':[{}, {}, {}], 'summary':{},'calls':[],'wall_ms':1}
        adj={'items':[{'key':'a','issue':'one'},{'key':'b','issue':'one'},{'key':'c','issue':None}],
             'references':{key:{'group':'single','repeat':1} for key in 'abc'}}
        with patch.object(experiment,'read',side_effect=[trial,adj,{'x':[{'issue':'one'},{'issue':'two'}]}]):
            result=experiment.scored('x','single',1,'final')
        self.assertEqual((1,1,1,1),(result['tp'],result['fp'],result['fn'],result['duplicates']))

    def test_transport_failure_is_not_a_learning_miss(self):
        issue={'issue':'one','rule_id':'SEM-ONE','evidence':'return value'}
        runs=[{'repeat':1,'missing':['one'],'final_workflow_completed':False},
              {'repeat':2,'missing':[],'final_workflow_completed':True},
              {'repeat':3,'missing':[],'final_workflow_completed':True}]
        original=SimpleNamespace(name='correctness-review',to_artifact=lambda:{})
        with patch.object(experiment,'read',side_effect=[{'x':[issue]},{'items':[]}]), \
             patch.object(experiment,'scored',side_effect=runs), \
             patch.object(experiment,'save'), \
             patch.object(experiment.SkillEvolutionEngine,'build_candidate_artifact',return_value={'artifact':{}}) as build, \
             patch.object(experiment.AgentSkill,'from_artifact'):
            experiment.feedback({'splits':{'feedback':['x']}},[original])
        record=build.call_args.args[2][0]
        self.assertEqual('confirmed_correct',record['category'])
        self.assertEqual([],record['missed_in_repeats'])

if __name__=='__main__':
    unittest.main()
