"""后台顺序执行阶段，语义审阅和版本选择交由当前会话完成。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/local/artifacts/full-comparison-100'


def write_state(**fields):
    path = OUT / 'background-state.json'
    value = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    value.update(fields, updated_at=datetime.now(timezone.utc).isoformat())
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['validation', 'remaining'])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    lock = open(OUT / 'background.lock', 'a+b')
    lock.seek(0)
    if os.name == 'nt':
        import msvcrt
        if not lock.read(1):
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    stages = ['validation-runs', 'validation-review'] if args.phase == 'validation' else ['remaining-runs', 'final-review']
    write_state(status='running', phase=args.phase, driver_pid=os.getpid(), child_pid=None)
    try:
        for stage in stages:
            write_state(stage=stage)
            log = OUT / ('background-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + stage + '.log')
            with open(log, 'wb') as handle:
                child = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'scripts/resume_gpt_comparison_600s.py'), stage],
                                         cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                write_state(child_pid=child.pid, log=str(log.relative_to(OUT)))
                code = child.wait()
            write_state(child_pid=None, last_exit_code=code)
            if code:
                write_state(status='needs_attention', failed_stage=stage)
                return
        write_state(status='needs_validation_audit' if args.phase == 'validation' else 'needs_final_audit', stage=stages[-1])
    finally:
        if os.name == 'nt':
            lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()


if __name__ == '__main__':
    main()
