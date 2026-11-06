"""Offline byte/coverage checks and summaries for explicitly named formal runs."""
import csv
import argparse
import itertools
import json
import math
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path
from common import INPUT, MODELS, ROOT, digest, save
from experiment_C import variants

def read(p):
    return json.loads(p.read_text(encoding="utf-8"))

def stats(vals):
    return dict(n=len(vals),mean=st.mean(vals),sd=st.stdev(vals),min=min(vals),max=max(vals))

def near(a,b):
    assert math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-6),(a,b)

def table(p,rows):
    with p.open("w",encoding="utf-8",newline="") as f:
        writer=csv.DictWriter(f,list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

def validate(p,exp):
    meta=read(p/"completion.json")
    assert meta["phase"]=="formal" and meta["passed"] and meta["exp"]==exp
    pilot=read(p.parent/meta["previous_pilot"]/"completion.json")
    assert pilot["passed"] and pilot["phase"]=="pilot" and pilot["signature"]==meta["signature"]
    for filename,sha in meta["signature"].items():
        src=ROOT/filename if filename!="run_experiment.py" else ROOT.parent/"stage7_local"/filename
        assert digest(src)==sha
    servers=[read(f) for f in sorted(p.glob("*_server.json"))]
    power=Counter(str(v["power_plugged"] if "power_plugged" in v else v["plugged_in"])
                  for s in servers for v in (s["power_before"],s["power_after"]) if v is not None)
    return dict(formal=p.name,pilot=meta["previous_pilot"],machine=meta["machine"],servers=len(servers),
                power_at_start_and_end=dict(power),requests_or_sessions=meta["expected"])

def audit_a(p,dest):
    results=[]
    metrics=["startup_ready_ms","first_ttft_ms","startup_text_ms","first_total_ms","second_ttft_ms","second_total_ms"]
    for r in range(1,7):
        for pos in (1,2):
            label=f"A{r}_{pos}"
            row=read(p/(label+".json"));sv=read(p/(label+"_server.json"))
            assert row["ok"] and row["warmup"]==sv["warmup"]==((r+pos)%2==0)
            assert row["warmup"]==("--no-warmup" not in sv["flags"])
            assert sv["start_ns"] < sv["ready_ns"] <= row["first"]["start_ns"]
            assert row["second"]["start_ns"] >= row["first"]["done_ns"]
            raw_text=[]
            for field in ("first","second"):
                rec=row[field]; events=rec["events"]
                visible=[(ev["ns"],ch["delta"]["content"]) for ev in events for ch in ev["chunk"]["choices"]
                         if (ch["delta"].get("content") or "").strip()]
                assert rec["first_ns"]==visible[0][0] and rec["done_ns"]>=events[-1]["ns"]
                assert rec["payload"]["messages"][-1]["content"]==INPUT[field]
                assert any(ch.get("finish_reason")=="stop" for ev in events for ch in ev["chunk"]["choices"])
                near(row[field+"_ttft_ms"],(rec["first_ns"]-rec["start_ns"])/1e6)
                near(row[field+"_total_ms"],(rec["done_ns"]-rec["start_ns"])/1e6)
                raw_text.append("".join(ch["delta"].get("content") or "" for ev in events for ch in ev["chunk"]["choices"]))
            near(row["startup_text_ms"],(row["first"]["first_ns"]-sv["start_ns"])/1e6)
            near(row["startup_ready_ms"],(sv["ready_ns"]-sv["start_ns"])/1e6)
            results.append(dict(round=r,position=pos,warmup=row["warmup"],**{m:row[m] for m in metrics},
                                first_output=raw_text[0],second_output=raw_text[1]))
    table(dest/"A_startups.csv",results)
    by={str(flag):{m:stats([r[m] for r in results if r["warmup"]==flag]) for m in metrics} for flag in (True,False)}
    pairs=[]
    for n in range(1,7):
        on,= [r for r in results if r["round"]==n and r["warmup"]]
        off,=[r for r in results if r["round"]==n and not r["warmup"]]
        pairs.append(dict(round=n,**{m:off[m]-on[m] for m in metrics}))
    table(dest/"A_paired_off_minus_on.csv",pairs)
    return dict(by_warmup=by,paired_difference={m:stats([r[m] for r in pairs]) for m in metrics},
                paired_off_faster=sum(r["startup_text_ms"]<0 for r in pairs),
                unique_outputs={k:sorted({r[k] for r in results}) for k in ("first_output","second_output")})

def audit_b(p,dest):
    results=[]; ids=read(p/"token_ids.json"); pilot=read(p/"protocol.json")["previous_pilot"]
    assert len(ids)==768 and ids==read(p.parent/pilot/"token_ids.json")
    for r,key,n in itertools.product(range(1,6),MODELS,(64,256,768)):
        row=read(p/f"B{r}_{key}_{n}.json"); raw=row["response"]; t=raw["timings"]
        assert row["ok"] and (row["round"],row["model"],row["length"])==(r,key,n)
        assert row["position"]==((64,256,768).index(n)-(r-1)-MODELS.index(key))%3+1
        assert t["cache_n"]==0 and t["prompt_n"]==n and t["predicted_n"]==32 and not raw.get("truncated",False)
        near(row["e2e_ms"],(row["end_ns"]-row["start_ns"])/1e6)
        for field,target in [("prompt_tps","prompt_per_second"),("generation_tps","predicted_per_second")]:
            near(row[field],t[target])
        assert max(t["prompt_ms"],t["predicted_ms"])>0
        results.append({k:row[k] for k in ("round","model","length","position","e2e_ms","prompt_tps","generation_tps")})
    table(dest/"B_requests.csv",results)
    return {k:{str(n):{m:stats([r[m] for r in results if r["model"]==k and r["length"]==n]) for m in
                        ("e2e_ms","prompt_tps","generation_tps")} for n in (64,256,768)} for k in MODELS}

def audit_c(p,dest):
    jobs=read(p/"variant_order.json"); assert jobs==variants(False) and len(jobs)==40
    results=[]; paired=[];out={}
    for k in MODELS:
        by=defaultdict(list); kinds=Counter()
        for pos,j in enumerate(jobs,1):
            r=read(p/f"C_{k}_{pos:02}.json"); assert r["model"]==k and r["ok"]
            assert all(r[field]==j[field] for field in ("item","rotation","labels","gold"))
            ch=r["response"]["choices"][0]; answer=ch["message"]["content"].strip()
            assert r["response"]["usage"]["prompt_tokens_details"]["cached_tokens"]==0
            valid=ch["finish_reason"]=="stop" and answer in j["labels"]
            position=j["labels"].index(answer) if valid else None
            assert r["valid"]==valid and r["answer"]==answer and r["correct"]==(valid and answer==j["gold"])
            assert r["selected_position"]==position
            results.append(dict(model=k,sequence=pos,item=j["item"],rotation=j["rotation"],first_label=j["labels"][0],
                                second_label=j["labels"][1],gold=j["gold"],answer=answer,valid=valid,correct=r["correct"],
                                selected_position=position,finish_reason=ch["finish_reason"]))
            by[j["item"]].append(results[-1])
        for item,g in sorted(by.items()):
            a,b=sorted(g,key=lambda x:x["rotation"])
            assert a["first_label"]==b["second_label"] and a["second_label"]==b["first_label"] and a["gold"]==b["gold"]
            if not (a["valid"] and b["valid"]): kind="invalid"
            elif a["answer"]==b["answer"]: kind="same_label"
            elif a["selected_position"]==b["selected_position"]==0: kind="both_first"
            else:
                assert a["selected_position"]==b["selected_position"]==1
                kind="both_second"
            kinds[kind]+=1
            paired.append(dict(model=k,item=item,gold=a["gold"],order0_answer=a["answer"],order1_answer=b["answer"],kind=kind))
        g=[r for r in results if r["model"]==k]
        assert len(by)==20 and len(g)==40
        out[k]=dict(pairs={kind:kinds[kind] for kind in ("both_first","both_second","same_label","invalid")},
                    correct_by_order=[sum(r["correct"] for r in g if r["rotation"]==rot) for rot in (0,1)],
                    invalid=sum(not r["valid"] for r in g),truncations=sum(r["finish_reason"]=="length" for r in g),
                    picked_first=sum(r["selected_position"]==0 for r in g))
    table(dest/"C_requests.csv",results)
    table(dest/"C_item_pairs.csv",paired)
    return out

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("runs",nargs=3,help="Explicit A, B, C formal directories in order")
    parser.add_argument("--output",default="recomputed",help="A new directory for the offline results")
    args=parser.parse_args()
    paths=[ROOT/a for a in args.runs]
    dest=ROOT/args.output
    dest.mkdir(parents=True,exist_ok=False)
    runs={k:validate(p,k) for k,p in zip("ABC",paths)}
    save(dest/"summary.json",dict(runs=runs,A=audit_a(paths[0],dest),B=audit_b(paths[1],dest),C=audit_c(paths[2],dest)))
    print("Verified A/B/C matrices and recomputed results")

if __name__=="__main__":
    main()
