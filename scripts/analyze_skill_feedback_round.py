"""Manually adjudicated fixed-truth comparison; primary findings matched one-to-one."""
import json
from pathlib import Path
from scripts.analyze_protocol_pilot import audit_result

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'docs/local/artifacts/skill-feedback-round-1'
PRIOR = ROOT/'docs/local/artifacts/model-comparison-gpt61-32k'
# Each tuple contains static and evolved finding identities in result order.
# None is an unsupported published assertion, retained as FP conservatively.
MAP = {
 'pr-0011':(['eval_execution','eval_input_exceptions','raw_return'],['eval_execution','eval_input_exceptions','raw_return']),
 'pr-0012':(['raw_return'],['raw_return',None]),
 'pr-0020':([None],[]),
 'pr-0024':(['raw_return','assert_removed'],[None,'raw_return','assert_removed']),
 'pr-0033':(['unexpected_exception_suppressed'],['unexpected_exception_suppressed']),
 'pr-0037':(['hash_conversion_exception'],['hash_conversion_exception']),
 'pr-0038':([None],['hash_conversion_exception']),
 'pr-0041':(['shell_execution','unbounded_wait','raw_return'],['shell_execution','subprocess_input_exceptions','unbounded_wait']),
 'pr-0042':(['sql_predicate_injection','sql_input_exceptions'],['sql_predicate_injection','sql_input_exceptions']),
 'pr-0046':([],[None]),
 'pr-0047':([None],[None]),
 'pr-0054':(['raw_return'],['raw_return']),
 'pr-0055':(['int_input_exceptions'],['int_input_exceptions']),
 'pr-0060':(['int_input_exceptions'],['int_input_exceptions']),
 'pr-0062':(['file_read_replaces_normalization'],['file_read_replaces_normalization']),
 'pr-0063':(['raw_return','stdout_side_effect_failure'],['stdout_side_effect_failure','raw_return']),
 'pr-0066':([None],[None]),
 'pr-0067':([None],[None]),
 'pr-0074':(['invalid_write_truncates'],['invalid_write_truncates','invalid_write_truncates']),
 'pr-0079':([None,'unused_config_requirement'],['unused_config_requirement']),
}

def calculate(rows):
 tp=sum(r['tp'] for r in rows);fp=sum(r['fp'] for r in rows);fn=sum(r['fn'] for r in rows)
 calls=[c for r in rows for c in r['calls']]
 return {'n':len(rows),'targets':tp+fn,'tp':tp,'fp':fp,'fn':fn,
         'precision':tp/(tp+fp) if tp+fp else 0,'recall':tp/(tp+fn) if tp+fn else 0,
         'f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0,
         'duplicates_merged':sum(r['duplicates'] for r in rows),
         'model_calls':len(calls),'failed_calls':sum(not c['ok'] for c in calls),
         'input_tokens':sum(c['usage'].get('prompt_tokens',0) for c in calls),
         'output_tokens':sum(c['usage'].get('completion_tokens',0) for c in calls),
         'total_tokens':sum(c['usage'].get('prompt_tokens',0)+c['usage'].get('completion_tokens',0) for c in calls),
         'mean_seconds':sum(r['wall_ms']/1000 for r in rows)/len(rows) if rows else 0,
         'strict_complete':sum(r['status']=='complete' for r in rows),
         'final_workflow_completed':sum(r['final_workflow_completed'] for r in rows)}

