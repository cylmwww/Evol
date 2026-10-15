"""统计 GPT 读取超时的调用、试次与流程恢复，保留独立补跑候选。"""
import json
from collections import Counter
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.analyze_protocol_pilot import audit_result

OUT = ROOT / 'docs/local/artifacts/full-comparison-100'


def read_timeout(error):
    return bool(re.search(r'read(?:\s+operation)?\s+timed?\s*out|readtimeout', str(error), re.I))


def main():
    records = []
    for path in sorted((OUT / 'runs').glob('*/*/*/*/result.json')):
        result = json.loads(path.read_text(encoding='utf-8'))
        audited = audit_result(result)
        timeouts = [c for c in result['calls'] if read_timeout(c.get('error', ''))]
        generic = [c for c in result['calls'] if 'timed out' in c.get('error', '').lower()
                   and not read_timeout(c.get('error', ''))]
        records.append({'case': result['id'], 'group': path.parts[-5], 'repeat': int(path.parts[-4]),
                        'status': audited['status'], 'final_workflow_completed': audited['final_workflow_completed'],
                        'read_timeout_calls': len(timeouts), 'other_timeout_calls': len(generic),
                        'terminal_error_is_read_timeout': read_timeout(result.get('error', '')),
                        'incomplete_after_read_timeout': bool(timeouts) and not audited['final_workflow_completed'],
                        'read_timeout_roles': [c['role'] for c in timeouts],
                        'terminal_error': result.get('error', ''), 'source': str(path.relative_to(OUT))})
    def counts(items):
        return {'terminal_trials': len(items), 'read_timeout_calls': sum(r['read_timeout_calls'] for r in items),
                'trials_with_read_timeout': sum(r['read_timeout_calls'] > 0 for r in items),
                'terminal_error_is_read_timeout': sum(r['terminal_error_is_read_timeout'] for r in items),
                'incomplete_after_read_timeout': sum(r['incomplete_after_read_timeout'] for r in items),
                'completed_after_read_timeout': sum(r['read_timeout_calls'] > 0 and r['final_workflow_completed'] for r in items),
                'other_timeout_calls': sum(r['other_timeout_calls'] for r in items),
                'statuses': dict(Counter(r['status'] for r in items))}
    report = {'scope': 'primary terminal trials only; judges, stability tests and interrupted calls excluded',
              'classification': 'explicit read-timeout error text; generic timeout recorded separately; incomplete-after-timeout is association, not proof of sole cause',
              'all': counts(records), 'groups': {g: counts([r for r in records if r['group'] == g])
                                               for g in ['single', 'multi', 'evolved']},
              'retry_candidates': [r for r in records if r['incomplete_after_read_timeout']],
              'affected_records': [r for r in records if r['read_timeout_calls'] or r['other_timeout_calls']]}
    target = OUT / 'read-timeout-audit.json'
    temp = target.with_suffix('.json.tmp')
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(target)
    print(json.dumps({'all': report['all'], 'groups': report['groups']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
