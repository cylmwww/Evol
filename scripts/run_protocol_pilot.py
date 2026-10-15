"""真实模型四组配对小样本评测；结果和原始响应仅保存在忽略目录。"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import random
import sys
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.curl_transport import post_json
from evol.config import Settings
from evol.agentic_core import AgenticReviewer, _parse_findings
from evol.context_manager import ContextManager
from evol.diff_parser import parse_unified_diff
from evol.evaluation_harness import one_to_one_match
from evol.evaluation_v2 import SINGLE_REVIEW_PROMPT
from evol.gates import FindingGate
from evol.llm import JsonChatClient, OutputProtocolError, validate_output, decode_output
from evol.reviewer import SecurityRuleReviewer, ReliabilityRuleReviewer
from evol.skills import AgentSkill
from evol.telemetry import ExecutionLedger

ARMS = ('A-single', 'B-single-scanner', 'C-no-critic', 'D-full')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def select_cases(rows, seed):
    rng = random.Random(seed)
    eligible = [r for r in rows if r['split'] == 'validation']
    risk = [r for r in eligible if r['expected_findings']]
    rng.shuffle(risk)
    # 优先覆盖不同风险类型，不按模型表现挑样本。
    selected, seen = [], set()
    for row in risk:
        kind = row['expected_findings'][0]['rule_id']
        if kind not in seen:
            selected.append(row); seen.add(kind)
        if len(selected) == 10:
            break
    clean = [r for r in eligible if not r['expected_findings']]
    selected += rng.sample(clean, 10)
    rng.shuffle(selected)
    return selected


class Store:
    def __init__(self, roles):
        self.input = {'mode': 'agentic', 'enabled_agents': roles}
        self.checkpoints = {}
        self.lock = threading.Lock()

    def get(self, *args):
        return {'input': self.input}

    def save_checkpoint(self, task, node, state, status, attempt):
        with self.lock:
            self.checkpoints[node] = json.loads(json.dumps({'state': state, 'status': status}))

    def load_checkpoints(self, *args):
        with self.lock:
            return dict(self.checkpoints)


class ObservedClient(JsonChatClient):
    def request_options(self, role):
        # All four arms use the same non-thinking mode for this comparison.
        return super().request_options('lead')

    def __init__(self, config, out, semaphore, timeout):
        super().__init__(config['base_url'], config['api_key'], config['model'],
                         config['provider'], timeout, config.get('headers'))
        self.out, self.semaphore = out, semaphore
        self.calls, self.lock = [], threading.Lock()
        self.started = time.monotonic()

    def clean(self, value):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text.replace(self.api_key, '[REDACTED]')

    def _complete_json_once(self, role, system, user, ledger=None, max_tokens=None):
        # 独立记录每一次真实传输，不依赖产品内可能被 checkpoint 恢复覆盖的账本。
        with self.lock:
            if len(self.calls) >= 16 or time.monotonic() - self.started > 300:
                raise RuntimeError('pilot request/time ceiling exceeded')
            record = {'role': role, 'ok': False, 'usage': {}, 'usage_known': False}
            self.calls.append(record)
            index = len(self.calls)
        payload = {'model': self.model, 'temperature': 0, 'stream': False,
                   'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                   'response_format': {'type': 'json_object'}}
        if max_tokens:
            payload['max_tokens'] = max_tokens
        payload.update(self.request_options(role))
        record['request_sha256'] = digest(json.dumps(payload, sort_keys=True).encode())
        began = time.monotonic()
        body, error = None, ''
        try:
            with self.semaphore:
                if urlsplit(self.base_url).hostname == 'api.joyehuang.dev':
                    raw, record['http_status'], record['content_type'] = post_json(self, payload)
                else:
                    request = urllib.request.Request(self.base_url + '/chat/completions',
                        data=json.dumps(payload, ensure_ascii=False).encode(),
                        headers={'Authorization': 'Bearer ' + self.api_key,
                                 'Content-Type': 'application/json', 'Accept': 'application/json', **self.extra_headers})
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        raw = response.read()
                        record['content_type'] = response.headers.get('Content-Type', '')
                        record['http_status'] = response.status
                # 原始响应留本地，供人工复核；不保存请求头或密钥。
                (self.out / f'call-{index:02d}.json').write_text(self.clean({'request': payload, 'response': raw.decode('utf-8', errors='replace')}), encoding='utf-8')
                body = json.loads(raw)
                record['usage'] = body.get('usage') or {}
                record['usage_known'] = bool(record['usage'])
                choice = body['choices'][0]
                record['finish_reason'] = choice.get('finish_reason')
                try:
                    result = decode_output(choice, role, ledger)
                except (ValueError, TypeError) as exc:
                    raise OutputProtocolError(str(exc)) from None
                record['ok'] = True
                return result
        except Exception as exc:
            error = str(exc).replace(self.api_key, '[REDACTED]')
            if isinstance(exc, urllib.error.HTTPError):
                record['http_status'] = exc.code
            record['error'] = error
            if isinstance(exc, OutputProtocolError):
                raise OutputProtocolError(error) from None
            raise RuntimeError(error) from None
        finally:
            record['duration_ms'] = round((time.monotonic()-began)*1000)
            if ledger:
                ledger.record_model(role, self.provider, self.model, record['usage'], record['duration_ms'], record['ok'], error)
            (self.out / f'call-{index:02d}-meta.json').write_text(self.clean(record), encoding='utf-8')


def run_one(case, arm, config, settings, skills, out, semaphore):
    folder = out / case['id'] / arm
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / 'result.json'
    if destination.exists():
        return json.loads(destination.read_text(encoding='utf-8'))
    started = time.monotonic()
    client = ObservedClient(config, folder, semaphore, settings.timeout_seconds)
    roles = ['lead', 'security', 'correctness-reliability'] + (['critic'] if arm == 'D-full' else [])
    store = Store(roles)
    reviewer = AgenticReviewer(store, client, settings.agent_token_budget,
        settings.agent_time_budget_seconds, enabled_roles=set(roles),
        scanners=[SecurityRuleReviewer(), ReliabilityRuleReviewer()],
        skill_provider=lambda tenant: skills,
        context_manager=ContextManager(settings.agent_context_window_tokens,
          settings.agent_context_input_tokens, settings.context_diff_token_budget,
          settings.context_observation_token_budget, settings.context_recent_observations,
          settings.context_map_chunk_tokens))
    parsed = parse_unified_diff(case['diff'])
    findings, error, summary = [], '', {}
    try:
        if arm.startswith(('A-', 'B-')):
            ledger = ExecutionLedger('single')
            scanner_findings = []
            if arm.startswith('B-'):
                scanner_findings, _, _ = reviewer._scan(case['diff'], parsed, ledger, reviewer.scanners)
            # 标签、修复验证模板和 after_files 均不进入模型上下文。
            payload = {'repository': case['repository'], 'unified_diff': case['diff'],
                       'scanner_findings': [x.to_dict() for x in scanner_findings]}
            prompt = SINGLE_REVIEW_PROMPT + '\nInclude call_chain:[{path,line,symbol}] with actual visible code anchors for high-risk findings. Do not invent callers or missing imports outside the diff.\nAvailable domain review instructions:\n' + '\n\n'.join(s.instructions for s in skills)
            raw = client.complete_json('single-reviewer', prompt, json.dumps(payload, ensure_ascii=False), ledger, max_tokens=4000)
            candidates = _parse_findings(raw, parsed, 'single-reviewer')
            # B 与多 Agent 一样把扫描发现作为候选，由模型审查决定发布；不盲目并入最终结果。
            gate = FindingGate().apply(reviewer._merge(candidates), parsed)
            findings = gate.accepted
            summary = {'gates': gate.checks, 'rejected_findings': gate.rejected}
        else:
            findings = reviewer.review_with_context(case['id'], case['diff'], parsed, repository=case['repository'])
            summary = reviewer.collaboration_summary(case['id'])
    except Exception as exc:
        error = str(exc).replace(config['api_key'], '[REDACTED]')
    calls = client.calls
    successful_roles = {x['role'] for x in calls if x['ok']}
    expected_roles = set(roles) if arm.startswith(('C-', 'D-')) else {'single-reviewer'}
    missing = sorted(expected_roles-successful_roles)
    status = 'failed' if error else ('partial' if missing or any(not x['ok'] for x in calls) else 'complete')
    matches = one_to_one_match(case['expected_findings'], findings, line_tolerance=0)
    result = {'id': case['id'], 'arm': arm, 'status': status, 'error': error,
        'missing_roles': missing, 'wall_ms': round((time.monotonic()-started)*1000),
        'findings': [x.to_dict() for x in findings], 'expected': case['expected_findings'],
        'tp': len(matches), 'fp': len(findings)-len(matches), 'fn': len(case['expected_findings'])-len(matches),
        'matches': [vars(m) for m in matches], 'calls': calls, 'summary': summary,
        'checkpoints': store.checkpoints}
    destination.write_text(client.clean(result), encoding='utf-8')
    return result


def metrics(results):
    tp=sum(x['tp'] for x in results); fp=sum(x['fp'] for x in results); fn=sum(x['fn'] for x in results)
    clean=[x for x in results if not x['expected']]
    calls=[c for x in results for c in x['calls']]
    return {'n':len(results),'tp':tp,'fp':fp,'fn':fn,
        'precision':tp/(tp+fp) if tp+fp else 0,'recall':tp/(tp+fn) if tp+fn else 0,
        'f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0,
        'clean_false_positive_cases':sum(bool(x['findings']) for x in clean),'clean_cases':len(clean),
        'status_counts':dict(Counter(x['status'] for x in results)),
        'model_calls':len(calls),'failed_calls':sum(not c['ok'] for c in calls),
        'known_input_tokens':sum(c['usage'].get('prompt_tokens',0) for c in calls),
        'known_output_tokens':sum(c['usage'].get('completion_tokens',0) for c in calls),
        'unknown_usage_calls':sum(not c['usage_known'] for c in calls),
        'mean_wall_seconds':sum(x['wall_ms'] for x in results)/max(1,len(results))/1000}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',action='store_true');parser.add_argument('--seed',type=int,default=20261013)
    args=parser.parse_args()
    out=ROOT/'docs/local/artifacts/protocol-pilot';out.mkdir(parents=True,exist_ok=True)
    dataset=ROOT/'pr_diff_100.jsonl';rows=[json.loads(x) for x in dataset.read_text(encoding='utf-8').splitlines() if x.strip()]
    cases=select_cases(rows,args.seed)
    settings=Settings.from_env(); config=settings.resolved_llm()
    if not config: raise ValueError('No model configured')
    skills=[AgentSkill.from_directory(str(p.parent)) for p in sorted((ROOT/'skills').glob('*/SKILL.md'))]
    jobs=[];rng=random.Random(args.seed)
    for case in cases:
        arms=list(ARMS);rng.shuffle(arms);jobs.extend((case,arm) for arm in arms)
    manifest={'seed':args.seed,'dataset_sha256':digest(dataset.read_bytes()),'model':config['model'],
        'case_ids':[x['id'] for x in cases], 'jobs':[(c['id'],a) for c,a in jobs],
        'risk_cases':10,'clean_cases':10,'skill_hashes':{s.name:s.content_sha256 for s in skills},
        'source_hashes':{str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in sorted((ROOT/'evol').glob('*.py'))},
        'script_sha256':digest(Path(__file__).read_bytes()),'arms':ARMS,'workers':2,'transport_concurrency':4,
        'role_token_budget':settings.agent_token_budget,'role_seconds':settings.agent_time_budget_seconds,
        'transport_timeout':settings.timeout_seconds,'request_ceiling_per_run':16,'admission_seconds_per_run':300,
        'memory':False,'evolution':False,'repository_context':'diff-only',
        'comparison':'native workflow costs, not equal total token budget',
        'single_skill_policy':'all frozen domain instructions; multi routes same skills dynamically',
        'scanner_policy':'B/C/D same product scanners and AST attachment; model decides final findings',
        'thinking':'disabled for every arm on api.deepseek.com/deepseek-flash',
        'scoring':'exact line and canonical CWE; all trials retained; complete-case intersection secondary'}
    path=out/'manifest.json'
    if path.exists() and json.loads(path.read_text())!=json.loads(json.dumps(manifest)):
        raise RuntimeError('Frozen manifest differs; do not mix runs')
    path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'prepared':len(jobs),'cases':len(cases),'skills':len(skills),'model':config['model']},ensure_ascii=False),flush=True)
    if not args.run:return
    semaphore=threading.BoundedSemaphore(4)
    results=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures={pool.submit(run_one,c,a,config,settings,skills,out,semaphore):(c['id'],a) for c,a in jobs}
        for future in as_completed(futures):
            r=future.result();results.append(r)
            print(json.dumps({'done':len(results),'id':r['id'],'arm':r['arm'],'status':r['status'],'findings':len(r['findings']),'calls':len(r['calls']),'seconds':round(r['wall_ms']/1000,1)},ensure_ascii=False),flush=True)
    report={'all_trials':{a:metrics([r for r in results if r['arm']==a]) for a in ARMS}}
    common=[c['id'] for c in cases if all(next(r for r in results if r['id']==c['id'] and r['arm']==a)['status']=='complete' for a in ARMS)]
    report['complete_case_ids']=common
    report['complete_intersection']={a:metrics([r for r in results if r['arm']==a and r['id'] in common]) for a in ARMS}
    (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__': main()