def main():
 manifest=json.loads((OUT/'manifest.json').read_text(encoding='utf-8'))
 truth=json.loads((PRIOR/'semantic-rescore.json').read_text(encoding='utf-8'))['truth']
 assert set(truth)==set(MAP)==set(manifest['case_ids'])
 rows={};notes=[]
 for gi,g in enumerate(['static','evolved']):
  rows[g]=[]
  for cid in manifest['case_ids']:
   r=audit_result(json.loads((OUT/g/cid/'D-full/result.json').read_text(encoding='utf-8')))
   labels=MAP[cid][gi]
   assert len(labels)==len(r['findings'])
   assert all(x is None or x in truth[cid] for x in labels)
   found={x for x in labels if x is not None}
   r.update(tp=len(found),fp=labels.count(None),fn=len(set(truth[cid])-found),
            duplicates=len([x for x in labels if x is not None])-len(found),
            matched_semantic_issues=sorted(found),missing_semantic_issues=sorted(set(truth[cid])-found))
   rows[g].append(r)
   for i,f in enumerate(r['findings']):
    note='Matched main semantic issue.' if labels[i] else 'Insufficient context or confirmed defect evidence; retained as FP conservatively.'
    if g=='static' and cid=='pr-0062':
     note='Title overstates missing globals; explanation also independently establishes returning unrelated file contents with valid bindings. Match that demonstrated behavior, not the NameError claim.'
    if g=='evolved' and cid=='pr-0012' and i==1:
     note='Low-severity source hygiene warning, not an established credential defect; retained as unsupported FP for the fixed defect universe. Removing this pending-verification warning is a separate sensitivity calculation.'
    if g=='evolved' and cid=='pr-0074' and i==1:
     note='Primary write-type exception overlaps invalid-write finding 0 and is merged. Additionally reports successful str-subclass raw return, a valid new out-of-catalogue behavior disclosed separately, not added to frozen main metrics.'
    if cid=='pr-0041' and ((g=='static' and i==2) or (g=='evolved' and i==1)):
     note='Compound finding mentions both input exception and successful raw return; one primary identity only, as in prior rescoring. Do not count one finding as two TP.'
    notes.append({'group':g,'id':cid,'finding_index':i,'title':f['title'],'explanation':f['explanation'],
                  'matched_issue':labels[i],'adjudication':note})
 scopes={'all_20':set(manifest['case_ids']),'feedback_4':set(manifest['training_case_ids']),
         'not_used_for_feedback_16':set(manifest['evaluation_case_ids'])}
 common={r['id'] for r in rows['static'] if r['status']=='complete'} & {r['id'] for r in rows['evolved'] if r['status']=='complete'}
 scopes['paired_strict_complete']=common
 results={scope:{g:calculate([r for r in rr if r['id'] in ids]) for g,rr in rows.items()} for scope,ids in scopes.items()}
 skill_use={g:sum('correctness-review' in r['summary']['collaboration']['agent_skills'] for r in rr) for g,rr in rows.items()}
 sensitivity={}
 for g,rr in rows.items():
  expanded=[]
  for r in rr:
   q=dict(r)
   # Both reports explicitly substantiate both mechanisms, but the main metric
   # retains prior one-to-one primary-issue counting.
   if r['id']=='pr-0041':q['tp']+=1;q['fn']-=1
   expanded.append(q)
  sensitivity[g]=calculate(expanded)
 report={'method':'Frozen prior shared semantic targets; manual adjudication, identical treatment, no official label edits',
         'generation':manifest['generation'],'scopes':results,'scope_case_ids':{k:sorted(v) for k,v in scopes.items()},
         'correctness_skill_selected_cases':skill_use,'findings':notes,
         'case_results':[{k:r[k] for k in ['id','status','final_workflow_completed','tp','fp','fn','duplicates','matched_semantic_issues','missing_semantic_issues','workflow_problems']}|{'group':g} for g,rr in rows.items() for r in rr],
         'compound_mechanism_sensitivity':sensitivity,
         'new_out_of_catalogue':[{'group':'evolved','id':'pr-0074','finding_index':1,'issue':'str-subclass successfully writes but returns original subtype instead of normalized plain string','affects_main_metrics':False}],
         'activation_decision':'experimental_only_no_blind_holdout_gate; no production Skill activation',
         'price_note':'Provider price unknown; tokens only'}
 (OUT/'semantic-comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 lines=['# 静态/进化 Skills 逐条语义复核','','主指标冻结原来的目标集合，CWE 分类差异不影响语义匹配。','']
 for x in notes:
  lines += [f"## {x['group']} / {x['id']} / finding {x['finding_index']}",'',x['title'],'',x['explanation'],'',f"匹配：{x['matched_issue'] or 'FP / evidence insufficient'}",'',x['adjudication'],'']
 (OUT/'adjudication-index.md').write_text('\n'.join(lines),encoding='utf-8')
 print(json.dumps({'scopes':results,'skill_use':skill_use},ensure_ascii=False))

if __name__=='__main__':
 main()
