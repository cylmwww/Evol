"""Audit final worker outcomes separately from successful HTTP/action calls."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_multi_agent_pilot import ARMS, metrics
from scripts.summarize_multi_agent_pilot import recover_calls


def audit_result(result):
    r = dict(result)
    r['recorded_status'] = r['status']
    problems = []
    if r['arm'].startswith(('C-', 'D-')) and not r['error']:
        collaboration = r['summary'].get('collaboration', {})
        workers = collaboration.get('worker_results', [])
        for worker in workers:
            if worker.get('status') != 'completed':
                problems.append({'worker': worker.get('worker'), 'error': worker.get('error'),
                                 'assignment_id': worker.get('assignment_id')})
        if not workers:
            problems.append({'error': 'No final worker results'})
        if not collaboration.get('lead', {}).get('final'):
            problems.append({'error': 'No lead final synthesis'})
    r['workflow_problems'] = problems
    r['final_workflow_completed'] = not r['error'] and not problems and not r.get('missing_roles')
    if problems and r['status'] == 'complete':
        r['status'] = 'partial'
    return r


def summarize(rows, deepseek_prices=False):
    m = metrics(rows)
    m['known_total_tokens'] = m['known_input_tokens'] + m['known_output_tokens']
    m['mean_tokens_per_trial'] = m['known_total_tokens'] / len(rows) if rows else None
    times = sorted(r['wall_ms'] / 1000 for r in rows)
    m['p50_seconds'] = statistics.median(times) if times else None
    m['p95_seconds'] = times[min(len(times)-1, int(.95*len(times)))] if times else None
    m['exact_complete_cases'] = sum(r['status'] == 'complete' and r['fp'] == r['fn'] == 0 for r in rows)
    m['clean_complete_without_comments'] = sum(r['status'] == 'complete' and not r['expected'] and not r['findings'] for r in rows)
    m['workflow_problem_cases'] = sum(bool(r['workflow_problems']) for r in rows)
    m['final_workflow_completed_cases'] = sum(r['final_workflow_completed'] for r in rows)
    m['cache_hit_tokens'] = sum(c['usage'].get('prompt_cache_hit_tokens', 0) for r in rows for c in r['calls'])
    m['cache_miss_tokens'] = sum(c['usage'].get('prompt_cache_miss_tokens', 0) for r in rows for c in r['calls'])
    # Published USD rates, retrieved 2026-10-13. Range avoids guessing billing tier/time.
    if deepseek_prices and m['cache_hit_tokens'] + m['cache_miss_tokens'] == m['known_input_tokens']:
        m['estimated_usd_off_peak'] = (m['cache_hit_tokens']*.003 + m['cache_miss_tokens']*.15 + m['known_output_tokens']*.6)/1_000_000
        m['estimated_usd_peak'] = m['estimated_usd_off_peak']*2
    return m


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='docs/local/artifacts/protocol-pilot')
    args = parser.parse_args()
    out = ROOT / args.output
    manifest = json.loads((out/'manifest.json').read_text(encoding='utf-8'))
    deepseek_prices = manifest.get('model') == 'deepseek-flash' and manifest.get('host', 'api.deepseek.com') == 'api.deepseek.com'
    priced_summary = lambda rr: summarize(rr, deepseek_prices)
    rows = []
    for file in sorted(out.glob('*/*/result.json')):
        r = audit_result(json.loads(file.read_text(encoding='utf-8')))
        r['calls'] = recover_calls(file.parent, r)
        rows.append(r)
    report = {'finished_runs': len(rows), 'all_trials': {}, 'paired_complete': {},
              'status_corrections': [], 'case_results': []}
    report['price_source'] = 'https://api-docs.deepseek.com/quick_start/pricing/' if deepseek_prices else None
    report['price_note'] = 'Published peak/off-peak USD estimates, not an account invoice; current rates may change.' if deepseek_prices else 'Provider pricing unknown; token counts only.'
    for arm in ARMS:
        report['all_trials'][arm] = priced_summary([r for r in rows if r['arm'] == arm])
    for left, right in [('A-single', 'B-single-scanner'), ('A-single', 'D-full'),
                        ('B-single-scanner', 'C-no-critic'), ('B-single-scanner', 'D-full'), ('C-no-critic', 'D-full')]:
        ids = {r['id'] for r in rows if r['arm'] == left and r['status'] == 'complete'} & {r['id'] for r in rows if r['arm'] == right and r['status'] == 'complete'}
        report['paired_complete'][left + ' vs ' + right] = {
            'case_ids': sorted(ids),
            'left': priced_summary([r for r in rows if r['arm'] == left and r['id'] in ids]),
            'right': priced_summary([r for r in rows if r['arm'] == right and r['id'] in ids])}
    common = set.intersection(*[{r['id'] for r in rows if r['arm'] == arm and r['status'] == 'complete'} for arm in ARMS])
    report['all_arms_complete_ids'] = sorted(common)
    report['all_arms_complete'] = {a: priced_summary([r for r in rows if r['arm'] == a and r['id'] in common]) for a in ARMS}
    for r in rows:
        detail = {k: r[k] for k in ['id', 'arm', 'status', 'recorded_status', 'tp', 'fp', 'fn', 'wall_ms', 'workflow_problems']}
        detail['tokens'] = sum(c['usage'].get('prompt_tokens', 0) + c['usage'].get('completion_tokens', 0) for c in r['calls'])
        report['case_results'].append(detail)
        if r['recorded_status'] != r['status']:
            report['status_corrections'].append(detail)
    report['failed_call_errors'] = dict(Counter(c.get('error') for r in rows for c in r['calls'] if not c['ok']))
    (out/'workflow-audited-summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'runs':len(rows), 'status_corrections':len(report['status_corrections']),
                      'all_trials':report['all_trials']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
