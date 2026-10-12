"""从事实表生成并核对任务内工作状态。"""
import json
from .models import hashed, canonical, RuntimeErrorCode

def state(store, rid):
    r = store.run(rid)
    steps = store.rows('SELECT * FROM steps WHERE run_id=? ORDER BY ordinal', (rid,))
    expected = {x['path']: x['sha256'] for x in json.loads(r['input_manifest_json'])}
    for op in store.rows("SELECT o.* FROM operations o JOIN attempts a USING(attempt_id) JOIN steps s ON s.run_id=a.run_id AND s.step_id=a.step_id WHERE a.run_id=? AND o.status='effect_confirmed' AND o.effect_kind='write' ORDER BY s.ordinal", (rid,)):
        expected[json.loads(op['intent_json'])['path']] = op['expected_sha']
    return dict(schema_version=1, next_step_ordinal=next((s['ordinal'] for s in steps if s['status'] != 'succeeded'), len(steps)),
                completed_step_ids=[s['step_id'] for s in steps if s['status'] == 'succeeded'],
                observation_ids=[s['result_observation_id'] for s in steps if s['result_observation_id']],
                expected_files=expected,
                pending_operation_ids=[o['operation_id'] for o in store.rows("SELECT o.operation_id FROM operations o JOIN attempts a USING(attempt_id) WHERE a.run_id=? AND o.status='prepared' ORDER BY a.rowid", (rid,))],
                budget={'dispatches_reserved': store.db.execute('SELECT count(*) FROM budget_ledger WHERE run_id=?', (rid,)).fetchone()[0]},
                plan_hash=r['plan_hash'], input_manifest_hash=r['input_manifest_hash'], policy_hash=r['policy_hash'])

def save(store, rid):
    seq = store.event(rid, 'checkpoint_saved')
    data = state(store, rid) | {'last_event_sequence': seq}
    n = store.db.execute('SELECT coalesce(max(sequence),0)+1 FROM checkpoints WHERE run_id=?', (rid,)).fetchone()[0]
    store.db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?,?)', (rid, n, seq, 1, canonical(data), hashed(data)))

def verify(store, rid):
    cp = store.one('SELECT * FROM checkpoints WHERE run_id=? ORDER BY sequence DESC LIMIT 1', (rid,))
    if not cp:
        raise RuntimeErrorCode('checkpoint_missing')
    data = json.loads(cp['state_json'])
    if hashed(data) != cp['state_sha'] or data != state(store, rid) | {'last_event_sequence': cp['last_event_sequence']}:
        raise RuntimeErrorCode('checkpoint_integrity_error')
    attempts = store.rows('SELECT * FROM attempts WHERE run_id=?', (rid,))
    ledger = store.rows('SELECT * FROM budget_ledger WHERE run_id=?', (rid,))
    if {a['attempt_id'] for a in attempts} != {a['attempt_id'] for a in ledger}:
        raise RuntimeErrorCode('ledger_integrity_error')
    for a in attempts:
        b = next(b for b in ledger if b['attempt_id'] == a['attempt_id'])
        if b['settled'] != int(a['status'] not in ('prepared','executing')) or (a['status'] == 'executing' and not b['execute_started']):
            raise RuntimeErrorCode('ledger_settlement_error')
    for s in store.rows('SELECT * FROM steps WHERE run_id=?', (rid,)):
        if s['status'] == 'succeeded' and not store.one('SELECT * FROM observations o JOIN attempts a USING(attempt_id) WHERE o.observation_id=? AND a.run_id=? AND a.step_id=? AND a.status=?', (s['result_observation_id'], rid, s['step_id'], 'succeeded')):
            raise RuntimeErrorCode('step_evidence_error')
