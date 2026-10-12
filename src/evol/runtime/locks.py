"""状态根目录的非阻塞操作系统锁。"""
from contextlib import contextmanager
import os
from .models import RuntimeErrorCode

@contextmanager
def worker_lock(root):
    with (root / 'worker.lock').open('a+b') as f:
        if os.fstat(f.fileno()).st_size==0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RuntimeErrorCode('worker_busy', 3) from e
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
