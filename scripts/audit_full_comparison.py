"""读取实验轨迹，生成复核索引及独立计数检查，不发送模型请求。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.run_full_comparison import OUT, read, digest, template


def progress():
    files=list((OUT/'runs').glob('*/*/*/*/result.json'))
    results=[read(p) for p in files]
    calls=[read(p) for p in (OUT/'runs').glob('*/*/*/*/call-*-meta.json')]
    result={'finished':len(results),'groups':dict(Counter(p.parts[-5] for p in files)),
            'statuses':dict(Counter(r['status'] for r in results)),
            'calls':len(calls),'http_statuses':dict(Counter(c.get('http_status') for c in calls)),
            'errors':dict(Counter(c.get('error') for c in calls if c.get('error')))}
    print(json.dumps(result,ensure_ascii=False))


def index(tag):
    records=[]
    for p in sorted((OUT/'adjudications'/tag).glob('*.json')):
        adj=read(p)
        for item in adj['items']:
            ref=adj['references'][item['key']]
            arm='A-single' if ref['group']=='single' else 'D-full'
            source=OUT/'runs'/ref['group']/str(ref['repeat'])/adj['id']/arm/'result.json'
            if tag=='validation' and ref['group']=='evolved':
                # 回退后的同路径可能已产生新结果，原候选应按内容散列定位。
                if not source.exists() or digest(source.read_bytes())!=ref['result_sha256']:
                    source=OUT/'rejected-validation'/str(ref['repeat'])/adj['id']/arm/'result.json'
            assert digest(source.read_bytes())==ref['result_sha256']
            finding=read(source)['findings'][ref['index']]
            records.append({'id':adj['id'],**ref,**item,'title':finding['title'],'explanation':finding['explanation'],
                            'source':str(source.relative_to(OUT))})
    target=OUT/f'{tag}-review-index.json'
    target.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'tag':tag,'findings':len(records),'matched':sum(x['issue'] is not None for x in records),
        'out_of_catalogue':sum(x.get('out_of_catalogue',False) for x in records),'uncertain':sum(x.get('uncertain',False) for x in records)},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['progress','feedback','validation','final'])
    a=parser.parse_args()
    progress() if a.action=='progress' else index(a.action)
