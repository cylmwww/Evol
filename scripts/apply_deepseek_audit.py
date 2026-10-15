"""将执行 agent 已审阅的逐条决策应用到映射，保留原模型判定。"""
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/local/artifacts/deepseek-comparison-100'


def main():
    tag, decision_path = sys.argv[1:]
    decisions = json.loads(Path(decision_path).read_text(encoding='utf-8'))
    index = json.loads((OUT / f'{tag}-review-index.json').read_text(encoding='utf-8'))
    changes = []
    for decision in decisions['changes']:
        row = index[decision['index']]
        source = OUT / 'adjudications' / tag / (row['id'] + '.json')
        backup = OUT / 'adjudications-before-agent-audit' / tag / source.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copyfile(source, backup)
        data = json.loads(source.read_text(encoding='utf-8'))
        item = next(x for x in data['items'] if x['key'] == row['key'])
        changes.append({'id': row['id'], 'key': row['key'], 'old_issue': item['issue'],
                        'new_issue': decision['issue'], 'reason': decision['reason']})
        item.update(issue=decision['issue'], reason=decision['reason'],
                    uncertain=decision.get('uncertain', decision['issue'] is None),
                    out_of_catalogue=decision.get('out_of_catalogue', False))
        source.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    audit_path = OUT / 'agent-audit.json'
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    audit[tag] = {**{k: v for k, v in decisions.items() if k != 'changes'},
                  'findings_in_index': len(index), 'mapping_changes': changes}
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'tag': tag, 'changes': len(changes), 'complete': decisions['complete']}))


if __name__ == '__main__':
    main()
