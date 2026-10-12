"""重新核对证据及文件；完成事务是唯一发布门禁。"""
import json
import uuid
from .models import canonical,RuntimeErrorCode,TERMINAL
from . import checkpoint,artifacts

def finalize(s):
    s.current_step=None
    if s.store.run(s.rid)['status']!='validating':
        with s.store.transaction():
            s.store.transition(s.rid,'validating')
            s.store.event(s.rid,'validation_started')
    s.hook('validation_started')
    s.compatible()
    s.check_files()
    steps=s.store.rows('SELECT * FROM steps WHERE run_id=? ORDER BY ordinal',(s.rid,))
    if any(step['status']!='succeeded' for step in steps) or checkpoint.state(s.store,s.rid)['pending_operation_ids']:
        raise RuntimeErrorCode('delivery_evidence_missing')
    reason=s.budget.reason()
    if reason:
        s.stopping(reason)
        s.stopped()
        return
    r=s.store.run(s.rid)
    data=dict(run_id=s.rid,input_manifest_hash=r['input_manifest_hash'],plan_hash=r['plan_hash'],policy_hash=r['policy_hash'],
              runtime_version=r['runtime_version'],tool_versions=json.loads(r['tool_versions_json']),steps=steps,
              files=checkpoint.state(s.store,s.rid)['expected_files'],budget=checkpoint.state(s.store,s.rid)['budget'],
              verification_scope='固定计划与声明文件完整内容；不判断业务修复正确性')
    raw=canonical(data).encode()
    digest=artifacts.write(s.base/'artifacts/result.json',raw)
    s.hook('result_written')
    # 核对发生在完成事务内部；取消与发布按提交顺序竞争。
    with s.store.transaction():
        reason=s.budget.reason()
        if not reason:
            s.compatible()
            s.check_files()
            # 文件与证据核验可能跨过截止时间，发布前必须重新准入。
            reason=s.budget.reason()
        if not reason:
            s.budget.observe()
            s.store.db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,s.rid,'artifacts/result.json',digest,len(raw),'result','published'))
            s.store.transition(s.rid,'completed')
            s.store.event(s.rid,'run_completed',{'sha256':digest})
            checkpoint.save(s.store,s.rid)
    if reason:
        s.stopping(reason)
        s.stopped()
    else:
        s.hook('completed_committed')

def result(runtime,rid):
    from .store import Store
    store=Store(runtime.root,readonly=True)
    try:
        if store.run(rid)['status']!='completed':
            raise RuntimeErrorCode('result_not_published')
        row=store.one("SELECT * FROM artifacts WHERE run_id=? AND purpose='result' AND publication_state='published'",(rid,))
        if not row:
            raise RuntimeErrorCode('artifact_integrity_error')
        artifacts.verify(store,runtime.root,rid)
        return json.loads((runtime.root/'runs'/rid/row['path']).read_text(encoding='utf-8'))
    finally:
        store.close()
