"""仅在完整运行与执行 agent 复核完成后，生成 DeepSeek 专属报告。"""
from collections import Counter
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.run_deepseek_comparison import OUT, PREVIOUS, experiment as exp


def main():
    manifest=exp.read(OUT/'manifest.json');summary=exp.read(OUT/'summary.json')
    audit=exp.read(OUT/'agent-audit.json')
    assert audit.get('final',{}).get('complete'), '尚未完成执行 agent 的最终映射审阅'
    results=[exp.scored(cid,g,r,'final') for ids in manifest['splits'].values() for cid in ids for g in exp.ARMS for r in range(1,4)]
    assert len(results)==900
    test=[x for x in results if x['id'] in manifest['splits']['test']]
    by_key={(r['id'],r['group'],r['repeat']):r for r in test}
    common={(cid,r) for cid in manifest['splits']['test'] for r in range(1,4)
            if all(by_key[cid,g,r]['status']=='complete' for g in exp.ARMS)}
    summary['paired_strict_complete_test']={g:exp.aggregate([r for r in test if r['group']==g and (r['id'],r['repeat']) in common]) for g in exp.ARMS}
    summary['paired_strict_complete_keys']=sorted(common)
    calls=[exp.read(p) for p in OUT.glob('**/call-*-meta.json')]
    summary['all_experiment_usage']={'recorded_attempts':len(calls),
        'known_input_tokens':sum(c.get('usage',{}).get('prompt_tokens',0) for c in calls),
        'known_output_tokens':sum(c.get('usage',{}).get('completion_tokens',0) for c in calls),
        'unknown_usage_attempts':sum(not c.get('usage_known') for c in calls)}
    feedback=exp.read(OUT/'feedback.json');candidate=exp.read(OUT/'candidate.json')
    summary['feedback_usage']={'reviewed_cases':len({x['case_id'] for x in feedback}),
        'records':len(feedback),'categories':dict(Counter(x['category'] for x in feedback)),
        'used_records':len(candidate['used_feedback_ids']),
        'used_cases':len({x['case_id'] for x in feedback if x['id'] in candidate['used_feedback_ids']}),
        'learned_rules':candidate['learned_rule_ids'],'removed_rules':candidate['removed_rule_ids']}
    old=exp.read(OUT/'previous-files-sha256.json')
    assert all((PREVIOUS/p).exists() and exp.digest((PREVIOUS/p).read_bytes())==h for p,h in old.items())
    summary['previous_preservation']={'files_verified':len(old),'changed':0}
    exp.save(OUT/'summary.json',summary)
    names={'single':'单 Agent','multi':'多 Agent','evolved':'多 Agent 自进化流程'}
    def table(values):
        lines=['| 组别 | 次数 | TP / FP / FN | Precision | Recall | F1 | 最终流程完成 |',
               '|---|---:|---|---:|---:|---:|---:|']
        for g,m in values.items():
            lines.append(f"| {names[g]} | {m['n']} | {m['tp']} / {m['fp']} / {m['fn']} | {m['precision']:.1%} | {m['recall']:.1%} | {m['f1']:.1%} | {m['final_workflow_completed']} / {m['n']} |")
        return lines
    chosen=summary['selection']['selected']
    lines=['# DeepSeek：100 条三组各三次的复核结果','','日期：2026-10-14。主体 900 次均有终态，失败和部分完成也计入，不代表 900 次均成功。',
        '',f'本轮模型 deepseek-flash，独立目录，不复用 GPT 输出或候选。GPT 历史目录 {len(old)} 个文件散列全部保持不变。',
        '',f'验证选择：{"采用本轮生成的候选" if chosen=="candidate" else "候选未超过基线，按冻结规则回退基础 Skill；候选验证结果与成本仍保留"}。没有生产激活。',
        '', '本轮最终状态：895 次严格完整、3 次部分完成、2 次失败；后两次为充值前的限流失败，均保留。若选择回退，第三组与多 Agent 使用相同基础 Skill，其分数差异不能解释为进化收益。',
        '', '## 主要结果：40 条最终测试集','',
        '每组 40×3=120 次，39 个冻结语义目标重复三次，共 117 个目标。主要机制一对一匹配、重复合并、证据不足保守计 FP。全部计划试次保留，技术失败的未报告目标仍计 FN。','',*table(summary['scopes']['test']),'']
    for repeat,values in summary['per_repeat'].items():
        lines.extend([f'## 第 {repeat} 次测试','',*table(values),''])
    lines.extend(['## 测试前的候选选择','',
        '以下是决定是否采用学习候选的原始验证结果；被拒候选仍保留。后面的验证集汇总使用最终选定版本，回退时两者并非同一批自进化试次。',
        '',*table(summary['selection']['validation']),''])
    lines.extend(['## 三组共同严格完整的配对子集','',
        '辅助分析，只保留同一案例、同一重复的三组均严格完整的记录；存在成功样本选择偏差，不替代全量主表。','',*table(summary['paired_strict_complete_test']),''])
    for scope,label in [('feedback','反馈 40 条回测'),('validation','验证 20 条'),('all','全部 100 条')]:
        lines.extend([f'## {label}','',*table(summary['scopes'][scope]),''])
    lines.extend(['## 反馈与候选','',
        f"40 条产生 {len(feedback)} 条反馈记录；构造器实际使用 {summary['feedback_usage']['used_records']} 条，涉及 {summary['feedback_usage']['used_cases']} 个案例。",
        '', '新增：'+(', '.join(candidate['learned_rule_ids']) or '无')+'。',
        '删除：'+(', '.join(candidate['removed_rule_ids']) or '无')+'。','',
        '原生确定性构造器只将漏报模式、规则 ID 和严重性编译成学习块；误报只删除已有同 ID 学习块，不能宣称具备通用误报学习。候选和配置在测试前冻结，三次重复不是三次独立进化搜索。',
        '', '## 成本和完成状态','',
        '| 组别（全部 100 条） | 请求 | 已知输入 Token | 已知输出 Token | 未知用量请求 | 平均秒/次 | 审计状态 |',
        '|---|---:|---:|---:|---:|---:|---|'])
    for g,m in summary['scopes']['all'].items():
        lines.append(f"| {names[g]} | {m['model_calls']} | {m['input_tokens']} | {m['output_tokens']} | {m['unknown_usage_calls']} | {m['mean_seconds']:.1f} | {m['status_counts']} |")
    usage=summary['all_experiment_usage']
    lines.extend(['',f"含接口预检、反馈/验证复核、最终复核、复核重试及被拒候选在内，记录 {usage['recorded_attempts']} 次传输尝试；已知输入 {usage['known_input_tokens']}、输出 {usage['known_output_tokens']} Token，未知用量 {usage['unknown_usage_attempts']} 次。不将未知用量视为零费用，不推算金额。",
        '', '## 复核与适用边界','',
        '- 冻结目录是 115 个约定的语义机制目标，不是完整独立生产缺陷真值。安全可达性、库绑定和行为兼容契约列明前提。',
        '- 同配置模型通过独立、去来源、随机顺序请求做语义映射，执行 agent 审阅关键判定并保存审计。不是人类验收，也不是不同模型的独立复核；具体范围、修正和目录外观察见 agent-audit.json。',
        '- 合成 100 条只有 28 个归一化模板，模板不跨集合，但集合内部有重复；历史已经查看过部分案例，不宣称历史盲测。三次运行不增加独立样本数。',
        '- 反馈/验证/测试为 40/20/40 条，目标数 36/40/39，无目标样本数 16/0/11。验证没有无目标样本，误报抑制的选择证据有限。',
        '- 单 Agent 无 scanner、多 Agent 有 scanner；单组为 4k 输出直调，多组每角色 32k/180 秒。比较的是完整系统，不能把全部差异归因于 Agent 数量。',
        '- 复合发现每条只计一个主要身份，重复身份合并。目录外发现另列，不修改冻结分母。保守 FP 不等于已经证实内容错误。',
        '- 14 项边界测试通过；首次测试临时目录权限错误已修正。本轮另以安全常量检查 eval 前置可达性。机制检查不是本次真实模型效果的替代。',
        '', '## 本地证据','',
        '- artifacts/deepseek-comparison-100/manifest.json、deepseek-protocol.json、truth.json：配置、方法、语义目录。',
        '- runs/、adjudications/、judge-calls/、judge-attempts/：逐试次结果、复核映射、原始响应和重试。',
        '- feedback.json、candidate.json、selection.json：新一轮反馈、候选及测试前选择。',
        '- summary.json、final-review-index.json、agent-audit.json：复核指标与审阅证据。',
        '- previous-files-sha256.json：GPT 历史结果保留证明。','',
        '新增独立实验入口、报告脚本、测试和本地档案。未修改原始数据、生产 Skills、数据库或密钥配置，未执行 Git 提交或推送。'])
    destination=ROOT/'docs/local/DeepSeek100条三组语义复核结果.md'
    destination.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'test':summary['scopes']['test'],'selected':chosen,'report':str(destination)},ensure_ascii=False))


if __name__=='__main__':main()
