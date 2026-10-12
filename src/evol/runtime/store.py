"""SQLite 真源；所有状态事务短小且使用 CAS。"""
from contextlib import contextmanager
import json
import sqlite3
import time
import uuid
from .models import TRANSITIONS, RuntimeErrorCode, canonical

SCHEMA = '''
CREATE TABLE schema_meta(schema_version INTEGER NOT NULL CHECK(schema_version > 0));
INSERT INTO schema_meta VALUES(1);
CREATE TABLE runs(
 run_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('queued','running','validating','recovering','stopping','completed','cancelled','budget_exhausted','failed','conflict')),
 state_version INTEGER NOT NULL CHECK(state_version>=0), plan_json TEXT NOT NULL, plan_hash TEXT NOT NULL,
 input_manifest_json TEXT NOT NULL, input_manifest_hash TEXT NOT NULL, policy_json TEXT NOT NULL, policy_hash TEXT NOT NULL,
 runtime_version TEXT NOT NULL, tool_versions_json TEXT NOT NULL, created_at REAL NOT NULL,
 started_at REAL, deadline_at REAL, last_observed_utc REAL NOT NULL, stop_reason TEXT, resolution_reason TEXT);
CREATE TABLE steps(
 run_id TEXT NOT NULL REFERENCES runs, step_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 tool_name TEXT NOT NULL, tool_version TEXT NOT NULL, args_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','active','succeeded','failed','skipped')),
 result_observation_id TEXT REFERENCES observations(observation_id),
 PRIMARY KEY(run_id,step_id), UNIQUE(run_id,ordinal));
CREATE TABLE attempts(
 attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
 attempt_no INTEGER NOT NULL CHECK(attempt_no>=1), status TEXT NOT NULL CHECK(status IN ('prepared','executing','succeeded','failed','aborted','conflict')),
 error_code TEXT, started_at REAL NOT NULL, ended_at REAL,
 FOREIGN KEY(run_id,step_id) REFERENCES steps, UNIQUE(run_id,step_id,attempt_no));
CREATE UNIQUE INDEX one_active_attempt ON attempts((1)) WHERE status IN ('prepared','executing');
CREATE TABLE operations(
 operation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts,
 intent_json TEXT NOT NULL, before_blob BLOB NOT NULL, expected_blob BLOB NOT NULL,
 before_sha TEXT NOT NULL, expected_sha TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','effect_confirmed','not_applied','conflict')),
 effect_kind TEXT NOT NULL CHECK(effect_kind IN ('read','write','assert')));
CREATE TABLE observations(
 observation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('execution','recovery')), sha256 TEXT NOT NULL,
 artifact_id TEXT NOT NULL REFERENCES artifacts, verified_at REAL NOT NULL);
CREATE TABLE events(
 run_id TEXT NOT NULL REFERENCES runs, sequence INTEGER NOT NULL CHECK(sequence>0), event_id TEXT NOT NULL UNIQUE,
 schema_version INTEGER NOT NULL CHECK(schema_version=1), kind TEXT NOT NULL, step_id TEXT, attempt_id TEXT, operation_id TEXT,
 payload_json TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(run_id,sequence));
CREATE TABLE checkpoints(
 run_id TEXT NOT NULL REFERENCES runs, sequence INTEGER NOT NULL, last_event_sequence INTEGER NOT NULL,
 schema_version INTEGER NOT NULL CHECK(schema_version=1), state_json TEXT NOT NULL, state_sha TEXT NOT NULL,
 PRIMARY KEY(run_id,sequence), FOREIGN KEY(run_id,last_event_sequence) REFERENCES events(run_id,sequence));
CREATE TABLE budget_ledger(
 run_id TEXT NOT NULL REFERENCES runs, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts,
 dispatch_reserved INTEGER NOT NULL CHECK(dispatch_reserved=1), execute_started INTEGER NOT NULL CHECK(execute_started IN (0,1)),
 settled INTEGER NOT NULL CHECK(settled IN (0,1)), outcome TEXT, PRIMARY KEY(run_id,attempt_id));
CREATE TABLE control_requests(
 request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs,
 kind TEXT NOT NULL CHECK(kind='cancel'), created_at REAL NOT NULL, applied_at REAL);
CREATE TABLE artifacts(
 artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs, path TEXT NOT NULL,
 sha256 TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0), purpose TEXT NOT NULL,
 publication_state TEXT NOT NULL CHECK(publication_state IN ('evidence','published')), UNIQUE(run_id,path));
CREATE TRIGGER immutable_run BEFORE UPDATE ON runs
WHEN NEW.plan_json != OLD.plan_json OR NEW.plan_hash != OLD.plan_hash OR
 NEW.input_manifest_json != OLD.input_manifest_json OR NEW.input_manifest_hash != OLD.input_manifest_hash OR
 NEW.policy_json != OLD.policy_json OR NEW.policy_hash != OLD.policy_hash OR
 NEW.runtime_version != OLD.runtime_version OR NEW.tool_versions_json != OLD.tool_versions_json
BEGIN SELECT RAISE(ABORT,'immutable_run'); END;
CREATE TRIGGER immutable_step BEFORE UPDATE ON steps
WHEN NEW.args_json != OLD.args_json OR NEW.tool_name != OLD.tool_name OR NEW.tool_version != OLD.tool_version OR NEW.ordinal != OLD.ordinal
BEGIN SELECT RAISE(ABORT,'immutable_step'); END;
CREATE TRIGGER immutable_deadline BEFORE UPDATE ON runs
WHEN (OLD.started_at IS NOT NULL AND (NEW.started_at IS NOT OLD.started_at OR NEW.deadline_at IS NOT OLD.deadline_at))
 OR (OLD.stop_reason IS NOT NULL AND NEW.stop_reason IS NOT OLD.stop_reason)
BEGIN SELECT RAISE(ABORT,'immutable_started_run'); END;
CREATE TRIGGER step_transition BEFORE UPDATE OF status ON steps
WHEN NEW.status != OLD.status AND NOT (
 (OLD.status='pending' AND NEW.status IN ('active','failed','skipped')) OR
 (OLD.status='active' AND NEW.status IN ('pending','succeeded','failed','skipped')))
BEGIN SELECT RAISE(ABORT,'illegal_step_transition'); END;
CREATE TRIGGER attempt_transition BEFORE UPDATE OF status ON attempts
WHEN NEW.status != OLD.status AND NOT (
 (OLD.status='prepared' AND NEW.status IN ('executing','succeeded','aborted','conflict')) OR
 (OLD.status='executing' AND NEW.status IN ('succeeded','failed','aborted','conflict')))
BEGIN SELECT RAISE(ABORT,'illegal_attempt_transition'); END;
CREATE TRIGGER operation_transition BEFORE UPDATE OF status ON operations
WHEN NEW.status != OLD.status AND NOT (OLD.status='prepared' AND NEW.status IN ('effect_confirmed','not_applied','conflict'))
BEGIN SELECT RAISE(ABORT,'illegal_operation_transition'); END;
CREATE TRIGGER immutable_operation BEFORE UPDATE ON operations
WHEN NEW.intent_json != OLD.intent_json OR NEW.before_blob != OLD.before_blob OR NEW.expected_blob != OLD.expected_blob
 OR NEW.before_sha != OLD.before_sha OR NEW.expected_sha != OLD.expected_sha OR NEW.attempt_id != OLD.attempt_id
 OR NEW.effect_kind != OLD.effect_kind
BEGIN SELECT RAISE(ABORT,'immutable_operation'); END;
'''

