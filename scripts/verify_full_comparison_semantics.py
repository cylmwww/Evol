"""为语义目录中的关键 Python 行为提供本地最小反例，不执行数据内的任意命令。"""
import hashlib
import io
import json
from pathlib import Path
import pickle
import tempfile

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/local/artifacts/full-comparison-100'


def main():
    checks=[]
    def raises(name,fn,expected):
        try:
            fn()
        except expected as exc:
            checks.append({'mechanism':name,'verified':True,'exception':type(exc).__name__})
        else:
            raise AssertionError(name)
    assert str(3)!=3
    checks.append({'mechanism':'raw_return','verified':True,'example':'str(3) is a string; raw 3 is int'})
    raises('int_input_exceptions',lambda:int('abc'),ValueError)
    raises('float_input_exceptions',lambda:float('abc'),ValueError)
    raises('hash_conversion_exception',lambda:hashlib.sha256('\ud800'.encode()).hexdigest(),UnicodeEncodeError)
    raises('md5_input_exceptions',lambda:hashlib.md5('abc'),TypeError)
    raises('eval_input_exceptions',lambda:eval('1+'),SyntaxError)
    raises('sql_input_exceptions',lambda:'SELECT * FROM users WHERE id='+3,TypeError)
    raises('unused_config_requirement',lambda:{}['API_KEY'],KeyError)
    raises('pickle_input_exceptions',lambda:pickle.loads('abc'),TypeError)
    payload=pickle.dumps(3)
    assert pickle.loads(payload)==3 and str(payload)!=pickle.loads(payload)
    checks.append({'mechanism':'pickle_return_contract','verified':True,'example':'pickle of integer 3 returns int, not textual bytes representation'})
    def before(value):
        try:return int(value)
        except ValueError:return None
    def after(value):
        try:return int(value)
        except Exception:return None
    raises('unexpected_exception_suppressed_before',lambda:before(None),TypeError)
    assert after(None) is None
    checks.append({'mechanism':'unexpected_exception_suppressed_after','verified':True})
    stream=io.StringIO();stream.close()
    raises('stdout_side_effect_failure',lambda:print('x',file=stream),ValueError)
    with tempfile.TemporaryDirectory(dir=OUT) as folder:
        path=Path(folder)/'state.json';path.write_text('valuable existing state')
        def invalid_write():
            with path.open('w') as handle:handle.write(3)
        raises('invalid_write_truncates',invalid_write,TypeError)
        assert path.read_text()==''
        checks.append({'mechanism':'invalid_write_truncates_data_loss','verified':True,'remaining_bytes':0})
    # 受控流证明延迟 flush 的失败，属于机制夹具，不冒充真实磁盘已满故障注入。
    class BrokenRaw(io.RawIOBase):
        def writable(self):return True
        def write(self,data):raise OSError('controlled flush failure')
    buffer=io.BufferedWriter(BrokenRaw())
    assert buffer.write(b'x')==1
    raises('implicit_flush_failure',buffer.flush,OSError)
    try:buffer.close()
    except OSError:pass
    result={'checks':checks,'count':len(checks),'scope':'stdlib counterexamples; flush uses controlled fixture; security reachability and business contracts remain explicit assumptions',
            'not_verified':['actual external command execution','production input trust boundaries','framework specific cookie/redirect contracts','real disk flush failure']}
    (OUT/'semantic-counterexamples.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'verified':len(checks),'all_passed':True}))


if __name__=='__main__':main()
