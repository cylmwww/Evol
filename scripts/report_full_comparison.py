"""基于落盘的语义复核映射输出中文实验报告和配对完整子集指标。"""
from collections import Counter
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.run_full_comparison import OUT, read, save, scored, aggregate, ARMS


def main():
    manifest=read(OUT/'manifest.json');summary=read(OUT/'summary.json')
    assert read(OUT/'agent-audit.json').get('final',{}).get('complete'), '最终语义映射尚未完成执行 agent 审阅'
    rows=[scored(cid,g,repeat,'final') for ids in manifest['splits'].values() for cid in ids for g in ARMS for repeat in range(1,4)]
    assert len(rows)==900
    test=[r for r in rows if r['id'] in manifest['splits']['test']]
    common={(r['id'],r['repeat']) for r in test if all(
        next(q for q in test if q['id']==r['id'] and q['repeat']==r['repeat'] and q['group']==g)['status']=='complete' for g in ARMS)}
    summary['paired_strict_complete_test']={g:aggregate([r for r in test if r['group']==g and (r['id'],r['repeat']) in common]) for g in ARMS}
    summary['paired_strict_complete_keys']=sorted(common)
    summary['duplicate_penalized_test']={g:aggregate([{**r,'fp':r['fp']+r['duplicates']} for r in test if r['group']==g]) for g in ARMS}
    all_calls=[read(p) for p in OUT.glob('**/call-*-meta.json')]
    summary['all_experiment_cost']={'calls_including_judge_and_rejected_candidates':len(all_calls),
        'known_input_tokens':sum(c.get('usage',{}).get('prompt_tokens',0) for c in all_calls),
        'known_output_tokens':sum(c.get('usage',{}).get('completion_tokens',0) for c in all_calls),
        'unknown_usage_calls':sum(not c.get('usage_known') for c in all_calls),
        'errors':dict(Counter(c.get('error') for c in all_calls if c.get('error')))}
    mutation=read(OUT/'candidate.json');feedback=read(OUT/'feedback.json')
    summary['feedback_usage']={'reviewed_cases':len({f['case_id'] for f in feedback}),
        'feedback_records':len(feedback),'categories':dict(Counter(f['category'] for f in feedback)),
        'used_feedback_records':len(mutation['used_feedback_ids']),
        'used_cases':len({f['case_id'] for f in feedback if f['id'] in mutation['used_feedback_ids']}),
        'learned_rule_ids':mutation['learned_rule_ids'],'removed_rule_ids':mutation['removed_rule_ids']}
    from scripts import audit_gpt_timeouts
    audit_gpt_timeouts.main()
    summary['read_timeouts']=read(OUT/'read-timeout-audit.json')
    save(OUT/'summary.json',summary)
    names={'single':'单 Agent','multi':'多 Agent','evolved':'多 Agent 自进化流程'}
    def table(values):
        lines=['| 组别 | 次数 | TP / FP / FN | Precision | Recall | F1 | 完整流程 |',
               '|---|---:|---|---:|---:|---:|---:|']
        for g,m in values.items():
            lines.append(f"| {names[g]} | {m['n']} | {m['tp']} / {m['fp']} / {m['fn']} | {m['precision']:.1%} | {m['recall']:.1%} | {m['f1']:.1%} | {m['final_workflow_completed']} / {m['n']} |")
        return lines
    selection=summary['selection']['selected']
    lines=['# 100 条三组各三次：语义复核结果','','日期：2026-10-14。主体 900 次计划运行均有终态记录；失败/部分完成也保留，并非 900 次均成功。',
           '',f'验证选择：{"采用唯一候选 Skill" if selection=="candidate" else "候选未超过基线，按冻结规则回退基础 Skill"}。候选未在生产环境激活。',
           '', '## 主要结果：40 条最终测试集', '',
           '每组 40×3=120 次，冻结目标每轮 39 个、三轮共 117 个。按问题主要机制匹配，重复身份合并，证据不足的报告保守计 FP。失败运行未报告的目标仍计 FN。','',
           *table(summary['scopes']['test']), '', '## 三次分别的测试指标','']
    amendment=OUT/'protocol-amendment-03-aispot.json'
    if amendment.exists():
        lines[3:3]=['', '本轮从原有 217 次 Joye 记录续跑，后续由 Aispot 提供同名 gpt-6.1-sol；模型别名相同不证明上游模型、路由和行为完全一致。40 条最终测试此前未运行，全部测试使用 Aispot。服务商切换是实验限制，不能将整体汇总称为单一端点实验。',
                    '', '续跑使用 2 个任务、最多 2 个同时在途请求；120 秒请求超时、每角色 32k/180 秒不变。已有终态包括技术失败全部保留，只补缺失试次。']
    if (OUT/'protocol-amendment-05-timeout-180s.json').exists():
        lines[3:3]=['', '用户在245次终态后授权把单请求等待上限由120秒改为180秒；角色32k/180秒、300秒请求准入上限及16次请求上限不变。此前结果保留，延迟/失败统计包含两种请求上限。']
    if (OUT/'protocol-amendment-06-admission-600s.json').exists():
        lines[3:3]=['', '用户在283次终态后授权把整次流程的请求准入上限由300秒改为600秒，单请求等待180秒、角色32k/180秒、请求数量上限16次保持不变。600秒决定能否发起下一请求，并不是整次流程的强制终止时间。所有此前结果继续保留。']
    lines.extend(['## 测试前的候选选择','',
                  '以下是候选选择所用的验证结果；若候选被拒，后面最终版本的验证回测与这批候选试次不同。',
                  '',*table(summary['selection']['validation']),''])
    for repeat,values in summary['per_repeat'].items():
        lines.extend([f'### 第 {repeat} 次','',*table(values),''])
    lines.extend(['## 三组共同严格成功的配对测试子集','',
                  '这是辅助结果，会受成功样本选择影响，不能替代全量主表。严格成功还要求所有请求均成功；重试恢复的最终流程完成单独统计。','',
                  *table(summary['paired_strict_complete_test']),''])
    for scope,label in [('feedback','40 条反馈样本回测'),('validation','20 条验证样本'),('all','100 条全部样本')]:
        lines.extend([f'## {label}','',*table(summary['scopes'][scope]),''])
    lines.extend(['## 进化实际学到的内容','',
        f"40 条反馈样本产生 {len(feedback)} 条复核记录；原生构造器实际使用 {summary['feedback_usage']['used_feedback_records']} 条、涉及 {summary['feedback_usage']['used_cases']} 个案例。",
        '', '新增规则：'+(', '.join(mutation['learned_rule_ids']) or '无')+'。',
        '删除规则：'+(', '.join(mutation['removed_rule_ids']) or '无')+'。','',
        '构造器仅将漏报的单行模式和规则标识转成学习块；误报只删除已有同 ID 学习块，不会自动生成通用抑制规则。40 条反馈不等于 40 条有效学习。三次重复使用同一个冻结版本。',
        '', '## 成本与可靠性','',
        '| 组别（全 100 条） | 请求 | 已知输入 Token | 已知输出 Token | 用量未知请求 | 平均秒/次 | 状态计数 |',
        '|---|---:|---:|---:|---:|---:|---|'])
    for g,m in summary['scopes']['all'].items():
        lines.append(f"| {names[g]} | {m['model_calls']} | {m['input_tokens']} | {m['output_tokens']} | {m['unknown_usage_calls']} | {m['mean_seconds']:.1f} | {m['status_counts']} |")
    cost=summary['all_experiment_cost']
    timeout=summary['read_timeouts']['all']
    lines.extend(['', '## 读取超时统计','',
        f"主体中明确的读取超时调用 {timeout['read_timeout_calls']} 次，涉及 {timeout['trials_with_read_timeout']} 个试次。终态错误直接为读取超时 {timeout['terminal_error_is_read_timeout']} 次；超时出现后最终流程未完成 {timeout['incomplete_after_read_timeout']} 次；出现超时但最终恢复完成 {timeout['completed_after_read_timeout']} 次。",
        '', '“超时后未完成”不证明超时是唯一原因；其他阶段预算耗尽或汇总失败可能同时发生。普通超时另列。复核请求、稳定性测试和中断调用不纳入主体分母；详情及补跑候选见read-timeout-audit.json。'])
    lines.extend(['', f"包括复核、接口预检及被拒候选在内，共 {cost['calls_including_judge_and_rejected_candidates']} 次已记录调用；已知输入 {cost['known_input_tokens']}、输出 {cost['known_output_tokens']} Token，用量未知 {cost['unknown_usage_calls']} 次。供应商计价未知，不推算金额。",
        '', '## 复核方法和限制','',
        '- 使用预先冻结的 115 个语义目标。16 个 Python 最小行为反例通过本地检查；flush 失败是受控夹具，安全可达性和业务契约仍是明确假设。',
        '- 由独立请求中的同配置模型做去来源、随机顺序的语义映射，执行 agent 审阅关键判定。不是人类复核，不是独立模型评审；相关模型偏差仍存在。具体审阅范围见 agent-audit.json。',
        '- 数据为合成短 diff，100 条只含 28 个归一化模板；模板在反馈/验证/测试之间隔离，同一集合内部仍有重复，不能把重复三次当新增独立样本。部分案例历史上已经被看过，不宣称历史盲测。',
        '- 反馈/验证/测试分别含 36/40/39 个语义目标，无目标样本数为 16/0/11。验证集没有无目标样本，误报抑制的版本选择证据有限。',
        '- 单 Agent 无 scanner，多 Agent 有 scanner，原生总成本不同；多组每角色 32k/180 秒，单组为 4k 输出上限的直调路径。只能比较当前三套完整配置，不能把差异都归因于 Agent 数量。',
        '- 主表是约定的语义机制覆盖，不是生产缺陷准确率。目录外发现单列且不改分母；四组/三组共同遗漏以及未列行为仍可能存在。',
        '- 技术失败保留在主指标；三组共同成功的子集只是辅助分析。所有原始记录均保留，未挑选最佳重复。',
        '- 验证过程中因 HTTP 429 将任务并发 8 降至 4、HTTP 并发 16 降至 8，旧配置及已有 217 次记录在 amendment-02 中保留。最终测试三组均使用较低并发；反馈/验证延迟含不同并发环境。',
        '', '## 本地证据与文件边界','',
        '- artifacts/full-comparison-100/manifest.json、truth.json：冻结配置、划分和语义目录。',
        '- artifacts/full-comparison-100/runs/：900 次主体运行及原始请求/响应和终态。',
        '- artifacts/full-comparison-100/adjudications/、final-review-index.json、agent-audit.json：语义映射和审阅证据。',
        '- artifacts/full-comparison-100/feedback.json、candidate.json、selection.json：反馈、唯一候选和测试前的选择。',
        '- artifacts/full-comparison-100/summary.json：全部/分集合/分重复和共同成功子集指标。',
        '', '仅新增实验脚本、对应边界测试及 docs/local 本地材料；未覆盖生产 Skills、数据集、数据库或环境配置，未执行 Git 提交或推送。'])
    (ROOT/'docs/local/100条三组语义复核结果.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'test':summary['scopes']['test'],'selection':selection,'feedback':summary['feedback_usage']},ensure_ascii=False))


if __name__=='__main__':main()
