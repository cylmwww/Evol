"""限流后的轻量连通检查；不占用正式试次，保留失败响应与用量。"""
import json
from pathlib import Path
import sys
import time
import urllib.error

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.run_full_comparison import setup, OUT, save
from scripts.curl_transport import post_json
from evol.llm import JsonChatClient


def main():
    _,settings,config,_=setup()
    folder=OUT/'transport-health'
    folder.mkdir(parents=True,exist_ok=True)
    attempt=len(list(folder.glob('call-*-meta.json')))+1
    client=JsonChatClient(config['base_url'],config['api_key'],config['model'],config['provider'],30,config.get('headers'))
    payload={'model':config['model'],'messages':[{'role':'user','content':'Reply with JSON {"ok":true}.'}],
             'max_tokens':64,'temperature':0,'stream':False,'response_format':{'type':'json_object'}}
    meta={'ok':False,'usage':{},'usage_known':False,'role':'transport-health'}
    start=time.monotonic();body=''
    try:
        raw,status,kind=post_json(client,payload)
        body=raw.decode('utf-8',errors='replace').replace(config['api_key'],'[REDACTED]')
        parsed=json.loads(body)
        meta.update(ok=True,http_status=status,usage=parsed.get('usage') or {},usage_known=bool(parsed.get('usage')))
    except urllib.error.HTTPError as exc:
        meta.update(http_status=exc.code,error=str(exc))
        body=exc.read().decode('utf-8',errors='replace').replace(config['api_key'],'[REDACTED]')
    except Exception as exc:
        meta['error']=str(exc).replace(config['api_key'],'[REDACTED]')
    meta['duration_ms']=round((time.monotonic()-start)*1000)
    save(folder/f'call-{attempt:02d}-meta.json',meta,True)
    save(folder/f'call-{attempt:02d}.json',{'response':body},True)
    print(json.dumps({'meta':meta,'error_response':body[:1500] if not meta['ok'] else None},ensure_ascii=False))
    if not meta['ok']:raise SystemExit(1)


if __name__=='__main__':main()
