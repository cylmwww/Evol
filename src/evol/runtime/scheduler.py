"""顺序派发和原子步骤提交。"""
import json
import sqlite3
import time
import uuid
from .models import VERSION,TOOLS,TERMINAL,canonical,hashed,sha,RuntimeErrorCode
from .store import Store
from .locks import worker_lock
from . import checkpoint, artifacts
from .budget import Budget
from evol.tools.registry import get
from evol.tools.files import Context,ContentConflict,VerificationFailed,StopBeforeEffect

class Scheduler:
    def __init__(self,runtime,store,rid):
        self.runtime,self.store,self.rid=runtime,store,rid
        self.base=runtime.root/'runs'/rid
        self.candidate=self.base/'candidate'
        self.policy=json.loads(store.run(rid)['policy_json'])
        self.budget=Budget(store,rid,runtime.clock)
        self.current_step=None
    def hook(self,point,detail=None):
        self.runtime.hook(point,{'step':self.current_step,'detail':detail})
    def ctx(self):
        refs={o['step_id']:o['expected_sha'] for o in self.store.rows("SELECT a.step_id,o.expected_sha FROM operations o JOIN attempts a USING(attempt_id) WHERE a.run_id=? AND o.status='effect_confirmed' AND o.effect_kind='write'",(self.rid,))}
        return Context(self.candidate,self.policy['max_file_bytes'],checkpoint.state(self.store,self.rid)['expected_files'],refs,lambda:self.budget.reason(),self.hook)
    def compatible(self):
        r=self.store.run(self.rid)
        if r['runtime_version']!=VERSION or json.loads(r['tool_versions_json'])!=TOOLS:
            raise RuntimeErrorCode('version_incompatible')
        for name in ('plan','policy','input_manifest'):
            if hashed(json.loads(r[name+'_json']))!=r[name+'_hash']:
                raise RuntimeErrorCode(name+'_integrity_error')
        plan=json.loads(r['plan_json'])
        rows=self.store.rows('SELECT * FROM steps WHERE run_id=? ORDER BY ordinal',(self.rid,))
        if len(rows)!=len(plan['steps']):
            raise RuntimeErrorCode('step_integrity_error')
        for i,(s,p) in enumerate(zip(rows,plan['steps'])):
            if (s['ordinal'],s['step_id'],s['tool_name'],s['tool_version'],json.loads(s['args_json']))!=(i,p['id'],p['tool'],TOOLS[p['tool']],p['args']):
                raise RuntimeErrorCode('step_integrity_error')
        for o in self.store.rows('SELECT o.*,a.step_id FROM operations o JOIN attempts a USING(attempt_id) WHERE a.run_id=?',(self.rid,)):
            intent=json.loads(o['intent_json'])
            step=next(x for x in rows if x['step_id']==o['step_id'])
            if sha(o['before_blob'])!=o['before_sha'] or sha(o['expected_blob'])!=o['expected_sha'] or intent.get('before_sha')!=o['before_sha'] or intent.get('expected_sha')!=o['expected_sha'] or intent.get('tool')!=step['tool_name'] or intent.get('version')!=step['tool_version'] or intent.get('args_hash')!=hashed(json.loads(step['args_json'])) or intent.get('path')!=json.loads(step['args_json'])['path']:
                raise RuntimeErrorCode('operation_integrity_error')
        artifacts.verify(self.store,self.runtime.root,self.rid)
        checkpoint.verify(self.store,self.rid)
    def check_files(self):
        from .workspace import read
        for name,expected in checkpoint.state(self.store,self.rid)['expected_files'].items():
            try:
                data=read(self.candidate,name,self.policy['max_file_bytes'])
            except (FileNotFoundError,ValueError) as exc:
                raise ContentConflict('missing_or_unsafe_file') from exc
            if sha(data)!=expected:
                raise ContentConflict('file_drift')
    def stopping(self,reason):
        with self.store.transaction():
            r=self.store.run(self.rid)
            if r['status'] in TERMINAL:
                return
            if not r['stop_reason']:
                self.store.db.execute('UPDATE runs SET stop_reason=? WHERE run_id=?',(reason,self.rid))
                self.store.event(self.rid,'stop_requested',{'reason':reason})
            if r['status']!='stopping':
                self.store.transition(self.rid,'stopping')
            checkpoint.save(self.store,self.rid)
    def stopped(self,resolution=None):
        r=self.store.run(self.rid)
        if r['status']!='stopping':
            raise RuntimeErrorCode('stop_protocol_error')
        if checkpoint.state(self.store,self.rid)['pending_operation_ids']:
            return False
        reason=r['stop_reason']
        status='conflict' if resolution=='conflict' else 'cancelled' if reason=='cancel_requested' else 'budget_exhausted' if reason in ('dispatch_budget','deadline_exceeded','clock_regression') else 'failed'
        with self.store.transaction():
            self.store.db.execute("UPDATE steps SET status=? WHERE run_id=? AND status IN ('pending','active')",('skipped' if status in ('cancelled','budget_exhausted') else 'failed',self.rid))
            self.store.db.execute('UPDATE control_requests SET applied_at=? WHERE run_id=?',(self.budget.clock.utc(),self.rid))
            self.store.transition(self.rid,status,resolution or reason)
            self.store.event(self.rid,'run_stopped',{'status':status})
            checkpoint.save(self.store,self.rid)
        return True
    def complete_operation(self,op,actual,source):
        aid,oid=op['attempt_id'],op['operation_id']
        a=self.store.one('SELECT * FROM attempts WHERE attempt_id=?',(aid,))
        obs=uuid.uuid4().hex
        rel='artifacts/observations/'+obs+'.bin'
        digest=artifacts.write(self.base/rel,actual)
        self.hook('observation_written')
        with self.store.transaction():
            self.budget.observe()
            self.store.db.execute("UPDATE operations SET status='effect_confirmed' WHERE operation_id=? AND status='prepared'",(oid,))
            self.store.db.execute("UPDATE attempts SET status='succeeded',ended_at=? WHERE attempt_id=?",(self.budget.clock.utc(),aid))
            self.store.db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)',(obs,self.rid,rel,digest,len(actual),'observation','evidence'))
            self.store.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',(obs,aid,source,digest,obs,self.budget.clock.utc()))
            self.store.db.execute("UPDATE steps SET status='succeeded',result_observation_id=? WHERE run_id=? AND step_id=?",(obs,self.rid,a['step_id']))
            self.store.db.execute('UPDATE budget_ledger SET settled=1,outcome=? WHERE attempt_id=?',('succeeded',aid))
            self.store.event(self.rid,'recovery_confirmed' if source=='recovery' else 'execution_confirmed',{'sha256':digest},a['step_id'],aid,oid)
            self.store.event(self.rid,'step_succeeded',step=a['step_id'],attempt=aid,operation=oid)
            checkpoint.save(self.store,self.rid)
        self.hook('after_step_commit')
    def not_applied(self,op,error=None):
        a=self.store.one('SELECT * FROM attempts WHERE attempt_id=?',(op['attempt_id'],))
        with self.store.transaction():
            self.store.db.execute("UPDATE operations SET status='not_applied' WHERE operation_id=?",(op['operation_id'],))
            self.store.db.execute("UPDATE attempts SET status='aborted',error_code=?,ended_at=? WHERE attempt_id=?",(error,self.budget.clock.utc(),op['attempt_id']))
            self.store.db.execute("UPDATE steps SET status='pending' WHERE run_id=? AND step_id=?",(self.rid,a['step_id']))
            self.store.db.execute("UPDATE budget_ledger SET settled=1,outcome='not_applied' WHERE attempt_id=?",(op['attempt_id'],))
            self.store.event(self.rid,'operation_not_applied',{'error':error},a['step_id'],op['attempt_id'],op['operation_id'])
            checkpoint.save(self.store,self.rid)
    def conflict(self,op=None,reason='file_drift'):
        self.stopping(self.budget.reason() or reason)
        if op:
            with self.store.transaction():
                self.store.db.execute("UPDATE operations SET status='conflict' WHERE operation_id=?",(op['operation_id'],))
                self.store.db.execute("UPDATE attempts SET status='conflict',ended_at=? WHERE attempt_id=?",(self.budget.clock.utc(),op['attempt_id']))
                self.store.db.execute("UPDATE budget_ledger SET settled=1,outcome='conflict' WHERE attempt_id=?",(op['attempt_id'],))
                self.store.event(self.rid,'conflict_detected',{'reason':reason},operation=op['operation_id'])
                checkpoint.save(self.store,self.rid)
        else:
            with self.store.transaction():
                self.store.event(self.rid,'conflict_detected',{'reason':reason})
        self.stopped('conflict')
    def prepare(self,step):
        self.current_step=step['step_id']
        self.check_files()
        intent,before,expected=get(step['tool_name']).prepare(self.ctx(),json.loads(step['args_json']))
        aid,oid=uuid.uuid4().hex,uuid.uuid4().hex
        with self.store.transaction():
            reason=self.budget.reason(dispatch=True,step=step['step_id'])
            if reason:
                return None,reason
            self.budget.observe()
            n=self.store.db.execute('SELECT count(*)+1 FROM attempts WHERE run_id=? AND step_id=?',(self.rid,step['step_id'])).fetchone()[0]
            self.store.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,NULL)',(aid,self.rid,step['step_id'],n,'prepared',None,self.budget.clock.utc()))
            self.store.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)',(oid,aid,canonical(intent),before,expected,sha(before),sha(expected),'prepared',intent['effect_kind']))
            self.store.db.execute('INSERT INTO budget_ledger VALUES(?,?,1,0,0,NULL)',(self.rid,aid))
            self.store.db.execute("UPDATE steps SET status='active' WHERE run_id=? AND step_id=?",(self.rid,step['step_id']))
            self.store.event(self.rid,'step_started',step=step['step_id'],attempt=aid)
            self.store.event(self.rid,'operation_prepared',{'before_sha':sha(before),'expected_sha':sha(expected)},step['step_id'],aid,oid)
            checkpoint.save(self.store,self.rid)
        self.hook('after_intent')
        return self.store.one('SELECT * FROM operations WHERE operation_id=?',(oid,)),None
    def dispatch(self,op):
        with self.store.transaction():
            reason=self.budget.reason()
            if reason:
                return reason
            self.budget.observe()
            self.store.db.execute("UPDATE attempts SET status='executing' WHERE attempt_id=? AND status='prepared'",(op['attempt_id'],))
            self.store.db.execute('UPDATE budget_ledger SET execute_started=1 WHERE attempt_id=?',(op['attempt_id'],))
            self.store.event(self.rid,'execute_started',step=self.current_step,attempt=op['attempt_id'],operation=op['operation_id'])
        self.hook('after_dispatch')
        return None
    def loop(self):
        while True:
            reason=self.budget.reason()
            if reason:
                self.stopping(reason)
                self.stopped()
                return
            step=self.store.one("SELECT * FROM steps WHERE run_id=? AND status='pending' ORDER BY ordinal LIMIT 1",(self.rid,))
            if not step:
                from .finalizer import finalize
                finalize(self)
                return
            reason=self.budget.reason(dispatch=True,step=step['step_id'])
            if reason:
                self.stopping(reason)
                self.stopped()
                return
            try:
                op,reason=self.prepare(step)
            except ContentConflict as exc:
                self.conflict(reason=str(exc))
                return
            except (ValueError,VerificationFailed) as exc:
                self.stopping(str(exc))
                self.stopped()
                return
            except OSError as exc:
                self.record_prepare_failure(step,exc)
                if not self.retry_wait(step['step_id']):
                    return
                continue
            if reason:
                self.stopping(reason)
                self.stopped()
                return
            reason=self.dispatch(op)
            if reason:
                self.stopping(reason)
                self.not_applied(op)
                self.stopped()
                return
            try:
                actual=get(step['tool_name']).execute(self.ctx(),op)
            except (OSError,ContentConflict,StopBeforeEffect) as exc:
                from .recovery import reconcile_one
                if not reconcile_one(self,op,error=type(exc).__name__):
                    return
                if self.store.run(self.rid)['status'] in TERMINAL or self.store.run(self.rid)['status']=='stopping':
                    return
                if self.store.one('SELECT status FROM steps WHERE run_id=? AND step_id=?',(self.rid,step['step_id']))['status']!='succeeded' and not self.retry_wait(step['step_id']):
                    return
                continue
            self.complete_operation(op,actual,'execution')
    def record_prepare_failure(self,step,exc):
        # 无副作用的临时读取失败也计入有界尝试及预算。
        with self.store.transaction():
            reason=self.budget.reason(dispatch=True,step=step['step_id'])
            if reason:
                return
            aid=uuid.uuid4().hex
            n=self.store.db.execute('SELECT count(*)+1 FROM attempts WHERE run_id=? AND step_id=?',(self.rid,step['step_id'])).fetchone()[0]
            now=self.budget.clock.utc()
            self.store.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)',(aid,self.rid,step['step_id'],n,'failed',type(exc).__name__,now,now))
            self.store.db.execute('INSERT INTO budget_ledger VALUES(?,?,1,0,1,?)',(self.rid,aid,'prepare_io_error'))
            self.store.event(self.rid,'prepare_failed',{'error_code':type(exc).__name__},step['step_id'],aid)
            checkpoint.save(self.store,self.rid)
    def retry_wait(self,step):
        reason=self.budget.reason(dispatch=True,step=step)
        if reason:
            self.stopping(reason)
            self.stopped()
            return False
        with self.store.transaction():
            self.store.event(self.rid,'retry_scheduled',{'reason':'not_applied_or_temporary_io'},step=step)
        self.hook('retry_wait')
        until=time.monotonic()+self.policy['retry_delay_ms']/1000
        while time.monotonic()<until:
            reason=self.budget.reason()
            if reason:
                self.stopping(reason)
                self.stopped()
                return False
            time.sleep(min(.1,max(0,until-time.monotonic())))
        return True

