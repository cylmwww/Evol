from contextlib import closing
import json
import sqlite3
from unittest.mock import patch
from common import Case,BEFORE,AFTER
from evol.runtime.service import Runtime
from evol.runtime.models import RuntimeErrorCode
from test_budget_cancel import FakeClock
from evol.runtime.scheduler import Scheduler
import time

class DeliveryTests(Case):
    def test_B04_deadline_during_final_verification(self):
        self.create({'max_wall_seconds':60})
        clock=FakeClock(time.time())
        self.r=Runtime(self.state,clock=clock)
        original=Scheduler.check_files
        crossed=[]
        def slow_final_check(scheduler):
            original(scheduler)
            if scheduler.store.db.in_transaction and scheduler.store.run(self.rid)['status']=='validating':
                clock.now+=61
                clock.mono+=61
                crossed.append(True)
        with patch.object(Scheduler,'check_files',slow_final_check):
            value=self.r.start(self.rid)
        self.assertTrue(crossed)
        self.assertEqual(value['status'],'budget_exhausted')
        self.assertEqual(value['stop_reason'],'deadline_exceeded')
        self.assertEqual(self.rows("SELECT * FROM artifacts WHERE purpose='result'"),[])
        with self.assertRaises(RuntimeErrorCode):
            self.r.result(self.rid)
        self.assertEqual(self.file.read_bytes(),AFTER)
        self.assert_source()

    def test_D01_result_written_crash(self):
        self.create()
        self.crash('result_written','*')
        self.assertTrue((self.state/'runs'/self.rid/'artifacts/result.json').exists())
        self.assertEqual(self.rows("SELECT * FROM artifacts WHERE purpose='result'"),[])
        self.assertEqual(self.cli('result',self.rid).returncode,7)
        self.assertEqual(self.r.resume(self.rid)['status'],'completed')
        self.assertEqual(len(self.rows('SELECT * FROM budget_ledger')),3)
        self.assertEqual(self.file.read_bytes(),AFTER)
        self.assert_source()
    def test_D01_cancel_after_result_crash(self):
        self.create()
        self.crash('result_written','*')
        self.r.cancel(self.rid)
        self.assertEqual(self.r.resume(self.rid)['status'],'cancelled')
        self.assertEqual(self.cli('result',self.rid).returncode,7)
        self.assertEqual(self.rows("SELECT * FROM artifacts WHERE purpose='result'"),[])
    def test_D02_result_modified(self):
        self.create()
        self.r.start(self.rid)
        result=self.state/'runs'/self.rid/'artifacts/result.json'
        result.write_bytes(b'{}')
        self.assertEqual(self.cli('result',self.rid).returncode,7)
        self.assertEqual(self.r.inspect(self.rid)['status'],'completed')
    def test_D02_result_missing(self):
        self.create()
        self.r.start(self.rid)
        (self.state/'runs'/self.rid/'artifacts/result.json').unlink()
        self.assertEqual(self.cli('result',self.rid).returncode,7)
        self.assertEqual(self.r.inspect(self.rid)['status'],'completed')
    def test_D03_all_terminal_results_blocked(self):
        for kind in ('cancelled','failed','conflict','budget_exhausted'):
            with self.subTest(kind=kind):
                r=Runtime(self.base/kind)
                plan=json.loads(json.dumps(self.plan))
                if kind=='failed':
                    plan['steps'][1]['args']['old_utf8']='absent'
                rid=r.create(self.source,plan,{'max_dispatches':1} if kind=='budget_exhausted' else None)['run_id']
                if kind=='cancelled':
                    r.cancel(rid)
                if kind=='conflict':
                    (self.base/kind/'runs'/rid/'candidate/config.txt').write_bytes(b'external')
                self.assertEqual(r.resume(rid)['status'],kind)
                with self.assertRaises(RuntimeErrorCode):
                    r.result(rid)
    def test_D04_checkpoint_tamper(self):
        self.create()
        with closing(sqlite3.connect(self.state/'runtime.sqlite3',isolation_level=None)) as db:
            db.execute("UPDATE checkpoints SET state_json='{}'")
        self.assertEqual(self.cli('resume',self.rid).returncode,7)
        self.assertEqual(self.file.read_bytes(),BEFORE)
    def test_D04_ledger_mismatch(self):
        self.create()
        self.crash('after_intent')
        with closing(sqlite3.connect(self.state/'runtime.sqlite3',isolation_level=None)) as db:
            db.execute('DELETE FROM budget_ledger WHERE rowid=(SELECT max(rowid) FROM budget_ledger)')
        self.assertEqual(self.cli('resume',self.rid).returncode,7)
        self.assertEqual(self.file.read_bytes(),BEFORE)
    def test_D04_step_evidence_mismatch(self):
        self.create()
        self.crash('after_step_commit')
        with closing(sqlite3.connect(self.state/'runtime.sqlite3',isolation_level=None)) as db:
            db.execute("UPDATE steps SET result_observation_id=NULL WHERE step_id='change'")
        self.assertEqual(self.cli('resume',self.rid).returncode,7)
        self.assertEqual(self.file.read_bytes(),AFTER)
    def test_artifact_evidence_missing(self):
        self.create()
        self.crash('after_step_commit')
        artifact=self.rows("SELECT * FROM artifacts WHERE purpose='observation'")[0]
        (self.state/'runs'/self.rid/artifact['path']).unlink()
        self.assertEqual(self.cli('resume',self.rid).returncode,7)
        self.assertEqual(self.file.read_bytes(),AFTER)
    def test_late_execute_exception_after_effect(self):
        self.create({'max_attempts_per_step':1})
        def hook(point,detail):
            if point=='after_replace':
                raise OSError('lost return')
        self.r=Runtime(self.state,hook=hook)
        self.assertEqual(self.r.start(self.rid)['status'],'completed')
        self.assertEqual(len(self.rows('SELECT * FROM budget_ledger')),3)
        self.assertEqual(self.file.read_bytes(),AFTER)
    def test_UNKNOWN_keeps_recovery_pending(self):
        self.create()
        self.crash('after_replace')
        from evol.tools.files import ReplaceExact
        with patch.object(ReplaceExact,'reconcile',return_value=('UNKNOWN',None)):
            value=self.r.resume(self.rid)
        self.assertEqual(value['status'],'recovering')
        self.assertTrue(value['pending_operations'])
        self.assertEqual(self.file.read_bytes(),AFTER)
        self.assertEqual(self.r.resume(self.rid)['status'],'completed')
    def test_UNKNOWN_after_cancel_stopping(self):
        self.create()
        self.crash('after_replace')
        self.r.cancel(self.rid)
        from evol.tools.files import ReplaceExact
        with patch.object(ReplaceExact,'reconcile',return_value=('UNKNOWN',None)):
            self.assertEqual(self.r.resume(self.rid)['status'],'stopping')
        self.assertEqual(self.r.resume(self.rid)['status'],'cancelled')
    def test_final_file_drift(self):
        self.create()
        def hook(point,detail):
            if point=='result_written':
                self.file.write_bytes(b'external')
        self.r=Runtime(self.state,hook=hook)
        self.assertEqual(self.r.start(self.rid)['status'],'conflict')
        self.assertEqual(self.file.read_bytes(),b'external')
        self.assertEqual(self.rows("SELECT * FROM artifacts WHERE purpose='result'"),[])
    def test_readonly_queries_never_initialize(self):
        self.assertEqual(self.cli('inspect','unknown').returncode,7)
        self.assertFalse(self.state.exists())
    def test_cli_exit_codes(self):
        self.create({'max_dispatches':1})
        self.assertEqual(self.cli('start',self.rid).returncode,6)
        self.assertEqual(self.cli('start',self.rid).returncode,7)
        self.assertEqual(self.cli('resume',self.rid).returncode,6)
        self.assertEqual(self.cli('events',self.rid,'--after-sequence','1').returncode,0)
        self.assertEqual(self.cli('result',self.rid).returncode,7)

    def test_C06_database_busy_bounded(self):
        self.create()
        with closing(sqlite3.connect(self.state/'runtime.sqlite3',isolation_level=None)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self.assertEqual(self.cli('cancel',self.rid).returncode,3)
                self.assertEqual(self.rows('SELECT * FROM control_requests'),[])
            finally:
                db.execute('ROLLBACK')
