"""Use the provider's documented curl transport without exposing credentials in argv."""
import io
import json
import os
import shutil
import subprocess
import urllib.error


def post_json(client, payload):
    binary = shutil.which('curl.exe' if os.name == 'nt' else 'curl')
    if not binary:
        raise RuntimeError('curl is required for this provider')
    headers = {'Authorization': 'Bearer ' + client.api_key,
               'Content-Type': 'application/json', 'Accept': 'application/json', **client.extra_headers}
    url = client.base_url + '/chat/completions'
    lines = ['url = ' + json.dumps(url), 'request = "POST"']
    lines += ['header = ' + json.dumps(k + ': ' + v) for k, v in headers.items()]
    lines += ['data = ' + json.dumps(json.dumps(payload, ensure_ascii=False), ensure_ascii=False),
              'silent', 'show-error', 'max-time = ' + str(client.timeout),
              'write-out = "\\nEVOL_HTTP:%{http_code}|%{content_type}"']
    try:
        proc = subprocess.run([binary, '--disable', '--config', '-'],
                              input='\n'.join(lines).encode('utf-8'), capture_output=True,
                              timeout=client.timeout + 5,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except subprocess.TimeoutExpired:
        raise RuntimeError('curl transport timeout') from None
    if proc.returncode:
        raise RuntimeError('curl transport failed: ' + proc.stderr.decode('utf-8', errors='replace').replace(client.api_key, '[REDACTED]'))
    raw, separator, metadata = proc.stdout.rpartition(b'\nEVOL_HTTP:')
    if not separator:
        raise RuntimeError('curl response has no HTTP metadata')
    status, content_type = metadata.decode('utf-8').split('|', 1)
    status = int(status)
    if not 200 <= status < 300:
        raise urllib.error.HTTPError(url, status, 'HTTP request rejected', None, io.BytesIO(raw))
    return raw, status, content_type
