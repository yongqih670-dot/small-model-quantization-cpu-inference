"""Verify named runs, recompute all metrics, and write fresh output files."""
import argparse
import csv
import itertools
import statistics as st
from collections import Counter,defaultdict
import G,H,I
from engine import HERE,ROOT,S9,OLD_D,MODELS,digest,read,save

def table(path,rows,fields=None):
    with path.open("w",encoding="utf-8",newline="") as f:
        wr=csv.DictWriter(f,fieldnames=fields or list(rows[0]));wr.writeheader();wr.writerows(rows)

def metadata(p,exp,n):
    m=read(p/"completion.json")
    assert m["phase"]=="formal" and m["exp"]==exp and m["passed"] and m["expected"]==m["attempts"]==n
    pilot=read(p.parent/m["pilot"]/"completion.json")
    assert pilot["passed"] and pilot["exp"]==exp and pilot["phase"]=="pilot" and pilot["hashes"]==m["hashes"]
    assert all(digest(ROOT/k)==h for k,h in m["hashes"].items())
    sessions=[read(f) for f in p.glob("*_server.json")]
    power=Counter(str(v["plugged_in"]) if v else "unknown" for s in sessions for v in (s["power_before"],s["power_after"]))
    return dict(formal=p.name,pilot=m["pilot"],count=n,servers=len(sessions),power=dict(power),machine=m["machine"])

def performance(p,groups,old=False):
    assert read(p/"schedule.json")==groups
    out=[]
    for i,group in enumerate(groups,1):
        tag=f"group_{i:03}" if old else f"g{i:03}"
        srv=read(p/(tag+"_server.json"));flags=srv["flags"]
        assert srv["model"]==group["model"] and (int(flags[flags.index("-t")+1]),int(flags[flags.index("-tb")+1]))==(group["t"],group["tb"])
        assert read(p/(tag+"_warm.json"))["timings"]["predicted_n"]==8
        for pos,(n,g) in enumerate(group["jobs"],1):
            name=tag+(f"_{pos:02}" if old else f"_{pos}")+".json"
            row=read(p/name)
            assert row["ok"] and row["round"]==group["round"] and row["model"]==group["model"]
            assert (row["t"],row["tb"])==(group["t"],group["tb"])
            assert (row.get("n",row.get("input_tokens")),row.get("g",row.get("output_tokens")))==(n,g)
            assert row.get("text","stage8")==group.get("text","stage8")
            t=row["response"]["timings"]
            assert (t["prompt_n"],t["predicted_n"],t["cache_n"])==(n,g,0) and not row["response"].get("truncated")
            total=(row["end_ns"]-row["start_ns"])/1e6
            assert total>0 and t["prompt_ms"]>0 and t["predicted_ms"]>0
            out.append(dict(round=row["round"],model=row["model"],t=row["t"],tb=row["tb"],n=n,g=g,text=row.get("text","stage8"),
                            total=total,prompt=t["prompt_ms"],generation=t["predicted_ms"],other=total-t["prompt_ms"]-t["predicted_ms"]))
    return out

def gap(x,reference):
    return (x/reference-1)*100

def threads(p,dest):
    rows=performance(p,G.schedule(False));table(dest/"G_requests.csv",rows)
    result=[];paired=[]
    for n,g in G.LOADS:
        group=[r for r in rows if (r["n"],r["g"])==(n,g)]
        v={(r["t"],r["tb"],r["round"]):r["total"] for r in group}
        assert len(v)==18
        means={k:st.mean(v[*k,i] for i in range(1,7)) for k in G.CONFIGS}
        best=min(means,key=means.get);rate=gap(means[8,16],means[best])
        close=0
        for i in range(1,7):
            fastest=min(v[*k,i] for k in means)
            pct=gap(v[8,16,i],fastest);close+=pct<=3
            paired.append(dict(n=n,g=g,round=i,ms_8_8=v[8,8,i],ms_8_16=v[8,16,i],ms_16_16=v[16,16,i],
                               delta_to_mean_best_ms=v[8,16,i]-v[*best,i],gap_to_round_best_pct=pct))
        result.append(dict(n=n,g=g,mean_8_8=means[8,8],mean_8_16=means[8,16],mean_16_16=means[16,16],
                           sd_8_8=st.stdev(v[8,8,i] for i in range(1,7)),sd_8_16=st.stdev(v[8,16,i] for i in range(1,7)),
                           sd_16_16=st.stdev(v[16,16,i] for i in range(1,7)),best=f"{best[0]}/{best[1]}",
                           gap_pct=rate,passes=rate<=3,within3_rounds=close))
    table(dest/"G_loads.csv",result);table(dest/"G_paired.csv",paired)
    return result

