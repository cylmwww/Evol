import json
from common import Case,BEFORE,AFTER
from evol.runtime import checkpoint
from evol.runtime.store import Store
from evol.runtime.models import RuntimeErrorCode

class StateTests(Case):
    def test_A01_normal_three_steps(self):
        self.create()
        result=self.r.start(self.rid)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.file.read_bytes(),AFTER)
        self.assert_source()
        self.assertEqual(result['budget']['dispatches_reserved'],3)
        self.assertEqual([s['status'] for s in self.rows('SELECT * FROM steps ORDER BY ordinal')],['succeeded']*3)
        self.assertEqual([a['execute_started'] for a in self.rows('SELECT * FROM budget_ledger')],[1]*3)
        self.assertEqual([a['settled'] for a in self.rows('SELECT * FROM budget_ledger')],[1]*3)
        self.assertEqual(len(self.rows('SELECT * FROM observations')),3)
        events=self.r.events(self.rid)
        self.assertEqual([e['sequence'] for e in events],list(range(1,len(events)+1)))
        self.assertEqual(sum(e['kind']=='run_completed' for e in events),1)
        self.assertIn('config.txt',self.r.result(self.rid)['files'])
        s=Store(self.state,readonly=True)
        try:
            checkpoint.verify(s,self.rid)
        finally:
            s.close()
    def test_output_size_prepare_rejects(self):
        self.plan['steps'][1]['args']['new_utf8']='x'*100
        self.create({'max_file_bytes':30})
        self.assertEqual(self.r.start(self.rid)['status'],'failed')
        self.assertEqual(self.file.read_bytes(),BEFORE)
        self.assertEqual(len(self.rows('SELECT * FROM attempts')),1)
        self.assert_source()
    def test_D05_store_transitions(self):
        self.create()
        s=Store(self.state)
        try:
            for target in ('completed','failed'):
                with self.assertRaises(RuntimeErrorCode),s.transaction():
                    s.transition(self.rid,target)
            with s.transaction():
                s.transition(self.rid,'stopping')
                s.transition(self.rid,'cancelled')
            with self.assertRaises(RuntimeErrorCode),s.transaction():
                s.transition(self.rid,'running')
            self.assertEqual(s.run(self.rid)['status'],'cancelled')
        finally:
            s.close()
