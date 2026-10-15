"""按明确的服务商/并发修订续跑 GPT，原始 manifest 与已有结果保持不变。"""
import json
from pathlib import Path
import sys
import threading
import time
from dataclasses import replace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_full_comparison as exp

OUT = exp.OUT
original_adjudicate = exp.adjudicate_case


def setup():
    cases = [json.loads(line) for line in (ROOT / 'pr_diff_100.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    rows = {case['id']: case for case in cases}
    assert len(cases) == len(rows) == 100
    settings = exp.Settings.from_env()
    skills = [exp.AgentSkill.from_directory(str(path.parent)) for path in sorted((ROOT / 'skills').glob('*/SKILL.md'))]
    active = False
    block = {}
    for raw in (ROOT / '.env').read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = (x.strip().strip('\"\'') for x in line.split('=', 1))
        if key == 'EVOL_LLM_PROVIDER':
            active = value.lower() == 'aispot'
        if active:
            block[key] = value
    config = {'provider': 'custom', 'base_url': block['EVOL_LLM_BASE_URL'],
              'api_key': block.get('EVOL_LLM_API_KEY') or block['EVOL_DEEPSEEK_API_KEY'],
              'model': block['EVOL_LLM_MODEL']}
    assert config['base_url'].rstrip('/') == 'https://aispot.swj0227.icu/v1'
    assert config['model'] == 'gpt-6.1-sol'
    old = exp.read(OUT / 'manifest.json')
    settings = replace(settings, timeout_seconds=old['transport_timeout'],
                       agent_time_budget_seconds=old['role_seconds'],
                       agent_token_budget=old['role_token_budget'])
    return rows, settings, config, skills


def prepare(rows, settings, config, skills):
    old = exp.read(OUT / 'manifest.json')
    assert old['model'] == config['model']
    assert exp.digest((ROOT / 'pr_diff_100.jsonl').read_bytes()) == old['dataset_sha256']
    assert exp.split_cases(list(rows.values())) == old['splits']
    assert {s.name: s.content_sha256 for s in skills} == old['skill_hashes']
    assert {cid: exp.target_catalog(row) for cid, row in rows.items()} == exp.read(OUT / 'truth.json')
    for relative, digest in {**old['source_hashes'], **old['dependencies_sha256']}.items():
        assert exp.digest((ROOT / relative).read_bytes()) == digest, relative
    assert exp.digest((ROOT / 'scripts/run_full_comparison.py').read_bytes()) == old['runner_sha256']
    amendment = {'reason': 'User explicitly requested continuation from 217/900 after Aispot five-run stability test.',
                 'prior_manifest_preserved': True, 'prior_endpoint_sha256': old['endpoint_sha256'],
                 'current_endpoint_host': 'aispot.swj0227.icu',
                 'current_endpoint_sha256': exp.digest(config['base_url'].encode()),
                 'model': config['model'], 'provider': 'custom', 'workers': 2, 'http_concurrency': 2,
                 'transport_timeout': settings.timeout_seconds, 'role_seconds': settings.agent_time_budget_seconds,
                 'role_token_budget': settings.agent_token_budget, 'wrapper_sha256': exp.digest(Path(__file__).read_bytes()),
                 'primary_trial_policy': 'Keep every existing terminal trial, including failures; run only absent trials.',
                 'judge_retry_policy': 'Up to three transient retries; archive every attempt; stop on auth/balance/rate limit.',
                 'limitation': 'Same requested model alias across two gateways does not prove identical upstream model, routing or behavior. Report gateway phases separately.',
                 'deepseek_preservation_note': 'DeepSeek preservation hashes describe the GPT snapshot before this explicitly authorized continuation.'}
    exp.save(OUT / 'protocol-amendment-03-aispot.json', amendment, True)
    snapshot = OUT / 'pre-aispot-files-sha256.json'
    if not snapshot.exists():
        files = list((OUT / 'runs').rglob('*.json'))
        assert len(list((OUT / 'runs').glob('*/*/*/*/result.json'))) == 217
        files += [OUT / n for n in ['manifest.json', 'truth.json', 'base-skills.json', 'candidate.json', 'feedback.json']]
        exp.save(snapshot, {str(p.relative_to(OUT)): exp.digest(p.read_bytes()) for p in sorted(files)}, True)
    return old


def adjudicate(cid, groups, tag, rows, config, settings):
    for attempt in range(1, 4):
        folder = (OUT / 'judge-calls' / tag / cid).resolve()
        if folder.exists() and not (OUT / 'adjudications' / tag / (cid + '.json')).exists():
            root = (OUT / 'judge-attempts-aispot' / tag / cid).resolve()
            target = root / f'attempt-{len(list(root.glob("attempt-*"))) + 1:02d}'
            assert folder.is_relative_to(OUT.resolve()) and target.is_relative_to(OUT.resolve())
            root.mkdir(parents=True, exist_ok=True)
            folder.rename(target)
        try:
            return original_adjudicate(cid, groups, tag, rows, config, settings)
        except Exception as exc:
            if attempt == 3 or any(f'HTTP Error {code}' in str(exc) for code in [401, 402, 403, 429]):
                raise
            print(json.dumps({'judge_retry': cid, 'tag': tag, 'attempt': attempt,
                              'error': str(exc).replace(config['api_key'], '[REDACTED]')}), flush=True)
            time.sleep(2 * attempt)


def main():
    exp.setup = setup
    exp.prepare = prepare
    exp.adjudicate_case = adjudicate
    exp.WORKERS = 2
    exp.SEMAPHORE = threading.BoundedSemaphore(2)
    if sys.argv[1] == 'verify-existing':
        rows, settings, config, skills = setup()
        prepare(rows, settings, config, skills)
        hashes = exp.read(OUT / 'pre-aispot-files-sha256.json')
        assert all(exp.digest((OUT / p).read_bytes()) == h for p, h in hashes.items())
        print(json.dumps({'existing_files_verified': len(hashes)}))
    else:
        exp.main()


if __name__ == '__main__':
    main()
