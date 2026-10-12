"""JSON CLI；生产入口不暴露故障注入。"""
import argparse
import json
import sqlite3
import sys
from .runtime.service import Runtime
from .runtime.models import RuntimeErrorCode

def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser()
    p.add_argument('--state-root', required=True)
    sub = p.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create')
    create.add_argument('--source', required=True)
    create.add_argument('--plan', required=True)
    create.add_argument('--policy')
    for name in ('start','resume','cancel','inspect','events','result'):
        cmd = sub.add_parser(name)
        cmd.add_argument('run_id')
        if name == 'events':
            cmd.add_argument('--after-sequence', type=int, default=0)
    a = p.parse_args()
    try:
        r = Runtime(a.state_root)
        if a.command == 'create':
            with open(a.plan, encoding='utf-8-sig') as f:
                plan = json.load(f)
            policy = None
            if a.policy:
                with open(a.policy, encoding='utf-8-sig') as f:
                    policy = json.load(f)
            value = r.create(a.source, plan, policy)
        elif a.command == 'events':
            value = r.events(a.run_id, a.after_sequence)
        else:
            value = getattr(r, a.command)(a.run_id)
        print(json.dumps(value, ensure_ascii=False))
        if a.command in ('start','resume'):
            return {'failed':4,'conflict':4,'cancelled':5,'budget_exhausted':6,'recovering':8,'stopping':8}.get(value['status'],0)
        return 0
    except (ValueError, OSError, sqlite3.DatabaseError, RuntimeErrorCode) as e:
        code = e.code if isinstance(e, RuntimeErrorCode) else 2 if isinstance(e, ValueError) else 3 if isinstance(e, sqlite3.OperationalError) and ('locked' in str(e) or 'busy' in str(e)) else 7
        print(json.dumps({'error':str(e),'exit_code':code},ensure_ascii=False))
        print(str(e), file=sys.stderr)
        return code
