"""独立 Aispot 单案例真实工作流检查，不写入历史批量实验。"""
import json
import argparse
from collections import Counter
from pathlib import Path
import sys
import threading
from dataclasses import replace
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_protocol_pilot import run_one, Settings, AgentSkill
from scripts.analyze_protocol_pilot import audit_result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeats', type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()
    block = {}
    active = False
    for line in (ROOT / '.env').read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = (s.strip().strip('\"\'') for s in line.split('=', 1))
        if key == 'EVOL_LLM_PROVIDER':
            active = value.lower() == 'aispot'
        if active:
            block[key] = value
    config = {'provider': 'custom', 'base_url': block['EVOL_LLM_BASE_URL'],
              'api_key': block.get('EVOL_LLM_API_KEY') or block['EVOL_DEEPSEEK_API_KEY'],
              'model': block['EVOL_LLM_MODEL']}
    assert config['base_url'].rstrip('/') == 'https://aispot.swj0227.icu/v1'
    settings = replace(Settings.from_env(), timeout_seconds=120, agent_time_budget_seconds=180,
                       agent_token_budget=32000)
    cases = [json.loads(line) for line in (ROOT / 'pr_diff_100.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    case = next(c for c in cases if c['id'] == 'pr-0003')
    skills = [AgentSkill.from_directory(str(p.parent)) for p in sorted((ROOT / 'skills').glob('*/SKILL.md'))]
    out = ROOT / 'docs/local/artifacts/aispot-one-case' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    out.mkdir(parents=True, exist_ok=True)
    print(json.dumps({'case': case['id'], 'arm': 'D-full', 'model': config['model'], 'timeout': 120, 'output': str(out)}), flush=True)
    records = []
    for repeat in range(1, args.repeats + 1):
        folder = out if args.repeats == 1 else out / f'repeat-{repeat}'
        result = run_one(case, 'D-full', config, settings, skills, folder, threading.BoundedSemaphore(2))
        audited = audit_result(result)
        (folder / 'workflow-audit.json').write_text(json.dumps(audited, ensure_ascii=False, indent=2), encoding='utf-8')
        record = {'repeat': repeat, 'status': audited['status'], 'error': result['error'],
                  'final_workflow_completed': audited['final_workflow_completed'],
                  'seconds': result['wall_ms']/1000, 'calls': len(result['calls']),
                  'successful_calls': sum(c['ok'] for c in result['calls']),
                  'http_statuses': dict(Counter(str(c.get('http_status')) for c in result['calls'])),
                  'errors': [c.get('error') for c in result['calls'] if not c['ok']],
                  'findings': len(result['findings']), 'workflow_problems': audited['workflow_problems']}
        records.append(record)
        summary = {'provider': 'aispot', 'model': config['model'], 'case': case['id'], 'arm': 'D-full',
                   'planned': args.repeats, 'finished': len(records), 'trial_concurrency': 1,
                   'transport_concurrency': 2, 'request_timeout_seconds': 120,
                   'strict_complete': sum(r['status'] == 'complete' for r in records),
                   'final_workflow_completed': sum(r['final_workflow_completed'] for r in records),
                   'records': records}
        (out / 'stability-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
