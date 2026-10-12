"""证据工件先落盘，再在状态事务中登记。"""
import os
from pathlib import Path
from .models import sha, RuntimeErrorCode
from .workspace import safe_path

def write(path,data):
    if os.name=='nt' and len(str(path.absolute()))>=248 and not str(path).startswith('\\\\?\\'):
        path=Path('\\\\?\\'+str(path.absolute()))
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    with temp.open('wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp,path)
    if path.read_bytes()!=data:
        raise RuntimeErrorCode('artifact_integrity_error')
    return sha(data)

def verify(store,root,rid):
    for a in store.rows('SELECT * FROM artifacts WHERE run_id=?',(rid,)):
        try:
            p=safe_path(root/'runs'/rid,a['path'])
            data=p.read_bytes()
        except (OSError,ValueError) as exc:
            raise RuntimeErrorCode('artifact_integrity_error') from exc
        if sha(data)!=a['sha256'] or len(data)!=a['size']:
            raise RuntimeErrorCode('artifact_integrity_error')
