"""仅访问普通文件，禁止链接及重解析点。"""
import os
import stat
from pathlib import Path
from .models import relative, sha

def safe_path(root, name):
    relative(name)
    root = Path(root).absolute()
    for p in [*reversed(root.parents), root, *[root.joinpath(*name.split('/')[:i]) for i in range(1, len(name.split('/')) + 1)]]:
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
            raise ValueError('拒绝链接或重解析点')
    target = root / name
    if os.name=='nt' and len(str(target))>=248 and not str(target).startswith('\\\\?\\'):
        target=Path('\\\\?\\'+str(target))
    if not target.is_file():
        raise ValueError('必须为普通文件')
    return target

def read(root, name, limit):
    p = safe_path(root, name)
    with p.open('rb') as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError('文件大小超限')
    return data

def snapshot(source, candidate, inputs, policy, hook):
    manifest = []
    total = 0
    for name in inputs:
        path = safe_path(source, name)
        before = path.stat()
        data = read(source, name, policy['max_file_bytes'])
        target = candidate / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hook('copy_read', name)
        after = path.stat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or read(source, name, policy['max_file_bytes']) != data or target.read_bytes() != data:
            raise ValueError('输入复制期间发生变化')
        total += len(data)
        if total > policy['max_input_bytes']:
            raise ValueError('输入总大小超限')
        manifest.append(dict(path=name, size=len(data), sha256=sha(data)))
    return manifest
