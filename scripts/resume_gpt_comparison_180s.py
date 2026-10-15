"""用户授权将单请求读取上限改为180秒；保留此前冻结记录和修订。"""
from dataclasses import replace
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import resume_gpt_comparison as resume

original_setup = resume.setup
original_prepare = resume.prepare


def setup():
    rows, settings, config, skills = original_setup()
    return rows, replace(settings, timeout_seconds=180), config, skills


def prepare(rows, settings, config, skills):
    old = resume.exp.read(resume.OUT / 'manifest.json')
    original_prepare(rows, replace(settings, timeout_seconds=old['transport_timeout']), config, skills)
    previous = resume.exp.read(resume.OUT / 'pre-180s-results-sha256.json')
    amendment = {'date': '2026-10-14', 'reason': 'User explicitly requested request timeout 180s and continuation.',
                 'previous_request_timeout': 120, 'current_request_timeout': 180,
                 'role_seconds_unchanged': settings.agent_time_budget_seconds,
                 'role_tokens_unchanged': settings.agent_token_budget,
                 'request_admission_ceiling_unchanged': 300, 'request_count_ceiling_unchanged': 16,
                 'workers': 2, 'http_concurrency': 2, 'model': config['model'],
                 'existing_terminal_trials': len(previous), 'prior_results_preserved': True,
                 'wrapper_sha256': resume.exp.digest(Path(__file__).read_bytes()),
                 'interpretation': 'Per-request network wait limit, not a 180s limit on the entire multi-agent workflow.',
                 'retry_policy': 'Finish missing primary trials first; preserve timeout-related supplementary reruns independently.'}
    resume.exp.save(resume.OUT / 'protocol-amendment-05-timeout-180s.json', amendment, True)
    return old


if __name__ == '__main__':
    resume.setup = setup
    resume.prepare = prepare
    resume.main()
