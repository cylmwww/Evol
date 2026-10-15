"""单请求180秒、请求准入600秒的用户授权续跑入口。"""
import inspect
from pathlib import Path
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import resume_gpt_comparison_180s as previous
from scripts import run_protocol_pilot as pilot

base_prepare = previous.prepare
base_method = pilot.ObservedClient._complete_json_once


def patched_method():
    source = textwrap.dedent(inspect.getsource(base_method))
    original = 'time.monotonic() - self.started > 300'
    assert source.count(original) == 1
    source = source.replace(original, 'time.monotonic() - self.started > 600', 1)
    namespace = dict(base_method.__globals__)
    exec(compile(source, str(Path(__file__).resolve()) + ':admission-patch', 'exec'), namespace)
    return namespace['_complete_json_once'], source


def install_patch():
    method, source = patched_method()
    pilot.ObservedClient._complete_json_once = method
    return source


def prepare(rows, settings, config, skills):
    old = base_prepare(rows, settings, config, skills)
    exp = previous.resume.exp
    existing = exp.read(previous.resume.OUT / 'pre-600s-results-sha256.json')
    _, source = patched_method()
    exp.save(previous.resume.OUT / 'protocol-amendment-06-admission-600s.json',
             {'date': '2026-10-14', 'reason': 'User explicitly raised request admission ceiling to 600s and requested background completion with failure counts and reviewed metrics.',
              'previous_admission_seconds': 300, 'admission_seconds': 600,
              'request_timeout_seconds': 180, 'role_seconds': settings.agent_time_budget_seconds,
              'role_token_budget': settings.agent_token_budget, 'request_ceiling': 16,
              'workers': 2, 'http_concurrency': 2, 'existing_terminal_trials': len(existing),
              'wrapper_sha256': exp.digest(Path(__file__).read_bytes()),
              'patched_method_sha256': exp.digest(source.encode()),
              'implementation': 'Runtime copy of frozen transport method; only the admission comparison changes from 300 to 600. Original source files and results preserved.',
              'interpretation': '600s checks admission of the next request, not a hard workflow kill time; an admitted request may finish later.'}, True)
    return old


if __name__ == '__main__':
    install_patch()
    previous.resume.setup = previous.setup
    previous.resume.prepare = prepare
    previous.resume.main()
