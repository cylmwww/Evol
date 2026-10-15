"""Repeat the frozen 20 x 4 pilot with the current custom provider."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from urllib.parse import urlsplit
from scripts.run_protocol_pilot import ROOT, Settings, AgentSkill, ARMS, run_one, digest


def main():
    settings = Settings.from_env()
    if settings.llm_provider == "joye":
        settings = replace(settings, llm_provider="custom", llm_api_key=settings.llm_api_key or settings.deepseek_api_key)
    config = settings.resolved_llm()
    assert urlsplit(config["base_url"]).hostname == "api.joyehuang.dev"
    out = ROOT / "docs/local/artifacts/model-comparison-gpt61-32k"
    out.mkdir(parents=True, exist_ok=True)
    baseline = json.loads((ROOT/"docs/local/artifacts/protocol-pilot/manifest.json").read_text(encoding="utf-8"))
    dataset = ROOT/"pr_diff_100.jsonl"
    assert digest(dataset.read_bytes()) == baseline["dataset_sha256"]
    rows = {r["id"]:r for r in map(json.loads,dataset.read_text(encoding="utf-8").splitlines())}
    skills = [AgentSkill.from_directory(str(p.parent)) for p in sorted((ROOT/"skills").glob("*/SKILL.md"))]
    manifest = dict(baseline)
    manifest.update(model=config["model"], provider=config["provider"], host=urlsplit(config["base_url"]).hostname,
        role_token_budget=settings.agent_token_budget, role_seconds=settings.agent_time_budget_seconds,
        transport_timeout=settings.timeout_seconds, skill_hashes={s.name:s.content_sha256 for s in skills},
        curl_transport_sha256=digest((ROOT/"scripts/curl_transport.py").read_bytes()),
        endpoint_sha256=digest(config["base_url"].encode()), thinking="provider default; no DeepSeek-specific options", transport="documented curl",
        source_hashes={str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in sorted((ROOT/"evol").glob("*.py"))},
        script_sha256=digest((ROOT/"scripts/run_model_comparison.py").read_bytes()),
        observer_sha256=digest((ROOT/"scripts/run_protocol_pilot.py").read_bytes()))
    path=out/"manifest.json"
    if path.exists():
        assert json.loads(path.read_text(encoding="utf-8"))==manifest, "Do not mix changed configurations"
    path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"prepared":80,"host":manifest["host"],"model":manifest["model"]}),flush=True)
    semaphore=threading.BoundedSemaphore(4)
    first_id, first_arm = baseline['jobs'][0]
    first = run_one(rows[first_id], first_arm, config, settings, skills, out, semaphore)
    print(json.dumps({'done':1,'id':first_id,'arm':first_arm,'status':first['status']}),flush=True)
    if first['calls'] and all(c.get('http_status',0) not in range(200,300) for c in first['calls']):
        raise RuntimeError('Transport preflight failed; remaining jobs not sent')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run_one,rows[i],a,config,settings,skills,out,semaphore) for i,a in baseline["jobs"][1:]]
        for n,f in enumerate(as_completed(futures),2):
            r=f.result()
            print(json.dumps({"done":n,"id":r["id"],"arm":r["arm"],"status":r["status"],"calls":len(r["calls"]),"seconds":r["wall_ms"]/1000}),flush=True)

if __name__ == "__main__":
    main()
