"""新实验与历史结果隔离，以及复核重试的证据保留。"""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from scripts import run_deepseek_comparison as run


class DeepSeekExperimentTests(unittest.TestCase):
    def test_output_is_separate_from_previous_experiment(self):
        self.assertNotEqual(run.OUT.resolve(),run.PREVIOUS.resolve())
        self.assertEqual(run.experiment.OUT,run.OUT)

    def test_transient_judge_failure_preserves_previous_attempt(self):
        with tempfile.TemporaryDirectory(dir=run.OUT) as folder:
            out=Path(folder)
            attempts=[]
            def judge(*args):
                p=out/'judge-calls/feedback/case'
                p.mkdir(parents=True,exist_ok=True)
                (p/'call-01-meta.json').write_text('evidence')
                attempts.append(1)
                if len(attempts)==1:raise RuntimeError('HTTP Error 502')
                return {'items':[]}
            with patch.object(run,'OUT',out),patch.object(run,'original_adjudicate',side_effect=judge),patch.object(run.time,'sleep'):
                result=run.resumable_adjudicate('case',[],'feedback',{}, {'api_key':'dummy'},None)
            self.assertEqual({'items':[]},result)
            self.assertTrue((out/'judge-attempts/feedback/case/attempt-01/call-01-meta.json').exists())
            self.assertTrue((out/'judge-calls/feedback/case/call-01-meta.json').exists())

    def test_authentication_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory(dir=run.OUT) as folder:
            with patch.object(run,'OUT',Path(folder)),patch.object(run,'original_adjudicate',side_effect=RuntimeError('HTTP Error 401')) as judge:
                with self.assertRaises(RuntimeError):
                    run.resumable_adjudicate('case',[],'feedback',{}, {'api_key':'dummy'},None)
                self.assertEqual(1,judge.call_count)


if __name__=='__main__':unittest.main()
