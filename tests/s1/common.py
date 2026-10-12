"""S1 测试共享夹具；每例源与状态互不包含。"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from evol.runtime.service import Runtime
from evol.runtime.store import Store

ROOT = Path(__file__).resolve().parents[2]
BEFORE = b'timeout=30\r\nother=keep\r\n'
AFTER = BEFORE.replace(b'30', b'60')
PLAN = {'schema_version':1,'name':'demo','inputs':['config.txt'],'steps':[
 {'id':'read','tool':'read_file','args':{'path':'config.txt'}},
 {'id':'change','tool':'replace_exact','args':{'path':'config.txt','old_utf8':'30','new_utf8':'60'}},
 {'id':'verify','tool':'assert_file_sha256','args':{'path':'config.txt','expected_from':'change'}}]}
def cleanup(base):
    # 删除前确认精确目标始终位于候选工作区测试工件目录内。
    base=base.absolute()
    if not base.is_relative_to((ROOT/'docs/local/artifacts/s1').absolute()):
        raise ValueError('测试清理越界')
    target=str(base)
    if os.name=='nt':
        target='\\\\?\\'+target
    shutil.rmtree(target)

class Case(unittest.TestCase):
    def setUp(self):
        self.base = ROOT/'docs/local/artifacts/s1'/uuid.uuid4().hex
        self.source = self.base/'source'
        self.source.mkdir(parents=True)
        (self.source/'config.txt').write_bytes(BEFORE)
        self.state = self.base/'state'
        self.r = Runtime(self.state)
        self.plan = copy.deepcopy(PLAN)
        self.addCleanup(cleanup,self.base)
    def create(self, policy=None):
        self.rid = self.r.create(self.source,self.plan,policy)['run_id']
        self.candidate = self.state/'runs'/self.rid/'candidate'
        self.file = self.candidate/'config.txt'
        return self.rid
    def rows(self, sql, args=()):
        s = Store(self.state,readonly=True)
        try:
            return s.rows(sql,args)
        finally:
            s.close()
    def cli(self, command, *args):
        return subprocess.run([sys.executable,'-m','evol','--state-root',str(self.state),command,*args],capture_output=True,timeout=20)
    def crash(self, point, step='change', action='crash'):
        result = subprocess.run([sys.executable,str(ROOT/'tests/s1/crash_driver.py'),str(self.state),self.rid,point,step,action],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,77,result.stderr)
    def assert_source(self):
        self.assertEqual((self.source/'config.txt').read_bytes(),BEFORE)
