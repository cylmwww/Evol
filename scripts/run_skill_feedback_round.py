"""One native feedback mutation; paired static/evolved full multi-agent reviews."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import json
import random
import threading
from urllib.parse import urlsplit

from scripts.run_protocol_pilot import ROOT, Settings, AgentSkill, run_one, digest
from evol.skill_evolution import SkillEvolutionEngine

OUT = ROOT / 'docs/local/artifacts/skill-feedback-round-1'
PRIOR = ROOT / 'docs/local/artifacts/model-comparison-gpt61-32k'
TRAIN = ['pr-0038', 'pr-0041', 'pr-0046', 'pr-0074']

def write_frozen(path, value):
    if path.exists():
        assert json.loads(path.read_text(encoding='utf-8')) == value, 'Changed experiment configuration'
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    settings = Settings.from_env()
    if settings.llm_provider == 'joye':
        settings = replace(settings, llm_provider='custom', llm_api_key=settings.llm_api_key or settings.deepseek_api_key)
    config = settings.resolved_llm()
    assert urlsplit(config['base_url']).hostname == 'api.joyehuang.dev'
    prior = json.loads((PRIOR/'manifest.json').read_text(encoding='utf-8'))
    assert settings.agent_token_budget == 32000 and settings.agent_time_budget_seconds == 180
    assert config['model'] == prior['model'], 'Keep model fixed for Skill comparison'
    assert digest((ROOT/'pr_diff_100.jsonl').read_bytes()) == prior['dataset_sha256']
    skills = [AgentSkill.from_directory(str(p.parent)) for p in sorted((ROOT/'skills').glob('*/SKILL.md'))]
    assert {s.name:s.content_sha256 for s in skills} == prior['skill_hashes']
    rows = {r['id']:r for r in map(json.loads,(ROOT/'pr_diff_100.jsonl').read_text(encoding='utf-8').splitlines())}
    OUT.mkdir(parents=True, exist_ok=True)
    feedback = []
    # Human-reviewed feedback from precisely four fixed training cases. No other
    # case labels or findings reach the mutation builder or model prompts.
    selections = [('pr-0041',1,'REL-SUBPROCESS-INPUTS','missed_issue'),
                  ('pr-0074',1,'REL-IMPLICIT-FLUSH','missed_issue'),
                  ('pr-0038',0,'REL-HASH-ENCODING','missed_issue'),
                  ('pr-0046',0,'REL-UNVERIFIED-GLOBAL','false_positive')]
    for cid,index,rule,category in selections:
        source_arm = 'C-no-critic' if cid == 'pr-0038' else 'D-full'
        r = json.loads((PRIOR/cid/source_arm/'result.json').read_text(encoding='utf-8'))
        finding = dict(r['findings'][index])
        finding['rule_id'] = rule
        # Native mutation admits one changed code line, not multiline evidence.
        finding['evidence'] = finding['evidence'].splitlines()[0].strip()
        feedback.append({'id':cid+':'+rule,'case_id':cid,'category':category,'payload':{'finding':finding},
                         'source_arm':source_arm,'source_finding_index':index})
    original = next(s for s in skills if s.name == 'correctness-review')
    mutation = SkillEvolutionEngine.build_candidate_artifact(original.name, original.to_artifact(), feedback)
    candidate = AgentSkill.from_artifact(mutation['artifact'], version='experimental-round-1')
    assert len(mutation['learned_rule_ids']) == 3
    write_frozen(OUT/'feedback.json', feedback)
    write_frozen(OUT/'candidate-mutation.json', mutation)
    (OUT/'candidate-SKILL.md').write_text(candidate.content, encoding='utf-8')
    banks = {'static':skills, 'evolved':[candidate if s.name==original.name else s for s in skills]}
    jobs = []
    rng = random.Random(20261014)
    for cid in prior['case_ids']:
        groups = ['static','evolved']
        rng.shuffle(groups)
        jobs.extend((cid,g) for g in groups)
    manifest = {
        'experiment':'native-feedback-skill-round-1','model':config['model'],'provider':config['provider'],
        'host':urlsplit(config['base_url']).hostname,'endpoint_sha256':digest(config['base_url'].encode()),
        'dataset_sha256':prior['dataset_sha256'],'case_ids':prior['case_ids'],'jobs':jobs,
        'training_case_ids':TRAIN,'evaluation_case_ids':[c for c in prior['case_ids'] if c not in TRAIN],
        'rounds':1,'critic':True,'roles':['lead','security','correctness-reliability','critic'],
        'role_token_budget':settings.agent_token_budget,'role_seconds':settings.agent_time_budget_seconds,
        'transport_timeout':settings.timeout_seconds,'workers':2,'transport_concurrency':4,
        'request_ceiling_per_run':16,'admission_seconds_per_run':300,
        'repository_context':'diff-only','memory':False,'evolution':'native mutation before replay; frozen during reviews',
        'production_activation':False,'generation':'SkillEvolutionEngine.build_candidate_artifact; deterministic feedback mutation, no LLM generator',
        'skill_hashes':{g:{s.name:s.content_sha256 for s in bank} for g,bank in banks.items()},
        'source_hashes':{str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in sorted((ROOT/'evol').glob('*.py'))},
        'observer_sha256':digest((ROOT/'scripts/run_protocol_pilot.py').read_bytes()),
        'curl_transport_sha256':digest((ROOT/'scripts/curl_transport.py').read_bytes()),
        'script_sha256':digest((ROOT/'scripts/run_skill_feedback_round.py').read_bytes()),
        'semantic_truth_sha256':digest((PRIOR/'semantic-rescore.json').read_bytes()),
        'adjudication_policy':'Use prior shared 24 issue identities; annotate all new findings, separately disclose new out-of-catalogue issues',
        'limitations':['16 cases excluded from mutation input, but already inspected in earlier evaluation; not blind holdout',
                      'Repeated templates may cross training and evaluation cases','No runtime Skill activation; experimental candidate only']}
    # JSON serialization normalizes job tuples for stable resume assertions.
    manifest = json.loads(json.dumps(manifest))
    write_frozen(OUT/'manifest.json', manifest)
    for group in banks:
        folder=OUT/group
        folder.mkdir(exist_ok=True)
        write_frozen(folder/'manifest.json',{**manifest,'group':group,'skill_hashes':manifest['skill_hashes'][group],
                                          'arms':['D-full'],'jobs':[[cid,'D-full'] for cid in prior['case_ids']]})
    print(json.dumps({'prepared':40,'feedback_cases':len(TRAIN),'evaluation_cases':16,
                      'learned_rules':mutation['learned_rule_ids'],'removed_rules':mutation['removed_rule_ids'],
                      'critic':True,'model':config['model']}),flush=True)
    if not args.run:
        return
    semaphore = threading.BoundedSemaphore(4)
    def review(job):
        cid,group=job
        result=run_one(rows[cid],'D-full',config,settings,banks[group],OUT/group,semaphore)
        return group,result
    group,result=review(jobs[0])
    print(json.dumps({'done':1,'group':group,'id':result['id'],'status':result['status']}),flush=True)
    if result['calls'] and all(c.get('http_status',0) not in range(200,300) for c in result['calls']):
        raise RuntimeError('Transport preflight failed; no remaining reviews sent')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(review,job) for job in jobs[1:]]
        for n,future in enumerate(as_completed(futures),2):
            group,result=future.result()
            print(json.dumps({'done':n,'group':group,'id':result['id'],'status':result['status'],
                              'calls':len(result['calls']),'seconds':result['wall_ms']/1000}),flush=True)

if __name__=='__main__':
    main()
