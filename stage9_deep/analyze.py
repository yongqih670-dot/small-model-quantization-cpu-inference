"""Recompute all tables directly from named Stage 9 runs."""
import argparse
import csv
import itertools
import math
import statistics as stats
from collections import Counter,defaultdict
from pathlib import Path
import D,E,F
from bench import HERE,ROOT,MODELS,SIZES,THREADS,WORKLOADS,digest,read,save

def measure(values):
    return dict(n=len(values),mean=stats.mean(values),sd=stats.stdev(values),min=min(values),max=max(values))

def write_table(path,rows):
    with path.open("w",encoding="utf-8",newline="") as f:
        writer=csv.DictWriter(f,list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

def provenance(p,exp,expected):
    rec=read(p/"completion.json")
    assert rec["passed"] and rec["phase"]=="formal" and rec["exp"]==exp and rec["attempts"]==rec["expected"]==expected
    pilot=read(p.parent/rec["pilot"]/"completion.json")
    assert pilot["phase"]=="pilot" and pilot["passed"] and pilot["hashes"]==rec["hashes"] and pilot["weights"]==rec["weights"]
    for rel,h in rec["hashes"].items():
        assert digest(ROOT/rel)==h,rel
    servers=[read(x) for x in p.glob("*_server.json")]
    power=Counter(str(s[when]["plugged_in"]) if s[when] else "unknown" for s in servers for when in ("power_before","power_after"))
    return dict(formal=p.name,pilot=rec["pilot"],attempted=expected,servers=len(servers),power_endpoints=dict(power),machine=rec["machine"])

def perf_rows(p,groups):
    assert read(p/"schedule.json")==groups
    data=[]
    for i,group in enumerate(groups,1):
        key=group["model"]
        label=f"group_{i:03}"
        sv=read(p/(label+"_server.json"))
        flags=sv["flags"]
        assert key==sv["model"] and int(flags[flags.index("-t")+1])==group["t"] and int(flags[flags.index("-tb")+1])==group["tb"]
        assert read(p/(label+"_warm.json"))["timings"]["predicted_n"]==8
        for j,(n,g) in enumerate(group["jobs"],1):
            row=read(p/(label+f"_{j:02}.json"))
            assert row["ok"] and (row["model"],row["round"],row["group"],row["position"])==(key,group["round"],i,j)
            assert (row["t"],row["tb"],row["input_tokens"],row["output_tokens"])==(group["t"],group["tb"],n,g)
            raw=row["response"];t=raw["timings"]
            assert (t["prompt_n"],t["predicted_n"],t["cache_n"])==(n,g,0) and not raw.get("truncated",False)
            assert row["end_ns"]>row["start_ns"] and t["prompt_ms"]>0 and t["predicted_ms"]>0
            data.append(dict(round=row["round"],model=key,t=row["t"],tb=row["tb"],input_tokens=n,output_tokens=g,
                        e2e_ms=(row["end_ns"]-row["start_ns"])/1e6,prompt_ms=t["prompt_ms"],gen_ms=t["predicted_ms"],
                        pp_tps=t["prompt_per_second"],tg_tps=t["predicted_per_second"]))
    return data

def analyze_d(p,dest):
    rows=perf_rows(p,D.schedule(False))
    assert len(rows)==60
    write_table(dest/"D_requests.csv",rows)
    cells=[];pairs=[];cross=[]
    for g in SIZES:
        lookup={}
        for k in D.KEYS:
            group=[r for r in rows if r["model"]==k and r["output_tokens"]==g]
            assert sorted(r["round"] for r in group)==list(range(1,7))
            lookup[k]={r["round"]:r["e2e_ms"] for r in group}
            cells.append(dict(output=g,model=k,**measure([r["e2e_ms"] for r in group]),
                              pp_ms=stats.mean(r["prompt_ms"] for r in group),gen_ms=stats.mean(r["gen_ms"] for r in group)))
        delta=[lookup[D.KEYS[1]][i]-lookup[D.KEYS[0]][i] for i in range(1,7)]
        pairs.extend(dict(output=g,round=i+1,Q8_minus_Q4_ms=v) for i,v in enumerate(delta))
        cross.append(dict(output=g,**measure(delta),q4_wins=sum(v>0 for v in delta),q8_wins=sum(v<0 for v in delta)))
    write_table(dest/"D_cells.csv",cells);write_table(dest/"D_paired.csv",pairs);write_table(dest/"D_difference.csv",cross)
    return dict(cells=cells,difference=cross)

def analyze_e(p,dest):
    jobs=E.variants(False)
    assert read(p/"schedule.json")==jobs
    assert len({j["item"] for j in jobs})==20 and len({str((j["item"],j["candidate"],j["rule"])) for j in jobs})==80
    data=read(E.DATA)
    scenes=[scene for domain in data["domains"] for scene,gold in domain["scenes"]]
    older=read(ROOT/"stage8_extensions"/"inputs.json")["cases"]
    assert len(set(scenes))==20 and not set(scenes).intersection(r["scene"] for r in older)
    assert all(sorted(g for _,g in domain["scenes"])==[0,0,1,1] for domain in data["domains"])
    for j in jobs:
        d,i=map(int,j["item"].split("-"))
        domain=data["domains"][d-1]
        clauses=domain["rules"][::1 if j["rule"]==0 else -1]
        scene,gold=domain["scenes"][i-1]
        assert j["gold"]==domain["labels"][gold]
        assert j["messages"][1]["content"].endswith("分类规则：\n"+"\n".join(clauses)+"\n待分类文本："+scene)
        assert "、".join(j["display"]) in j["messages"][1]["content"]
    outputs=[];items=[];models={}
    for k in MODELS:
        d=defaultdict(dict)
        for i,j in enumerate(jobs,1):
            r=read(p/f"{k}_{i:03}.json"); assert r["ok"] and r["model"]==k and r["sequence"]==i
            assert all(r[v]==j[v] for v in ("item","candidate","rule"))
            raw=r["response"]; assert raw["usage"]["prompt_tokens_details"]["cached_tokens"]==0
            choice=raw["choices"][0];ans=choice["message"]["content"].strip()
            valid=choice["finish_reason"]=="stop" and ans in j["labels"]
            c,b=j["candidate"],j["rule"]
            obs=dict(model=k,item=j["item"],seq=i,c=c,b=b,first_list=j["display"][0],first_rule=j["rule_first"],
                     gold=j["gold"],answer=ans,valid=valid,correct=valid and ans==j["gold"],is_A=valid and ans==j["labels"][0],
                     finish=choice["finish_reason"])
            outputs.append(obs);d[j["item"]][c,b]=obs
        kinds=Counter();cl=[];rl=[]
        for item,a in sorted(d.items()):
            assert len(a)==4
            lc=rc=None
            if not all(v["valid"] for v in a.values()): kind="invalid"
            else:
                if all(v["answer"]==v["first_list"] for v in a.values()): kind="always_list_first"
                elif all(v["answer"]==v["first_rule"] for v in a.values()): kind="always_rule_first"
                elif len({v["answer"] for v in a.values()})==1: kind="constant_label"
                else: kind="other"
                lc=sum(a[0,b]["is_A"]-a[1,b]["is_A"] for b in (0,1))/2
                rc=sum(a[c,0]["is_A"]-a[c,1]["is_A"] for c in (0,1))/2
                cl.append(lc);rl.append(rc)
            kinds[kind]+=1
            items.append(dict(model=k,item=item,kind=kind,candidate_effect=lc,rule_effect=rc,
                              **{f"answer_{c}{b}":a[c,b]["answer"] for c,b in itertools.product((0,1),repeat=2)}))
        o=[v for v in outputs if v["model"]==k];conf=[v for v in o if v["first_list"]!=v["first_rule"]]
        assert len(o)==80 and len(conf)==40
        models[k]=dict(kinds={x:kinds[x] for x in ("always_list_first","always_rule_first","constant_label","other","invalid")},
                      candidate_effect=stats.mean(cl) if cl else None,rule_effect=stats.mean(rl) if rl else None,
                      complete_items=len(cl),conflict_list=sum(v["valid"] and v["answer"]==v["first_list"] for v in conf),
                      conflict_rule=sum(v["valid"] and v["answer"]==v["first_rule"] for v in conf),
                      correct=sum(v["correct"] for v in o),invalid=sum(not v["valid"] for v in o),
                      truncated=sum(v["finish"]=="length" for v in o),
                      cells={f"{c}{b}":sum(v["correct"] for v in o if (v["c"],v["b"])==(c,b)) for c,b in itertools.product((0,1),repeat=2)})
    write_table(dest/"E_responses.csv",outputs);write_table(dest/"E_items.csv",items)
    return models

def analyze_f(p,dest):
    groups=F.schedule(False);rows=perf_rows(p,groups)
    assert len(rows)==162 and all(len({(i%9) for i,v in enumerate(groups) if (v['t'],v['tb'])==(t,b)})==9
                                 for t,b in itertools.product(THREADS,repeat=2))
    cells=[];paired=[];factor_effects=[]
    for n,g in WORKLOADS:
        subset=[r for r in rows if (r["input_tokens"],r["output_tokens"])==(n,g)]
        base={r["round"]:r["e2e_ms"] for r in subset if r["t"]==r["tb"]==4}
        for t,b in itertools.product(THREADS,repeat=2):
            cell=[r for r in subset if (r["t"],r["tb"])==(t,b)]
            assert sorted(r["round"] for r in cell)==list(range(1,10))
            diff=[r["e2e_ms"]-base[r["round"]] for r in cell]
            paired.extend(dict(input=n,output=g,t=t,tb=b,round=r["round"],difference_ms=v) for r,v in zip(cell,diff))
            cells.append(dict(input=n,output=g,t=t,tb=b,**measure([r["e2e_ms"] for r in cell]),
                              delta_vs_base_ms=stats.mean(diff),delta_sd_ms=stats.stdev(diff),wins_vs_base=sum(x<0 for x in diff),
                              pp_tps=stats.mean(r["pp_tps"] for r in cell),tg_tps=stats.mean(r["tg_tps"] for r in cell)))
        timings={(r["round"],r["t"],r["tb"]):r["e2e_ms"] for r in subset}
        for changed in ("t","tb"):
            for fixed in THREADS:
                for value in THREADS[1:]:
                    old=(4,fixed) if changed=="t" else (fixed,4)
                    new=(value,fixed) if changed=="t" else (fixed,value)
                    delta=[timings[i,*new]-timings[i,*old] for i in range(1,10)]
                    factor_effects.append(dict(input=n,output=g,changed=changed,held_threads=fixed,from_threads=4,to_threads=value,
                                               delta_mean_ms=stats.mean(delta),delta_sd_ms=stats.stdev(delta),faster_rounds=sum(v<0 for v in delta)))
    write_table(dest/"F_requests.csv",rows);write_table(dest/"F_cells.csv",cells);write_table(dest/"F_paired.csv",paired)
    write_table(dest/"F_one_factor.csv",factor_effects)
    return cells

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("runs",nargs=3,help="D, E, F formal run directories")
    ap.add_argument("--output",default="recomputed")
    args=ap.parse_args();ds=[HERE/r for r in args.runs]
    out=HERE/args.output;out.mkdir(parents=True,exist_ok=False)
    prov={exp:provenance(p,exp,n) for p,exp,n in zip(ds,"DEF",(60,240,162))}
    result=dict(provenance=prov,D=analyze_d(ds[0],out),E=analyze_e(ds[1],out),F=analyze_f(ds[2],out))
    save(out/"summary.json",result);print("All schedules, recorded workloads, scores and file hashes verified.")

if __name__=="__main__":
    main()
