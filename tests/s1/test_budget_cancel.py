import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
from common import Case,ROOT,BEFORE,AFTER
from evol.runtime.service import Runtime
from evol.runtime.models import RuntimeErrorCode
from evol.runtime.budget import Clock

class FakeClock:
    def __init__(self,utc):
        self.now=utc
        self.mono=100.0
    def utc(self):
        return self.now
    def monotonic(self):
        return self.mono

class ControlTests(Case):
    def unavailable(self):
        with self.assertRaises(RuntimeErrorCode):
            self.r.result(self.rid)
        self.assertFalse(self.r.inspect(self.rid)['result_available'])
    def test_C01_queued_cancel(self):
        self.create()
        self.r.cancel(self.rid)
        self.r.cancel(self.rid)
        self.assertEqual(self.r.resume(self.rid)['status'],'cancelled')
        self.assertEqual(self.rows('SELECT * FROM attempts'),[])
        self.assertEqual(self.file.read_bytes(),BEFORE)
        self.assertEqual(len(self.rows('SELECT * FROM control_requests')),1)
        self.unavailable()
    def test_C02_after_T1_cancel(self):
        self.create()
        def hook(point,detail):
            if point=='after_intent' and detail['step']=='change':
                self.r.cancel(self.rid)
        self.r=Runtime(self.state,hook=hook)
        self.assertEqual(self.r.start(self.rid)['status'],'cancelled')
        a=self.rows("SELECT * FROM budget_ledger JOIN attempts USING(attempt_id) WHERE step_id='change'")[0]
        self.assertEqual(a['execute_started'],0)
        self.assertEqual(a['status'],'aborted')
        self.assertEqual(self.file.read_bytes(),BEFORE)
        self.unavailable()
    def test_C03_dispatched_cancel_effect_completed(self):
        self.create()
        def hook(point,detail):
            if point=='before_replace':
                self.r.cancel(self.rid)
        self.r=Runtime(self.state,hook=hook)
        self.assertEqual(self.r.start(self.rid)['status'],'cancelled')
        self.assertEqual(self.file.read_bytes(),AFTER)
        self.assertEqual(self.rows("SELECT * FROM attempts WHERE step_id='verify'"),[])
        self.assertEqual(self.rows("SELECT * FROM operations WHERE effect_kind='write'")[0]['status'],'effect_confirmed')
        self.unavailable()
    def test_C03_cancel_before_effect(self):
        self.create()
        def hook(point,detail):
            if point=='temp_written':
                self.r.cancel(self.rid)
        self.r=Runtime(self.state,hook=hook)
        self.assertEqual(self.r.start(self.rid)['status'],'cancelled')
        self.assertEqual(self.file.read_bytes(),BEFORE)
        self.unavailable()
    def test_C04_dead_worker_cancel(self):
        self.create()
        self.crash('after_intent')
        self.assertTrue(self.r.cancel(self.rid)['cancellation_pending'])
        self.assertEqual(self.r.resume(self.rid)['status'],'cancelled')
        self.assertEqual(self.file.read_bytes(),BEFORE)
        self.assertEqual(len(self.rows('SELECT * FROM attempts')),2)
        self.unavailable()
    def test_C04_applied_then_cancel(self):
        self.create()
        self.crash('after_replace')
        self.r.cancel(self.rid)
        self.assertEqual(self.r.resume(self.rid)['status'],'cancelled')
        self.assertEqual(self.file.read_bytes(),AFTER)
        self.assertEqual(len(self.rows('SELECT * FROM attempts')),2)
        self.unavailable()
    def test_C05_cancel_first_delivery(self):
        self.create()
        def hook(point,detail):
            if point=='result_written':
                self.r.cancel(self.rid)
        self.r=Runtime(self.state,hook=hook)
        self.assertEqual(self.r.start(self.rid)['status'],'cancelled')
        self.assertTrue((self.state/'runs'/self.rid/'artifacts/result.json').exists())
        self.assertEqual(self.rows("SELECT * FROM artifacts WHERE purpose='result'"),[])
        before=self.r.events(self.rid)
        self.assertEqual(self.r.resume(self.rid)['status'],'cancelled')
        self.assertEqual(self.r.events(self.rid),before)
        self.unavailable()
    def test_C05_completion_first(self):
        self.create()
        self.r.start(self.rid)
        self.assertTrue(self.r.cancel(self.rid)['already_terminal'])
        self.assertEqual(self.rows('SELECT * FROM control_requests'),[])
        self.assertEqual(self.r.result(self.rid)['run_id'],self.rid)
    def test_C06_concurrent_controls(self):
        self.create()
        proc=subprocess.Popen([sys.executable,str(ROOT/'tests/s1/crash_driver.py'),str(self.state),self.rid,'after_intent','change','wait'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        barrier=self.state/'test-barrier'
        deadline=time.monotonic()+10
        try:
            while not barrier.exists() and time.monotonic()<deadline:
                self.assertIsNone(proc.poll())
                time.sleep(.01)
            self.assertTrue(barrier.exists())
            start=time.monotonic()
            self.assertEqual(self.cli('inspect',self.rid).returncode,0)
            self.assertEqual(self.cli('cancel',self.rid).returncode,0)
            self.assertLess(time.monotonic()-start,5)
            barrier.with_suffix('.release').write_text('release')
            out,err=proc.communicate(timeout=10)
            self.assertEqual(proc.returncode,5,err)
            self.assertEqual(self.file.read_bytes(),BEFORE)
            self.unavailable()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    def test_B01_exact_dispatches(self):
        self.create({'max_dispatches':3})
        self.assertEqual(self.r.start(self.rid)['status'],'completed')
        self.assertEqual(len(self.rows('SELECT * FROM budget_ledger')),3)
    def test_B02_no_dispatch_balance(self):
        self.create({'max_dispatches':2})
        self.assertEqual(self.r.start(self.rid)['status'],'budget_exhausted')
        self.assertEqual(len(self.rows('SELECT * FROM budget_ledger')),2)
        self.assertEqual(self.rows("SELECT * FROM attempts WHERE step_id='verify'"),[])
        self.unavailable()
    def test_B03_crash_reservation_preserved(self):
        self.create({'max_dispatches':2})
        self.crash('after_intent')
        self.assertEqual(self.r.resume(self.rid)['status'],'budget_exhausted')
        self.assertEqual(len(self.rows('SELECT * FROM budget_ledger')),2)
        self.assertEqual(self.file.read_bytes(),BEFORE)
        self.unavailable()
    def test_B04_deadline_offline(self):
        self.create()
        self.crash('after_intent')
        deadline=self.rows('SELECT deadline_at FROM runs')[0]['deadline_at']
        self.r=Runtime(self.state,clock=FakeClock(deadline+1))
        self.assertEqual(self.r.resume(self.rid)['status'],'budget_exhausted')
        self.assertEqual(self.file.read_bytes(),BEFORE)
        self.assertEqual(len(self.rows('SELECT * FROM attempts')),2)
        self.unavailable()
    def test_B04_applied_deadline(self):
        self.create()
        self.crash('after_replace')
        deadline=self.rows('SELECT deadline_at FROM runs')[0]['deadline_at']
        self.r=Runtime(self.state,clock=FakeClock(deadline+1))
        self.assertEqual(self.r.resume(self.rid)['status'],'budget_exhausted')
        self.assertEqual(self.file.read_bytes(),AFTER)
        self.assertEqual(len(self.rows('SELECT * FROM attempts')),2)
        self.unavailable()
    def test_B05_temporary_prepare_error(self):
        self.create()
        from evol.runtime import scheduler
        real=scheduler.Scheduler.check_files
        calls=[]
        def flaky(s):
            calls.append(1)
            if len(calls)==1:
                raise PermissionError('test transient')
            return real(s)
        with patch.object(scheduler.Scheduler,'check_files',flaky):
            self.assertEqual(self.r.start(self.rid)['status'],'completed')
        self.assertEqual(len(self.rows('SELECT * FROM budget_ledger')),4)
        self.assertEqual([a['attempt_no'] for a in self.rows("SELECT * FROM attempts WHERE step_id='read' ORDER BY attempt_no")],[1,2])
        self.assertIn('retry_scheduled',[e['kind'] for e in self.r.events(self.rid)])
    def test_B05_deterministic_error(self):
        self.plan['steps'][1]['args']['old_utf8']='absent'
        self.create()
        self.assertEqual(self.r.start(self.rid)['status'],'failed')
        self.assertEqual(len(self.rows('SELECT * FROM attempts')),1)
        self.assertEqual(self.file.read_bytes(),BEFORE)
        self.unavailable()
    def test_B05_retry_exhaustion(self):
        self.create()
        from evol.runtime.scheduler import Scheduler
        with patch.object(Scheduler,'check_files',side_effect=PermissionError('temporary')):
            self.assertEqual(self.r.start(self.rid)['status'],'failed')
        self.assertEqual(len(self.rows('SELECT * FROM attempts')),2)
        self.unavailable()
    def test_B06_clock_regression(self):
        self.create()
        self.crash('after_intent')
        observed=self.rows('SELECT last_observed_utc FROM runs')[0]['last_observed_utc']
        self.r=Runtime(self.state,clock=FakeClock(observed-10))
        self.assertEqual(self.r.resume(self.rid)['status'],'budget_exhausted')
        self.assertEqual(self.rows('SELECT stop_reason FROM runs')[0]['stop_reason'],'clock_regression')
        self.assertEqual(self.file.read_bytes(),BEFORE)
        self.unavailable()
    def test_cancel_conflict_preserves_first_reason(self):
        self.create()
        self.crash('after_replace')
        self.r.cancel(self.rid)
        self.file.write_bytes(b'external')
        self.assertEqual(self.r.resume(self.rid)['status'],'conflict')
        row=self.rows('SELECT * FROM runs')[0]
        self.assertEqual(row['stop_reason'],'cancel_requested')
        self.assertEqual(row['resolution_reason'],'conflict')
        self.assertEqual(self.file.read_bytes(),b'external')
        self.unavailable()
