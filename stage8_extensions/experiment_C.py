"""Paired order reversal on 20 previously unused classification scenarios."""
import random
from common import GEN, INPUT, MODELS, attempt, finish, is_pilot, messages, prepare, save, send, server

def variants(pilot):
    if pilot:
        dev=INPUT["development_classifier"]
        records=[dict(id=dev["id"],labels=dev["labels"],gold=dev["gold"],text=dev["input"])]
    else:
        records=[dict(id=c["id"],labels=c["labels"],gold=c["gold"],text=c["rule"]+"\n待分类文本："+c["scene"]) for c in INPUT["cases"]]
    jobs=[]
    for c in records:
        for rotation in (0,1):
            labels=c["labels"][::1 if rotation==0 else -1]
            user=INPUT["classification_template"].format(labels="、".join(labels),input=c["text"])
            jobs.append(dict(item=c["id"],rotation=rotation,labels=labels,gold=c["gold"],messages=messages(user)))
    random.Random(20261106).shuffle(jobs)
    return jobs

def main():
    pilot=is_pilot()
    jobs=variants(pilot)
    if not pilot:
        assert len({j["item"] for j in jobs})==20
        assert all(sum(j["labels"][0]==j["gold"] for j in jobs if j["rotation"]==rot)==10 for rot in (0,1))
    out,meta=prepare("C",pilot,dict(seed=20261106,n_questions=len(jobs)//2,model_order=MODELS))
    save(out/"variant_order.json",jobs)
    rows=[]
    for key in MODELS:
        label=f"C_{key}"
        with server(out,label,key) as (port,sv):
            for pos,job in enumerate(jobs,1):
                def collect(row):
                    response=send(port,"/v1/chat/completions",dict(**GEN,messages=job["messages"],max_tokens=96))
                    row["response"]=response
                    choice=response["choices"][0]
                    answer=choice["message"]["content"].strip()
                    row["answer"]=answer
                    row["truncated"]=choice.get("finish_reason")=="length"
                    row["valid"]=choice.get("finish_reason")=="stop" and answer in job["labels"]
                    row["correct"]=row["valid"] and answer==job["gold"]
                    row["selected_position"]=job["labels"].index(answer) if row["valid"] else None
                fields=dict(model=key,sequence=pos,**{k:v for k,v in job.items() if k!="messages"})
                rows.append(attempt(out,label+f"_{pos:02}",fields,collect))
    return finish(out,meta,rows,len(MODELS)*len(jobs))

if __name__=="__main__":
    raise SystemExit(main())