class Store:
    def __init__(self, root, create=False, readonly=False):
        path = root / 'runtime.sqlite3'
        if not path.exists() and not create:
            raise RuntimeErrorCode('state_not_found')
        uri = path.as_uri() + ('?mode=ro' if readonly else '?mode=rwc' if create else '?mode=rw')
        self.db = sqlite3.connect(uri, uri=True, timeout=3, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=3000')
        self.db.execute('PRAGMA synchronous=FULL')
        tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if not tables and create:
            self.db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + '\nCOMMIT;')
        try:
            rows = self.db.execute('SELECT schema_version FROM schema_meta').fetchall()
            if len(rows) != 1 or rows[0][0] != 1:
                raise RuntimeErrorCode('schema_version_error')
        except RuntimeErrorCode:
            self.close()
            raise
        except sqlite3.DatabaseError as exc:
            self.close()
            raise RuntimeErrorCode('schema_integrity_error') from exc
    def close(self):
        self.db.close()
    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]
    def one(self, sql, args=()):
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None
    def run(self, rid):
        r = self.one('SELECT * FROM runs WHERE run_id=?', (rid,))
        if not r:
            raise RuntimeErrorCode('run_not_found')
        return r
    def event(self, rid, kind, payload=None, step=None, attempt=None, operation=None):
        seq = self.db.execute('SELECT coalesce(max(sequence),0)+1 FROM events WHERE run_id=?', (rid,)).fetchone()[0]
        self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (rid, seq, uuid.uuid4().hex, 1, kind, step, attempt, operation, canonical(payload or {}), time.time()))
        return seq
    def transition(self, rid, target, reason=None):
        if not self.db.in_transaction:
            raise RuntimeErrorCode('transaction_required')
        r = self.run(rid)
        if target not in TRANSITIONS[r['status']]:
            raise RuntimeErrorCode('illegal_transition')
        changed = self.db.execute('UPDATE runs SET status=?,state_version=state_version+1,resolution_reason=coalesce(?,resolution_reason) WHERE run_id=? AND state_version=? AND status=?',
                                 (target, reason, rid, r['state_version'], r['status'])).rowcount
        if changed != 1:
            raise RuntimeErrorCode('state_version_conflict')
        self.event(rid, 'state_changed', {'from': r['status'], 'to': target})
