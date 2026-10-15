"""按原始结果散列沿用已复核映射，避免同一输出跨阶段判定漂移。"""
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/local/artifacts/deepseek-comparison-100'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def identity(cid, ref):
    return (cid, ref['group'], ref['repeat'], ref['index'], ref['result_sha256'])


def main():
    previous = {}
    for tag in ['feedback', 'validation']:
        for path in (OUT / 'adjudications' / tag).glob('*.json'):
            data = read(path)
            for item in data['items']:
                previous[identity(data['id'], data['references'][item['key']])] = (tag, item)
    reused = []
    changed = []
    for path in (OUT / 'adjudications/final').glob('*.json'):
        data = read(path)
        dirty = False
        for item in data['items']:
            ref = data['references'][item['key']]
            old = previous.get(identity(data['id'], ref))
            if old is None:
                continue
            tag, reviewed = old
            reused.append({'id': data['id'], 'key': item['key'], 'source_tag': tag})
            if item['issue'] != reviewed['issue']:
                changed.append({'id': data['id'], 'key': item['key'], 'old_issue': item['issue'],
                                'new_issue': reviewed['issue'], 'source_tag': tag})
            item.update({k: v for k, v in reviewed.items() if k != 'key'})
            dirty = True
        if dirty:
            backup = OUT / 'adjudications-before-agent-audit/final' / path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copyfile(path, backup)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    audit_path = OUT / 'agent-audit.json'
    audit = read(audit_path)
    audit['cross_stage_consistency'] = {'reused_count': len(reused), 'reused': reused,
                                      'changed_issue_count': len(changed), 'changes': changed,
                                      'criterion': 'case/group/repeat/finding index/result SHA256 exact match'}
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'reused': len(reused), 'changed': len(changed)}))


if __name__ == '__main__':
    main()
