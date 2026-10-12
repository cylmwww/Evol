"""使用真实子进程退出验证恢复，不以模拟异常代替崩溃。"""
from pathlib import Path
import subprocess
import sys
import shutil
import uuid
import unittest

from runtime_lab import Runtime, Conflict, workspace_lock


SCRIPT = Path(__file__).with_name('runtime_lab.py')
BEFORE = b'{\r\n  "timeout": 30,\r\n  "other": "unchanged"\r\n}\r\n'
EXPECTED = BEFORE.replace(b'30', b'60')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        base = SCRIPT.parent / 'docs' / 'local' / 'artifacts'
        base.mkdir(parents=True, exist_ok=True)
        self.root = base / ('test-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.root.resolve().relative_to(base.resolve())
        self.addCleanup(shutil.rmtree, self.root)
        self.file = self.root / 'config.json'
        self.file.write_bytes(BEFORE)
        self.runtime = Runtime(self.root)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--workspace', str(self.root), *args],
                              capture_output=True, timeout=15)

    def crash(self, point):
        result = self.cli('apply', 'config.json', '--old', '30', '--new', '60', '--crash', point)
        self.assertEqual(result.returncode, 77, result.stderr)

    def verify_recovery(self, point, event):
        self.crash(point)
        self.assertEqual(self.file.read_bytes(), BEFORE if point == 'after_intent' else EXPECTED)
        result = self.cli('recover')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.file.read_bytes(), EXPECTED)
        state = self.runtime.inspect()
        self.assertEqual(state['operations'][0]['status'], 'completed')
        self.assertIn(event, [e['kind'] for e in state['events']])
        self.assertEqual(self.cli('recover').returncode, 0)
        self.assertEqual(self.runtime.inspect(), state)
        self.assertEqual(self.file.read_bytes(), EXPECTED)

    def test_crash_after_intent(self):
        self.verify_recovery('after_intent', 'retry_authorized')

    def test_crash_after_replace(self):
        self.verify_recovery('after_replace', 'recovery_confirmed')

    def test_crash_after_result(self):
        self.verify_recovery('after_result', 'execution_confirmed')

    def test_normal_execution(self):
        self.runtime.apply('config.json', b'30', b'60')
        self.assertEqual(self.file.read_bytes(), EXPECTED)

    def test_conflict_preserves_actual_file(self):
        self.crash('after_replace')
        changed = EXPECTED + b'external change'
        self.file.write_bytes(changed)
        with self.assertRaises(Conflict):
            self.runtime.recover()
        self.assertEqual(self.file.read_bytes(), changed)
        self.assertEqual(self.runtime.inspect()['operations'][0]['status'], 'conflict')
        with self.assertRaises(Conflict):
            self.runtime.apply('config.json', b'60', b'90')

    def test_missing_target_is_conflict(self):
        self.crash('after_intent')
        self.file.unlink()
        with self.assertRaises(Conflict):
            self.runtime.recover()
        self.assertFalse(self.file.exists())

    def test_non_unique_patch_rejected(self):
        self.file.write_bytes(b'30 30')
        with self.assertRaises(ValueError):
            self.runtime.apply('config.json', b'30', b'60')
        self.assertEqual(self.file.read_bytes(), b'30 30')
        self.assertEqual(self.runtime.inspect()['operations'], [])

    def test_pending_operation_blocks_new_write(self):
        self.crash('after_intent')
        with self.assertRaises(Conflict):
            self.runtime.apply('config.json', b'30', b'90')
        self.assertEqual(self.file.read_bytes(), BEFORE)

    def test_lock_excludes_other_process(self):
        with workspace_lock(self.root):
            result = self.cli('apply', 'config.json', '--old', '30', '--new', '60')
            self.assertEqual(result.returncode, 2)
        self.assertEqual(self.file.read_bytes(), BEFORE)

    def test_reject_escape_and_state_paths(self):
        for name in ['../outside', '.runtime/writer.lock']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.runtime.apply(name, b'30', b'60')


if __name__ == '__main__':
    unittest.main(verbosity=2)
