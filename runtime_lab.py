"""单文件修改与进程崩溃恢复实验，仅使用标准库。"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import uuid


class Conflict(RuntimeError):
    pass


class Busy(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def workspace_lock(root):
    """内核锁随进程退出释放；约束所有使用本工具的写入者。"""
    state = root / '.runtime'
    state.mkdir(exist_ok=True)
    with (state / 'writer.lock').open('a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Busy('工作区已有写入者') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class Runtime:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)

    @contextmanager
    def session(self):
        with workspace_lock(self.root):
            db = sqlite3.connect(self.root / '.runtime' / 'operations.sqlite')
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL,
                    before BLOB NOT NULL, expected BLOB NOT NULL,
                    before_sha TEXT NOT NULL, expected_sha TEXT NOT NULL,
                    old BLOB NOT NULL, new BLOB NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, operation_id TEXT NOT NULL,
                    kind TEXT NOT NULL, detail TEXT NOT NULL,
                    time TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
            ''')
            try:
                yield db
            finally:
                db.close()

    def target(self, relative):
        rel = Path(relative)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts:
            raise ValueError('必须使用工作区内的相对文件路径')
        if rel.parts[0].lower() == '.runtime':
            raise ValueError('禁止修改运行状态目录')
        path = self.root
        for part in rel.parts:
            path = path / part
            if path.is_symlink() or path.is_junction():
                raise ValueError('不支持符号链接或目录联接')
        path.resolve().relative_to(self.root)
        return path

    @staticmethod
    def event(db, op, kind, detail):
        db.execute('INSERT INTO events(operation_id,kind,detail) VALUES (?,?,?)',
                   (op, kind, detail))

    @staticmethod
    def crash(selected, point):
        if selected == point:
            os._exit(77)  # 故意跳过 finally 和正常退出清理。

    def apply(self, relative, old, new, crash=None):
        if not old or old == new:
            raise ValueError('替换内容不能为空，也不能是无变化操作')
        with self.session() as db:
            if db.execute("SELECT 1 FROM operations WHERE status != 'completed'").fetchone():
                raise Conflict('存在未解决操作，请先恢复或处理冲突')
            path = self.target(relative)
            before = path.read_bytes()
            if before.count(old) != 1:
                raise ValueError('原内容必须恰好匹配一次')
            expected = before.replace(old, new, 1)
            op = uuid.uuid4().hex
            with db:
                db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?)',
                           (op, str(path.relative_to(self.root)), before, expected,
                            digest(before), digest(expected), old, new, 'prepared'))
                self.event(db, op, 'prepared', '原始快照、补丁和预期内容已持久化')
            self.crash(crash, 'after_intent')
            row = db.execute('SELECT * FROM operations WHERE id=?', (op,)).fetchone()
            self.write(db, row, crash)
            return op

    def conflict(self, db, row, detail):
        with db:
            db.execute("UPDATE operations SET status='conflict' WHERE id=?", (row['id'],))
            self.event(db, row['id'], 'conflict', detail)
        raise Conflict(detail)

    def complete(self, db, row, kind):
        with db:
            db.execute("UPDATE operations SET status='completed' WHERE id=?", (row['id'],))
            self.event(db, row['id'], kind, '实际文件逐字节等于预期文件')

    def write(self, db, row, crash=None):
        path = self.target(row['path'])
        if path.read_bytes() != row['before']:
            self.conflict(db, row, '写入前原始内容已发生变化')
        fd, name = tempfile.mkstemp(prefix='.evol-', dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(row['expected'])
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        self.crash(crash, 'after_replace')
        if path.read_bytes() != row['expected']:
            self.conflict(db, row, '写入后内容不符合预期')
        self.complete(db, row, 'execution_confirmed')
        self.crash(crash, 'after_result')

    def recover(self):
        recovered = []
        with self.session() as db:
            rows = db.execute("SELECT * FROM operations WHERE status != 'completed' ORDER BY rowid").fetchall()
            for row in rows:
                try:
                    actual = self.target(row['path']).read_bytes()
                except (OSError, ValueError) as exc:
                    self.conflict(db, row, '无法核对目标文件：' + str(exc))
                if actual == row['expected']:
                    self.complete(db, row, 'recovery_confirmed')
                elif actual == row['before']:
                    with db:
                        self.event(db, row['id'], 'retry_authorized', '原始状态仍满足，已持有独占锁')
                    self.write(db, row)
                else:
                    self.conflict(db, row, '实际内容既不是原始内容，也不是预期内容')
                recovered.append(row['id'])
        return recovered

    def inspect(self):
        with self.session() as db:
            return {
                'operations': [dict(r) for r in db.execute(
                    'SELECT id,path,before_sha,expected_sha,status FROM operations ORDER BY rowid')],
                'events': [dict(r) for r in db.execute('SELECT * FROM events ORDER BY sequence')],
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    apply = commands.add_parser('apply')
    apply.add_argument('path')
    apply.add_argument('--old', required=True)
    apply.add_argument('--new', required=True)
    apply.add_argument('--crash', choices=['after_intent', 'after_replace', 'after_result'])
    commands.add_parser('recover')
    commands.add_parser('inspect')
    args = parser.parse_args()
    runtime = Runtime(args.workspace)
    try:
        if args.command == 'apply':
            result = runtime.apply(args.path, args.old.encode('utf-8'), args.new.encode('utf-8'), args.crash)
        elif args.command == 'recover':
            result = runtime.recover()
        else:
            result = runtime.inspect()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (Conflict, Busy, ValueError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
