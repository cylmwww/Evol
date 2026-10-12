from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from common import Case,ROOT,BEFORE,AFTER
from evol.runtime.locks import worker_lock
from evol.runtime.models import RuntimeErrorCode
from evol.runtime.store import Store

class RecoveryTests(Case):
    def verify(self,point,applied=False):
        self.create()
        self.crash(point)
        self.assertEqual(self.file.read_bytes(),AFTER if applied else BEFORE)
        before=self.rows('SELECT * FROM budget_ledger')
        # 替换次数的外部证据：APPLIED 恢复时拦截 os.replace。
        from unittest.mock import patch
        calls=[]
        real=os.replace
        def counted(a,b):
            if Path(b)==self.file:
                calls.append(str(b))
            return real(a,b)
        with patch('os.replace',side_effect=counted):
            result=self.r.resume(self.rid)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.file.read_bytes(),AFTER)
        self.assertEqual(len(calls),0 if applied else 1)
        self.assertEqual(result['budget']['dispatches_reserved'],len(before)+(1 if applied else 2))
        self.assert_source()
    def test_R01_after_intent(self):
        self.verify('after_intent')
    def test_R02_temp_before_replace(self):
        self.create()
        self.crash('temp_written')
        self.assertEqual(self.file.read_bytes(),BEFORE)
        pending=self.r.inspect(self.rid)['pending_operations'][0]
        self.assertTrue((self.candidate/('.evol-'+pending+'.tmp')).exists())
        self.assertEqual(self.r.resume(self.rid)['status'],'completed')
        self.assertEqual(self.file.read_bytes(),AFTER)
        self.assertTrue(self.r.inspect(self.rid)['residual_temporary_files'])
    def test_R03_after_replace(self):
        self.verify('after_replace',True)
        self.assertIn('recovery_confirmed',[e['kind'] for e in self.r.events(self.rid)])
    def test_R04_after_step_commit(self):
        self.verify('after_step_commit',True)
    def test_R05_second_file(self):
        (self.source/'second.txt').write_bytes(BEFORE)
        self.plan['inputs'].append('second.txt')
        self.plan['steps'].insert(2,{'id':'second','tool':'replace_exact','args':{'path':'second.txt','old_utf8':'30','new_utf8':'60'}})
        self.create()
        self.crash('after_replace','second')
        self.assertEqual(self.file.read_bytes(),AFTER)
        self.assertEqual((self.candidate/'second.txt').read_bytes(),AFTER)
        self.assertEqual(self.r.resume(self.rid)['status'],'completed')
        self.assertEqual(len(self.rows("SELECT * FROM attempts WHERE step_id='change'")),1)
        self.assertEqual((self.source/'second.txt').read_bytes(),BEFORE)
    def test_R06_missing_or_modified(self):
        self.create()
        self.crash('after_intent')
        self.file.unlink()
        self.assertEqual(self.r.resume(self.rid)['status'],'conflict')
        self.assertFalse(self.file.exists())
        self.assert_source()
    def test_R06_external_change(self):
        self.create()
        self.crash('after_replace')
        self.file.write_bytes(b'external')
        self.assertEqual(self.r.resume(self.rid)['status'],'conflict')
        self.assertEqual(self.file.read_bytes(),b'external')
    def test_R07_completed_file_drift(self):
        self.create()
        self.crash('after_step_commit')
        self.file.write_bytes(b'external')
        self.assertEqual(self.r.resume(self.rid)['status'],'conflict')
        self.assertEqual(self.file.read_bytes(),b'external')
    def test_R08_repeat_resume(self):
        self.create()
        self.r.start(self.rid)
        before=self.r.events(self.rid),self.rows('SELECT * FROM budget_ledger'),self.file.stat().st_mtime_ns
        self.assertEqual(self.r.resume(self.rid)['status'],'completed')
        self.assertEqual((self.r.events(self.rid),self.rows('SELECT * FROM budget_ledger'),self.file.stat().st_mtime_ns),before)
    def test_R09_second_worker(self):
        self.create()
        with worker_lock(self.state):
            result=self.cli('start',self.rid)
        self.assertEqual(result.returncode,3,result.stderr)
        self.assertEqual(self.file.read_bytes(),BEFORE)
        self.assertEqual(self.rows('SELECT * FROM attempts'),[])
    def test_R10_versions(self):
        self.create()
        s=Store(self.state)
        try:
            with s.transaction():
                s.db.execute('DROP TRIGGER immutable_run')
                s.db.execute("UPDATE runs SET tool_versions_json='{}' WHERE run_id=?",(self.rid,))
        finally:
            s.close()
        self.assertEqual(self.cli('resume',self.rid).returncode,7)
        self.assertEqual(self.file.read_bytes(),BEFORE)
    def test_R10_schema(self):
        self.create()
        with closing(sqlite3.connect(self.state/'runtime.sqlite3',isolation_level=None)) as db:
            db.execute('UPDATE schema_meta SET schema_version=2')
        self.assertEqual(self.cli('resume',self.rid).returncode,7)
        self.assertEqual(self.file.read_bytes(),BEFORE)
    def test_R10_plan_digest(self):
        self.create()
        with closing(sqlite3.connect(self.state/'runtime.sqlite3',isolation_level=None)) as db:
            db.execute('DROP TRIGGER immutable_run')
            db.execute("UPDATE runs SET plan_hash='tampered'")
        self.assertEqual(self.cli('resume',self.rid).returncode,7)
        self.assertEqual(self.file.read_bytes(),BEFORE)
