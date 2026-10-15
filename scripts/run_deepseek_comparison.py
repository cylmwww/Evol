"""独立 DeepSeek 实验入口：复用冻结方法，隔离 GPT 历史结果和学习候选。"""
import json
from pathlib import Path
import sys
import time
import urllib.error
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import run_full_comparison as experiment
from scripts.curl_transport import post_json
from evol.llm import JsonChatClient

PREVIOUS=ROOT/'docs/local/artifacts/full-comparison-100'
OUT=ROOT/'docs/local/artifacts/deepseek-comparison-100'
experiment.OUT=OUT


def prepare():
    rows,settings,config,skills=experiment.setup()
    assert config['provider']=='deepseek' and urlsplit(config['base_url']).hostname=='api.deepseek.com'
    assert config['model']=='deepseek-flash', '本轮冻结使用当前已配置模型，禁止中途混入其他模型'
    prior=experiment.read(PREVIOUS/'manifest.json')
    assert experiment.split_cases(list(rows.values()))==prior['splits']
    assert experiment.digest((ROOT/'pr_diff_100.jsonl').read_bytes())==prior['dataset_sha256']
    manifest=experiment.prepare(rows,settings,config,skills)
    assert experiment.read(OUT/'truth.json')==experiment.read(PREVIOUS/'truth.json')
    protocol={'wrapper_sha256':experiment.digest(Path(__file__).read_bytes()),
        'previous_directory':str(PREVIOUS),'previous_results_used':False,'previous_candidate_used':False,
        'same_splits':True,'same_semantic_catalog':True,'thinking': 'disabled for all tested roles and judge',
        'judge_retry_policy':'up to 3 attempts for transient failures, all attempts archived; no retry of 401/402/403/429',
        'main_trial_retry_policy':'native protocol retries only; all terminal trials retained',
        'feedback_agent_audit_required':True,'validation_agent_audit_required_before_selection':True,
        'final_agent_audit_required_before_reporting':True}
    experiment.save(OUT/'deepseek-protocol.json',protocol,True)
    preservation=OUT/'previous-files-sha256.json'
    if not preservation.exists():
        experiment.save(preservation,{str(p.relative_to(PREVIOUS)):experiment.digest(p.read_bytes())
                                     for p in sorted(PREVIOUS.rglob('*')) if p.is_file()},True)
    return rows,settings,config,skills,manifest


def health(config):
    folder=OUT/'transport-health';folder.mkdir(parents=True,exist_ok=True)
    attempt=len(list(folder.glob('call-*-meta.json')))+1
    client=JsonChatClient(config['base_url'],config['api_key'],config['model'],config['provider'],30,config.get('headers'))
    payload={'model':config['model'],'messages':[{'role':'user','content':'Reply with JSON {"ok":true}.'}],
             'max_tokens':128,'temperature':0,'stream':False,'response_format':{'type':'json_object'},
             **client.request_options('lead')}
    meta={'role':'transport-health','ok':False,'usage':{},'usage_known':False}
    start=time.monotonic();body=''
    try:
        raw,status,kind=post_json(client,payload)
        body=raw.decode('utf-8',errors='replace').replace(config['api_key'],'[REDACTED]')
        parsed=json.loads(body)
        assert json.loads(parsed['choices'][0]['message']['content']).get('ok') is True
        meta.update(ok=True,http_status=status,usage=parsed.get('usage') or {},usage_known=bool(parsed.get('usage')),
                    returned_model=parsed.get('model'))
    except urllib.error.HTTPError as exc:
        meta.update(http_status=exc.code,error=str(exc))
        body=exc.read().decode('utf-8',errors='replace').replace(config['api_key'],'[REDACTED]')
    except Exception as exc:
        meta['error']=str(exc).replace(config['api_key'],'[REDACTED]')
    meta['duration_ms']=round((time.monotonic()-start)*1000)
    experiment.save(folder/f'call-{attempt:02d}-meta.json',meta,True)
    experiment.save(folder/f'call-{attempt:02d}.json',{'response':body},True)
    print(json.dumps({'meta':meta,'error_response':body[:1500] if not meta['ok'] else None},ensure_ascii=False),flush=True)
    if not meta['ok']:raise SystemExit(1)


original_adjudicate=experiment.adjudicate_case


def resumable_adjudicate(cid,groups,tag,rows,config,settings):
    for attempt in range(1,4):
        source=(OUT/'judge-calls'/tag/cid).resolve()
        if source.exists() and not (OUT/'adjudications'/tag/(cid+'.json')).exists():
            root=(OUT/'judge-attempts'/tag/cid).resolve()
            number=len(list(root.glob('attempt-*')))+1 if root.exists() else 1
            destination=(root/f'attempt-{number:02d}').resolve()
            assert source.is_relative_to(OUT.resolve()) and destination.is_relative_to(OUT.resolve())
            destination.parent.mkdir(parents=True,exist_ok=True)
            source.rename(destination)
        try:
            return original_adjudicate(cid,groups,tag,rows,config,settings)
        except Exception as exc:
            if attempt==3 or any(f'HTTP Error {code}' in str(exc) for code in (401,402,403,429)):
                raise
            print(json.dumps({'judge_retry':cid,'tag':tag,'attempt':attempt,'error':str(exc).replace(config['api_key'],'[REDACTED]')}),flush=True)
            time.sleep(2*attempt)


def main():
    if len(sys.argv)!=2:
        raise SystemExit('指定 prepare/health/feedback-runs/.../index-feedback/index-validation/index-final/progress/verify-previous')
    stage=sys.argv[1]
    rows,settings,config,skills,manifest=prepare()
    if stage=='health':
        health(config)
    elif stage=='verify-previous':
        old=experiment.read(OUT/'previous-files-sha256.json')
        changed=[p for p,h in old.items() if not (PREVIOUS/p).exists() or experiment.digest((PREVIOUS/p).read_bytes())!=h]
        assert not changed, changed
        print(json.dumps({'previous_files_verified':len(old),'changed':0}))
    elif stage=='progress' or stage.startswith('index-'):
        from scripts import audit_full_comparison as audit
        audit.OUT=OUT
        audit.progress() if stage=='progress' else audit.index(stage.removeprefix('index-'))
    else:
        experiment.adjudicate_case=resumable_adjudicate
        experiment.main()


if __name__=='__main__':main()
