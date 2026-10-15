"""只读本地分段配置，依次轻量检查 Joye 和 Aispot，不改运行配置。"""
import json
import argparse
from pathlib import Path
import sys
import time
import urllib.error
from datetime import datetime
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evol.llm import JsonChatClient
from scripts.curl_transport import post_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', choices=['joye', 'aispot'])
    parser.add_argument('--timeout', type=int, default=30)
    args = parser.parse_args()
    blocks = {}
    current = None
    for raw in (ROOT / '.env').read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = (x.strip() for x in line.split('=', 1))
        value = value.strip('\"\'')
        if key == 'EVOL_LLM_PROVIDER':
            current = {}
            blocks[value.lower()] = current
        if current is not None:
            current[key] = value
    secrets = [v for block in blocks.values() for k, v in block.items() if 'API_KEY' in k and v]
    def redact(value):
        text = str(value)
        for secret in secrets:
            text = text.replace(secret, '[REDACTED]')
        return text
    reports = []
    for provider in ([args.provider] if args.provider else ['joye', 'aispot']):
        block = blocks.get(provider, {})
        key = block.get('EVOL_LLM_API_KEY') or block.get('EVOL_DEEPSEEK_API_KEY')
        base = block.get('EVOL_LLM_BASE_URL', '').rstrip('/')
        model = block.get('EVOL_LLM_MODEL')
        report = {'provider': provider, 'host': urlsplit(base).hostname, 'model': model, 'ok': False}
        start = time.monotonic()
        try:
            if not key or not base or not model:
                raise ValueError('Incomplete saved configuration')
            expected = {'joye': 'api.joyehuang.dev', 'aispot': 'aispot.swj0227.icu'}[provider]
            assert urlsplit(base).scheme == 'https' and urlsplit(base).hostname == expected
            client = JsonChatClient(base, key, model, 'custom', args.timeout)
            payload = {'model': model, 'messages': [{'role': 'user', 'content': 'Reply with JSON {"ok":true}.'}],
                       'max_tokens': 128, 'temperature': 0, 'stream': False,
                       'response_format': {'type': 'json_object'}}
            raw, status, _ = post_json(client, payload)
            report['http_status'] = status
            data = json.loads(raw)
            content = data['choices'][0]['message']['content']
            report.update(returned_model=data.get('model'), usage=data.get('usage'), reply=redact(content))
            report['ok'] = json.loads(content).get('ok') is True
        except urllib.error.HTTPError as exc:
            report.update(http_status=exc.code, error_response=redact(exc.read().decode('utf-8', errors='replace'))[:1500])
        except Exception as exc:
            report['error'] = redact(exc)
        report['duration_ms'] = round((time.monotonic() - start) * 1000)
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if report['ok']:
            break
    folder = ROOT / 'docs/local/artifacts/provider-connectivity'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ('probe-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
    target.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
