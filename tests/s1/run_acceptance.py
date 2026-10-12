"""可复现的验收汇总；保留失败，不将跳过计为通过。"""
import json
from pathlib import Path
import sys
import unittest

class Evidence(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.outcomes={}
    def addSuccess(self,test):
        super().addSuccess(test)
        self.outcomes[test.id()]='passed'
    def addFailure(self,test,err):
        super().addFailure(test,err)
        self.outcomes[test.id()]='failed'
    def addError(self,test,err):
        super().addError(test,err)
        self.outcomes[test.id()]='error'
    def addSkip(self,test,reason):
        super().addSkip(test,reason)
        self.outcomes[test.id()]='skipped'
    def addSubTest(self,test,subtest,err):
        super().addSubTest(test,subtest,err)
        if err:
            self.outcomes[test.id()]='failed'

root=Path(__file__).resolve().parents[2]
suite=unittest.defaultTestLoader.discover(str(root/'tests/s1'),pattern='test_*.py')
result=unittest.TextTestRunner(verbosity=2,resultclass=Evidence).run(suite)
ids=['A01','A02','A03','A04','A05']+['R%02d'%i for i in range(1,11)]+['C%02d'%i for i in range(1,7)]+['B%02d'%i for i in range(1,7)]+['D%02d'%i for i in range(1,6)]
matrix={}
for ident in ids:
    selected={n:v for n,v in result.outcomes.items() if 'test_'+ident+'_' in n}
    matrix[ident]={'status':'passed' if selected and all(v=='passed' for v in selected.values()) else 'incomplete','tests':selected}
data={'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'outcomes':result.outcomes,'matrix':matrix}
(root/'docs/local/artifacts/s1/acceptance-summary.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
raise SystemExit(0 if result.wasSuccessful() else 1)
