"""受限文件工具：工具上下文不包含数据库连接。"""
from dataclasses import dataclass
import json
import os
import stat
from .registry import register
from evol.runtime.models import sha, hashed
from evol.runtime.workspace import read, safe_path

class ContentConflict(Exception):
    pass
class VerificationFailed(Exception):
    pass
class StopBeforeEffect(Exception):
    pass
@dataclass(frozen=True)
class Context:
    candidate: object
    max_file_bytes: int
    expected_files: dict
    references: dict
    stop_check: object
    hook: object

class FileTool:
    version = '1'
    def validate(self, args, ctx):
        if args['path'] not in ctx.expected_files:
            raise ValueError('undeclared_path')
        safe_path(ctx.candidate,args['path'])
        return args
    def prepare(self, ctx, args):
        self.validate(args,ctx)
        before = read(ctx.candidate,args['path'],ctx.max_file_bytes)
        if sha(before) != ctx.expected_files[args['path']]:
            raise ContentConflict('file_drift')
        expected = before
        if self.name=='replace_exact':
            old, new = args['old_utf8'].encode(), args['new_utf8'].encode()
            if before.count(old)!=1:
                raise ValueError('replacement_not_unique')
            expected=before.replace(old,new,1)
            if len(expected)>ctx.max_file_bytes:
                raise ValueError('output_size_limit')
        if self.name=='assert_file_sha256' and sha(before)!=ctx.references[args['expected_from']]:
            raise VerificationFailed('assertion_failed')
        intent=dict(tool=self.name,version=self.version,args_hash=hashed(args),path=args['path'],before_sha=sha(before),expected_sha=sha(expected),effect_kind=self.kind)
        return intent,before,expected
    def execute(self, ctx, operation):
        intent=json.loads(operation['intent_json'])
        path=safe_path(ctx.candidate,intent['path'])
        actual=read(ctx.candidate,intent['path'],ctx.max_file_bytes)
        if actual!=operation['before_blob']:
            raise ContentConflict('precondition_changed')
        if self.kind=='write':
            temp=path.parent/('.evol-'+operation['operation_id']+'.tmp')
            with temp.open('wb') as f:
                f.write(operation['expected_blob'])
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp,stat.S_IMODE(path.stat().st_mode))
            ctx.hook('temp_written',intent['path'])
            if ctx.stop_check():
                raise StopBeforeEffect()
            if read(ctx.candidate,intent['path'],ctx.max_file_bytes)!=operation['before_blob']:
                raise ContentConflict('pre_replace_drift')
            ctx.hook('before_replace',intent['path'])
            os.replace(temp,path)
            ctx.hook('after_replace',intent['path'])
        actual=read(ctx.candidate,intent['path'],ctx.max_file_bytes)
        if actual!=operation['expected_blob']:
            raise ContentConflict('effect_mismatch')
        return actual
    def reconcile(self, ctx, operation):
        intent=json.loads(operation['intent_json'])
        try:
            actual=read(ctx.candidate,intent['path'],ctx.max_file_bytes)
        except FileNotFoundError:
            return 'CONFLICT',None
        except ValueError:
            return 'CONFLICT',None
        except OSError:
            return 'UNKNOWN',None
        if self.kind!='write':
            # 未提交的读取证据不能仅凭文件存在推定为已完成调用。
            return ('NOT_APPLIED',actual) if actual==operation['before_blob'] else ('CONFLICT',actual)
        if actual==operation['expected_blob']:
            return 'APPLIED',actual
        if actual==operation['before_blob']:
            return 'NOT_APPLIED',actual
        return 'CONFLICT',actual

@register
class ReadFile(FileTool):
    name='read_file'
    kind='read'
@register
class ReplaceExact(FileTool):
    name='replace_exact'
    kind='write'
@register
class AssertFileSha256(FileTool):
    name='assert_file_sha256'
    kind='assert'
