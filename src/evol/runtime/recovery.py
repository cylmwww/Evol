"""先核对外部效果，再决定是否允许新的尝试。"""
from .models import TERMINAL,RuntimeErrorCode,sha,hashed
from . import checkpoint
from evol.tools.registry import get
from evol.tools.files import ContentConflict
import json

def reconcile_one(s,op,error=None):
    a=s.store.one('SELECT * FROM attempts WHERE attempt_id=?',(op['attempt_id'],))
    step=s.store.one('SELECT * FROM steps WHERE run_id=? AND step_id=?',(s.rid,a['step_id']))
    s.current_step=a['step_id']
    decision,actual=get(step['tool_name']).reconcile(s.ctx(),op)
    reason=s.budget.reason()
    if reason:
        s.stopping(reason)
    if decision=='UNKNOWN':
        if s.store.run(s.rid)['status'] not in ('stopping','recovering'):
            with s.store.transaction():
                s.store.transition(s.rid,'recovering')
                checkpoint.save(s.store,s.rid)
        return False
    if decision=='CONFLICT':
        s.conflict(op,'operation_conflict')
        return False
    if decision=='APPLIED':
        s.complete_operation(op,actual,'recovery')
    else:
        s.not_applied(op,error)
    try:
        s.check_files()
    except ContentConflict as exc:
        s.conflict(reason=str(exc))
        return False
    except OSError:
        if s.store.run(s.rid)['status'] not in ('stopping','recovering'):
            with s.store.transaction():
                s.store.transition(s.rid,'recovering')
        return False
    if s.store.run(s.rid)['status']=='stopping':
        s.stopped()
        return False
    return True

def recover(s):
    if s.store.run(s.rid)['status']!='stopping':
        with s.store.transaction():
            if s.store.run(s.rid)['status']!='recovering':
                s.store.transition(s.rid,'recovering')
            s.store.event(s.rid,'recovery_started')
    ops=s.store.rows("SELECT o.* FROM operations o JOIN attempts a USING(attempt_id) WHERE a.run_id=? AND o.status='prepared'",(s.rid,))
    if len(ops)>1:
        raise RuntimeErrorCode('multiple_pending_operations')
    reason=s.budget.reason()
    if reason:
        s.stopping(reason)
    if ops and not reconcile_one(s,ops[0]):
        return False
    try:
        s.check_files()
    except ContentConflict as exc:
        s.conflict(reason=str(exc))
        return False
    except OSError:
        return False
    checkpoint.verify(s.store,s.rid)
    if s.store.run(s.rid)['status']=='stopping':
        s.stopped()
        return False
    with s.store.transaction():
        s.store.transition(s.rid,'running')
        checkpoint.save(s.store,s.rid)
    return True
