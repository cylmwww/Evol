"""任务创建和只读查询服务。"""
import json
import os
from pathlib import Path
import uuid
import time
from .models import VERSION, TOOLS, TERMINAL, validate, canonical, hashed, RuntimeErrorCode
from .workspace import snapshot, safe_path
from .store import Store
from .locks import worker_lock
from . import checkpoint

class Runtime:
    def __init__(self, root, hook=None, clock=None):
        self.root = Path(root).absolute()
        self.hook = hook or (lambda point, detail=None: None)
        self.clock = clock
    def create(self, source, plan, policy=None):
        policy = validate(plan, policy)
        source = Path(source).absolute()
        for name in plan['inputs']:
            safe_path(source, name)
        source = source.resolve(strict=True)
        root = self.root.resolve()
        if source == root or source in root.parents or root in source.parents:
            raise ValueError('状态目录与源目录必须互不包含')
        self.root.mkdir(parents=True, exist_ok=True)
        with worker_lock(self.root):
            store = Store(self.root, create=True)
            try:
                rid = uuid.uuid4().hex
                staging = self.root / 'staging' / rid
                candidate = staging / 'candidate'
                candidate.mkdir(parents=True)
                manifest = snapshot(source, candidate, plan['inputs'], policy, self.hook)
                (staging / 'input-manifest.json').write_text(canonical(manifest), encoding='utf-8')
                (staging / 'plan.json').write_text(canonical(plan), encoding='utf-8')
                (staging / 'artifacts' / 'observations').mkdir(parents=True)
                final = self.root / 'runs' / rid
                final.parent.mkdir(exist_ok=True)
                os.replace(staging, final)
                self.hook('creation_moved', rid)
                now = time.time()
                with store.transaction():
                    store.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (rid,'queued',0,canonical(plan),hashed(plan),canonical(manifest),hashed(manifest),canonical(policy),hashed(policy),VERSION,canonical(TOOLS),now,None,None,now,None,None))
                    for ordinal, s in enumerate(plan['steps']):
                        store.db.execute('INSERT INTO steps VALUES(?,?,?,?,?,?,?,NULL)',
                            (rid,s['id'],ordinal,s['tool'],TOOLS[s['tool']],canonical(s['args']),'pending'))
                    store.event(rid, 'run_created')
                    checkpoint.save(store, rid)
                return self.inspect(rid)
            finally:
                store.close()
    def inspect(self, rid):
        store = Store(self.root, readonly=True)
        try:
            r = store.run(rid)
            data = checkpoint.state(store, rid)
            known = {r['run_id'] for r in store.rows('SELECT run_id FROM runs')}
            return dict(run_id=rid,status=r['status'], stop_reason=r['stop_reason'], resolution_reason=r['resolution_reason'],
                next_step_ordinal=data['next_step_ordinal'], pending_operations=data['pending_operation_ids'], budget=data['budget'],
                result_available=r['status']=='completed', cancellation_pending=bool(store.one('SELECT * FROM control_requests WHERE run_id=? AND applied_at IS NULL',(rid,))),
                last_event=store.one('SELECT * FROM events WHERE run_id=? ORDER BY sequence DESC LIMIT 1',(rid,)),
                orphan_directories=[p.name for p in (self.root/'runs').iterdir() if p.is_dir() and p.name not in known],
                residual_temporary_files=[str(p.relative_to(self.root)) for p in (self.root/'runs'/rid/'candidate').rglob('.evol-*.tmp')])
        finally:
            store.close()
    def events(self, rid, after=0):
        store = Store(self.root, readonly=True)
        try:
            store.run(rid)
            return store.rows('SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence',(rid,after))
        finally:
            store.close()

    def start(self, rid):
        from .scheduler import execute
        return execute(self,rid)
    def resume(self, rid):
        from .scheduler import execute
        return execute(self,rid,resume=True)
    def result(self, rid):
        from .finalizer import result
        return result(self,rid)

    def cancel(self, rid):
        store=Store(self.root)
        try:
            with store.transaction():
                r=store.run(rid)
                if r['status'] in TERMINAL:
                    return {'run_id':rid,'status':r['status'],'already_terminal':True}
                request=store.one('SELECT * FROM control_requests WHERE run_id=?',(rid,))
                if not request:
                    request_id=uuid.uuid4().hex
                    store.db.execute('INSERT INTO control_requests VALUES(?,?,?,?,NULL)',(request_id,rid,'cancel',time.time()))
                    store.event(rid,'cancel_requested',{'request_id':request_id})
                return {'run_id':rid,'status':r['status'],'cancellation_pending':True}
        finally:
            store.close()
