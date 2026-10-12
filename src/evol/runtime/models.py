"""冻结契约和状态。"""
import hashlib
import json
import math
from pathlib import PureWindowsPath

VERSION = '0.1.0'
TOOLS = {n: '1' for n in ('read_file', 'replace_exact', 'assert_file_sha256')}
TERMINAL = {'completed', 'cancelled', 'budget_exhausted', 'failed', 'conflict'}
TRANSITIONS = {
    'queued': {'running', 'stopping'},
    'running': {'validating', 'stopping', 'recovering'},
    'validating': {'completed', 'stopping', 'recovering'},
    'recovering': {'running', 'validating', 'stopping'},
    'stopping': {'cancelled', 'budget_exhausted', 'failed', 'conflict'},
    **{s: set() for s in TERMINAL},
}
DEFAULTS = dict(max_steps=16, max_dispatches=32, max_attempts_per_step=2,
                max_wall_seconds=60, retry_delay_ms=200, max_file_bytes=1048576,
                max_input_bytes=8388608, max_files=32)
class RuntimeErrorCode(Exception):
    def __init__(self, message, code=7):
        super().__init__(message)
        self.code = code

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def hashed(value):
    return sha(canonical(value).encode())

def relative(path):
    if not isinstance(path, str) or not path or '\\' in path:
        raise ValueError('路径须为非空的正斜线相对路径')
    p = PureWindowsPath(path)
    parts = path.split('/')
    if p.drive or p.root or any(x in ('', '.', '..') or any(ord(c)<32 or c in ':<>"|?*' for c in x) or x.endswith((' ', '.')) for x in parts):
        raise ValueError('非法相对路径')
    if any(PureWindowsPath(x).is_reserved() for x in parts):
        raise ValueError('保留设备路径')
    return path

def validate(plan, overrides=None):
    policy = DEFAULTS | (overrides or {})
    if set(policy) != set(DEFAULTS):
        raise ValueError('未知策略字段')
    for key, value in policy.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('策略必须为有限正数')
        if key != 'max_wall_seconds' and not isinstance(value, int):
            raise ValueError('计数策略必须为整数')
    if not isinstance(plan, dict) or set(plan) != {'schema_version', 'name', 'inputs', 'steps'} or type(plan['schema_version']) is not int or plan['schema_version'] != 1:
        raise ValueError('计划字段或版本非法')
    if not isinstance(plan['name'], str) or not plan['name']:
        raise ValueError('缺少计划名称')
    inputs, steps = plan['inputs'], plan['steps']
    if not isinstance(inputs, list) or not 0 < len(inputs) <= policy['max_files']:
        raise ValueError('输入数量超限')
    for path in inputs:
        relative(path)
    if len({p.casefold() for p in inputs}) != len(inputs):
        raise ValueError('输入路径重复')
    if not isinstance(steps, list) or not 0 < len(steps) <= policy['max_steps']:
        raise ValueError('步骤数量超限')
    seen = {}
    for s in steps:
        if not isinstance(s, dict) or set(s) != {'id', 'tool', 'args'} or not isinstance(s['id'], str) or not s['id'] or s['id'] in seen:
            raise ValueError('非法步骤或重复 id')
        if not isinstance(s['tool'],str) or s['tool'] not in TOOLS:
            raise ValueError('未知工具')
        args = s['args']
        fields = {'path'} | ({'old_utf8', 'new_utf8'} if s['tool'] == 'replace_exact' else {'expected_from'} if s['tool'] == 'assert_file_sha256' else set())
        if not isinstance(args, dict) or set(args) != fields or not all(isinstance(v, str) for v in args.values()):
            raise ValueError('非法工具参数')
        if relative(args['path']) not in inputs:
            raise ValueError('路径未声明')
        if s['tool'] == 'replace_exact' and (not args['old_utf8'] or args['old_utf8'] == args['new_utf8']):
            raise ValueError('空替换或无变化替换')
        if s['tool'] == 'assert_file_sha256':
            ref = seen.get(args['expected_from'])
            if not ref or ref['tool'] != 'replace_exact' or ref['args']['path'] != args['path']:
                raise ValueError('非法验证引用')
        seen[s['id']] = s
    return policy
