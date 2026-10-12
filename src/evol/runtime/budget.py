"""持久化派发预算与 UTC/单调时钟截止时间。"""
import time
import json

class Clock:
    def utc(self):
        return time.time()
    def monotonic(self):
        return time.monotonic()

class Budget:
    def __init__(self, store, rid, clock=None):
        self.store,self.rid,self.clock=store,rid,clock or Clock()
        self.start_utc=self.clock.utc()
        self.start_mono=self.clock.monotonic()
    def reason(self, dispatch=False, step=None):
        r=self.store.run(self.rid)
        if r['stop_reason']:
            return r['stop_reason']
        if self.store.one('SELECT * FROM control_requests WHERE run_id=?',(self.rid,)):
            return 'cancel_requested'
        now=self.clock.utc()
        if now+1 < r['last_observed_utc'] or now+1 < self.start_utc+(self.clock.monotonic()-self.start_mono):
            return 'clock_regression'
        if r['deadline_at'] is not None and (now>=r['deadline_at'] or self.start_utc+self.clock.monotonic()-self.start_mono>=r['deadline_at']):
            return 'deadline_exceeded'
        p=json.loads(r['policy_json'])
        if dispatch:
            n=self.store.db.execute('SELECT count(*) FROM budget_ledger WHERE run_id=?',(self.rid,)).fetchone()[0]
            if n>=p['max_dispatches']:
                return 'dispatch_budget'
            if step:
                n=self.store.db.execute('SELECT count(*) FROM attempts WHERE run_id=? AND step_id=?',(self.rid,step)).fetchone()[0]
                if n>=p['max_attempts_per_step']:
                    return 'attempts_exhausted'
        return None
    def observe(self):
        # 仅在已持有短事务时调用，不将时间回退覆盖为新的基线。
        self.store.db.execute('UPDATE runs SET last_observed_utc=max(last_observed_utc,?) WHERE run_id=?',(self.clock.utc(),self.rid))
