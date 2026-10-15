"""汇总 pilot 独立传输记录；恢复 SSE 用量，不改变审查成功判定。"""
import argparse
import json
from pathlib import Path
import statistics
import sys
from collections import Counter

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.run_multi_agent_pilot import ARMS, metrics


def recover_calls(folder, result):
    calls=[]
    for index, original in enumerate(result['calls'],1):
        call=dict(original)
        rawfile=folder/f'call-{index:02d}.json'
        if not call['usage_known'] and rawfile.exists():
            raw=json.loads(rawfile.read_text(encoding='utf-8'))['response']
            usages=[]
            for line in raw.splitlines():
                if not line.startswith('data:'):continue
                try: body=json.loads(line[5:].strip())
                except ValueError:continue
                if body.get('usage'):usages.append(body['usage'])
            if usages:
                # SSE usage 为累计值，取最后一条，绝不对各 chunk 累加。
                call['usage']=usages[-1];call['usage_known']=True;call['usage_recovered_from_sse']=True
        calls.append(call)
    return calls


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='docs/local/artifacts/multi-agent-pilot');args=parser.parse_args()
    out=ROOT/args.output
    results=[]
    for path in sorted(out.glob('*/*/result.json')):
        r=json.loads(path.read_text(encoding='utf-8'));r['calls']=recover_calls(path.parent,r);results.append(r)
    allmetrics={a:metrics([r for r in results if r['arm']==a]) for a in ARMS}
    for arm,m in allmetrics.items():
        rr=[r for r in results if r['arm']==arm]
        times=sorted(r['wall_ms']/1000 for r in rr)
        m['p50_wall_seconds']=statistics.median(times) if times else None
        m['p95_wall_seconds']=times[min(len(times)-1,int(len(times)*.95))] if times else None
        m['sse_usage_recovered_calls']=sum(c.get('usage_recovered_from_sse',False) for r in rr for c in r['calls'])
        m['known_total_tokens']=m['known_input_tokens']+m['known_output_tokens']
        m['clean_complete_without_comments']=sum(not r['expected'] and not r['findings'] and r['status']=='complete' for r in rr)
        m['exact_complete_cases']=sum(r['status']=='complete' and r['fp']==0 and r['fn']==0 for r in rr)
        m['observed_content_types']=dict(Counter(c.get('content_type','no_response') for r in rr for c in r['calls']))
    report={'finished_runs':len(results),'all_trials':allmetrics,'complete_only':{a:metrics([r for r in results if r['arm']==a and r['status']=='complete']) for a in ARMS},'paired_complete':{}}
    for left,right in [('A-single','B-single-scanner'),('B-single-scanner','C-no-critic'),('B-single-scanner','D-full'),('C-no-critic','D-full')]:
        ids=set(r['id'] for r in results if r['arm']==left and r['status']=='complete') & set(r['id'] for r in results if r['arm']==right and r['status']=='complete')
        report['paired_complete'][left+' vs '+right]={'case_ids':sorted(ids),'left':metrics([r for r in results if r['arm']==left and r['id'] in ids]),'right':metrics([r for r in results if r['arm']==right and r['id'] in ids])}
    report['total_known_tokens']=sum(m['known_total_tokens'] for m in allmetrics.values())
    report['total_calls']=sum(m['model_calls'] for m in allmetrics.values())
    report['unknown_usage_calls']=sum(m['unknown_usage_calls'] for m in allmetrics.values())
    (out/'audited-summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 逐项结果复核索引','', '严格标签匹配之外的发现需结合 diff 复核；本文件不自动认定额外问题为误报。','']
    rows={r['id']:r for r in map(json.loads,(ROOT/'pr_diff_100.jsonl').read_text(encoding='utf-8').splitlines())}
    for caseid in sorted({r['id'] for r in results}):
        row=rows[caseid];lines+=['## '+caseid,'','```diff',row['diff'].strip(),'```','', '标准标签：'+json.dumps(row['expected_findings'],ensure_ascii=False),'']
        for r in [r for r in results if r['id']==caseid]:
            lines += ['### '+r['arm']+' / '+r['status'],'',f"TP={r['tp']} FP={r['fp']} FN={r['fn']}",'']
            for i,f in enumerate(r['findings']):
                lines += [f"- [{i}] {f.get('cwe')} L{f['line']} {f['title']}",f"  {f['explanation']}"]
            lines += ['']
    (out/'review-index.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({'runs':len(results),'calls':report['total_calls'],'known_tokens':report['total_known_tokens'],'unknown_usage_calls':report['unknown_usage_calls'],'statuses':{a:m['status_counts'] for a,m in allmetrics.items()}},ensure_ascii=False))

if __name__=='__main__':main()
