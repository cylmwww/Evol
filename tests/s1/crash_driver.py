"""测试专用独立进程入口；生产 CLI 不暴露故障点。"""
import json
import os
from pathlib import Path
import sys
import time
from evol.runtime.service import Runtime
root,rid,point,step,action=sys.argv[1:]
barrier=Path(root)/'test-barrier'
def hook(where,detail=None):
    selected=detail.get('step') if isinstance(detail,dict) else None
    if where==point and (step=='*' or selected==step):
        if action=='crash':
            os._exit(77)
        if action=='wait':
            barrier.write_text(json.dumps({'point':where,'step':selected}))
            while not barrier.with_suffix('.release').exists():
                time.sleep(.01)
        if action=='cancel_crash':
            Runtime(root).cancel(rid)
            os._exit(77)
r=Runtime(root,hook=hook)
value=r.resume(rid)
print(json.dumps(value))
raise SystemExit({'failed':4,'conflict':4,'cancelled':5,'budget_exhausted':6,'recovering':8,'stopping':8}.get(value['status'],0))