def classify(p,dest):
    old,hist,gate=H.history()
    assert gate
    table(dest/"H_historical_pairs.csv",old)
    jobs=H.schedule(False);assert read(p/"schedule.json")==jobs
    data=read(H.DATA);scenes=[s for group in data for s,_ in group]
    prior={s for domain in read(H.RULES)["domains"] for s,_ in domain["scenes"]}
    prior.update(j["scene"] for j in read(ROOT/"stage8_extensions/inputs.json")["cases"])
    assert len(set(scenes))==30 and not set(scenes)&prior
    assert all(sorted(g for _,g in group)==[0,0,0,1,1,1] for group in data)
    assert len(jobs)==30 and sum(j["order"][0]==0 for j in jobs)==15
    pairs=defaultdict(dict);cost={};requests=[]
    for k in MODELS:
        for i,j in enumerate(jobs,1):
            trial=read(p/f"{k}_{i:02}.json")
            assert all(r["ok"] for r in trial) and [r["c"] for r in trial]==j["order"]
            assert trial[0]["start_ns"]<trial[0]["end_ns"]<=trial[1]["start_ns"]<trial[1]["end_ns"]
            baseline=next(r for r in trial if r["c"]==0)
            cost[k,j["item"]]=((baseline["end_ns"]-baseline["start_ns"])/1e6,(trial[1]["end_ns"]-trial[0]["start_ns"])/1e6)
            for r in trial:
                assert (r["model"],r["item"])==(k,j["item"])
                answer,valid=H.scored(r["response"],j["labels"])
                pairs[k,j["item"],0][r["c"]]=dict(gold=j["gold"],answer=answer,valid=valid)
                requests.append(dict(model=k,item=j["item"],candidate=r["c"],gold=j["gold"],answer=answer,valid=valid,
                    duration_ms=(r["end_ns"]-r["start_ns"])/1e6,finish=r["response"]["choices"][0]["finish_reason"]))
    obs=H.trials(pairs)
    for v in obs:
        v["single_ms"],v["pair_ms"]=cost[v["model"],v["item"]]
    table(dest/"H_new_pairs.csv",obs);table(dest/"H_new_requests.csv",requests)
    new={}
    for k in MODELS:
        grp=[x for x in obs if x["model"]==k];assert len(grp)==30
        m=H.rates(grp);m["single_ms"]=st.mean(x["single_ms"] for x in grp);m["pair_ms"]=st.mean(x["pair_ms"] for x in grp)
        m["cost_ratio"]=m["pair_ms"]/m["single_ms"]
        m["invalid_requests"]=sum(not r["valid"] for r in requests if r["model"]==k)
        new[k]=m
    return dict(history=hist,new=new)

def historical_d():
    import D
    m=read(OLD_D/"completion.json")
    assert m["passed"] and m["attempts"]==60
    return performance(OLD_D,D.schedule(False),True)

PARTS=["prompt","generation","other"]
def decomposition(rows):
    by={}
    for r in rows:
        key=(r["text"],r["g"],r["round"])
        group=by.setdefault(key,{})
        assert r["model"] not in group
        group[r["model"]]=r
    pairs=[]
    for (text,g,i),group in sorted(by.items()):
        a,b=group["Q4_K_M"],group["Q8_0"]
        diff={v:b[v]-a[v] for v in ["total",*PARTS]}
        assert abs(diff["total"]-sum(diff[k] for k in PARTS))<.00001
        pairs.append(dict(text=text,g=g,round=i,**diff,largest_abs=max(PARTS,key=lambda k:abs(diff[k]))))
    cells=[];flips=[]
    for text,g in sorted({(r["text"],r["g"]) for r in pairs}):
        v=[r for r in pairs if (r["text"],r["g"])==(text,g)]
        assert {r["round"] for r in v}==set(range(1,7))
        for neg in (r for r in v if r["total"]<0):
            for pos in (r for r in v if r["total"]>0):
                swing={k:pos[k]-neg[k] for k in PARTS}
                flips.append(dict(text=text,g=g,q8_faster_round=neg["round"],q8_slower_round=pos["round"],
                                  total_swing=pos["total"]-neg["total"],**swing,largest_positive=max(PARTS,key=swing.get)))
        cells.append(dict(text=text,g=g,**{k:st.mean(r[k] for r in v) for k in ["total",*PARTS]},
                          sd=st.stdev(r["total"] for r in v),q4_wins=sum(r["total"]>0 for r in v),q8_wins=sum(r["total"]<0 for r in v)))
    counts=dict(Counter(r["largest_positive"] for r in flips))
    return pairs,cells,flips,counts

def stages(p,dest):
    ids=read(p/"tokens.json");pilot=read(p/"protocol.json")["pilot"]
    assert ids==read(p.parent/pilot/"tokens.json") and set(ids)==set(read(I.TEXT))
    assert all(len(t)==768 for t in ids.values()) and len({tuple(t) for t in ids.values()})==3
    ret={}
    for prefix,rows in (("historical",historical_d()),("new",performance(p,I.schedule(False)))):
        pairs,cells,flips,counts=decomposition(rows)
        table(dest/f"I_{prefix}_requests.csv",rows);table(dest/f"I_{prefix}_pairs.csv",pairs)
        table(dest/f"I_{prefix}_cells.csv",cells)
        table(dest/f"I_{prefix}_contrasts.csv",flips,["text","g","q8_faster_round","q8_slower_round","total_swing",*PARTS,"largest_positive"])
        ret[prefix]=dict(cells=cells,contrasts=counts)
        if prefix=="historical":
            ret["outlier16"]=max((r for r in pairs if r["g"]==16),key=lambda r:abs(r["total"]))
    return ret

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("runs",nargs=3);ap.add_argument("--output",default="recomputed")
    args=ap.parse_args();paths=[HERE/v for v in args.runs]
    out=HERE/args.output;out.mkdir(parents=True,exist_ok=False)
    prov={exp:metadata(p,exp,n) for exp,p,n in zip("GHI",paths,[72,180,108])}
    summary=dict(runs=prov,G=threads(paths[0],out),H=classify(paths[1],out),I=stages(paths[2],out))
    save(out/"summary.json",summary);print("Three matrices, bound pilots, historical records and stage timings verified.")

if __name__=="__main__":
    main()
