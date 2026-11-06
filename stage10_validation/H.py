"""Selective classification: historical gate followed by fresh paired inference."""
import random
import sys
from collections import defaultdict
from engine import GEN,HERE,S9,MODELS,OLD_E,attempt,base,clock,complete,prepare,read,save,send,server

DATA=HERE/"holdout.json"
RULES=S9/"new_items.json"
def scored(raw,labels):
    choice=raw["choices"][0]
    assert raw["usage"]["prompt_tokens_details"]["cached_tokens"]==0
    ans=choice["message"]["content"].strip()
    return ans,choice["finish_reason"]=="stop" and ans in labels

def trials(pairs):
    result=[]
    for (k,item,rule),pair in sorted(pairs.items()):
        assert set(pair)=={0,1}
        a,b=pair[0],pair[1]
        assert a["gold"]==b["gold"]
        accepted=a["valid"] and b["valid"] and a["answer"]==b["answer"]
        baseline_wrong=not (a["valid"] and a["answer"]==a["gold"])
        result.append(dict(model=k,item=item,rule=rule,gold=a["gold"],baseline=a["answer"],reverse=b["answer"],
                           accepted=accepted,baseline_wrong=baseline_wrong,missed=accepted and baseline_wrong))
    return result

def rates(rows):
    n=len(rows); accepted=sum(v["accepted"] for v in rows); missed=sum(v["missed"] for v in rows)
    err=sum(v["baseline_wrong"] for v in rows)
    return dict(n=n,accepted=accepted,review=n-accepted,missed=missed,baseline_wrong=err,
                caught=err-missed,review_of_baseline_correct=(n-accepted)-(err-missed),
                coverage=accepted/n,accepted_accuracy=(accepted-missed)/accepted if accepted else None,
                missed_over_all=missed/n,baseline_accuracy=1-err/n)

def history():
    meta=read(OLD_E/"completion.json")
    assert meta["passed"] and meta["attempts"]==240
    jobs=read(OLD_E/"schedule.json")
    pairs=defaultdict(dict)
    for k in MODELS:
        for i,j in enumerate(jobs,1):
            v=read(OLD_E/f"{k}_{i:03}.json");assert v["ok"] and v["item"]==j["item"] and v["candidate"]==j["candidate"] and v["rule"]==j["rule"]
            answer,valid=scored(v["response"],j["labels"])
            pairs[k,j["item"],j["rule"]][j["candidate"]]=dict(answer=answer,valid=valid,gold=j["gold"])
    rows=trials(pairs)
    assert len(rows)==120
    summary={k:{str(r):rates([v for v in rows if v["model"]==k and v["rule"]==r]) for r in [0,1]} for k in MODELS}
    gate=any(v["0"]["caught"]>0 and v["0"]["accepted"]>0 for v in summary.values())
    return rows,summary,gate

def schedule(pilot):
    prior=read(RULES)
    domains=[prior["pilot"]] if pilot else prior["domains"]
    scenes=[prior["pilot"]["scenes"]] if pilot else read(DATA)
    cases=[]
    for d,(domain,group) in enumerate(zip(domains,scenes),1):
        labels=domain["labels"]
        for i,(scene,gold) in enumerate(group,1):
            body="分类规则：\n"+"\n".join(domain["rules"])+"\n待分类文本："+scene
            msgs=[base.messages(base.INPUT["classification_template"].format(labels="、".join(ls),input=body))
                  for ls in (labels,labels[::-1])]
            cases.append(dict(item=f"{d}-{i}",labels=labels,gold=labels[gold],messages=msgs))
    random.Random(20261109).shuffle(cases)
    for pos,case in enumerate(cases):
        case["order"]=[0,1] if pos%2==0 else [1,0]
    return cases

def main():
    if not history()[2]:
        raise SystemExit("Predeclared historical gate did not pass")
    pilot=base.is_pilot();jobs=schedule(pilot)
    out,meta=prepare("H",MODELS,jobs,[DATA,RULES,OLD_E/"completion.json"])
    rows=[]
    for k in MODELS:
        with server(out,k,k,4,4) as port:
            for i,j in enumerate(jobs,1):
                answers=[]
                for c in j["order"]:
                    req=dict(**GEN,messages=j["messages"][c],max_tokens=96)
                    v=dict(item=j["item"],model=k,c=c,ok=False)
                    try:
                        v["start_ns"]=clock()
                        raw=send(port,"/v1/chat/completions",req)
                        v["end_ns"]=clock();v["response"]=raw
                        scored(raw,j["labels"]);v["ok"]=True
                    except Exception as ex:
                        v["error"]=repr(ex)
                    answers.append(v)
                save(out/f"{k}_{i:02}.json",answers)
                rows.extend(answers)
    return complete(out,meta,rows,len(jobs)*len(MODELS)*2)

if __name__=="__main__":
    raise SystemExit(main())