def execute(runtime,rid,resume=False):
    with worker_lock(runtime.root):
        store=Store(runtime.root)
        try:
            scheduler=Scheduler(runtime,store,rid)
            r=store.run(rid)
            if r['status'] in TERMINAL:
                if not resume:
                    raise RuntimeErrorCode('start_requires_queued')
                return runtime.inspect(rid)
            if not resume and r['status']!='queued':
                raise RuntimeErrorCode('start_requires_queued')
            scheduler.compatible()
            if r['status']=='queued':
                with store.transaction():
                    reason=scheduler.budget.reason()
                    if not reason:
                        now=scheduler.budget.clock.utc()
                        store.transition(rid,'running')
                        store.db.execute('UPDATE runs SET started_at=?,deadline_at=?,last_observed_utc=? WHERE run_id=?',(now,now+scheduler.policy['max_wall_seconds'],now,rid))
                        store.event(rid,'run_started')
                    checkpoint.save(store,rid)
            else:
                from .recovery import recover
                if not recover(scheduler):
                    return runtime.inspect(rid)
            try:
                scheduler.loop()
            except ContentConflict as exc:
                scheduler.conflict(reason=str(exc))
            except OSError:
                # 暂时无法核对，不伪造终态；保留待协调状态。
                if store.run(rid)['status'] not in ('recovering','stopping'):
                    with store.transaction():
                        store.transition(rid,'recovering')
                        checkpoint.save(store,rid)
            return runtime.inspect(rid)
        finally:
            store.close()
