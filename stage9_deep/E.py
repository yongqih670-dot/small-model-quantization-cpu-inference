"""Cross candidate order and the order of two independently readable rules."""
import random
from bench import GEN, HERE, MODELS, attempt, base, complete, read, save, send, server, setup

DATA=HERE/"new_items.json"
def variants(pilot):
    data=read(DATA)
    domains=[data["pilot"]] if pilot else data["domains"]
    jobs=[]
    for d,domain in enumerate(domains,1):
        labels=domain["labels"]
        for i,(scene,gold) in enumerate(domain["scenes"],1):
            for c in (0,1):
                for r in (0,1):
                    candidates=labels[::1 if c==0 else -1]
                    clauses=domain["rules"][::1 if r==0 else -1]
                    user=base.INPUT["classification_template"].format(labels="、".join(candidates),
                        input="分类规则：\n"+"\n".join(clauses)+"\n待分类文本："+scene)
                    jobs.append(dict(item=f"{d}-{i}",candidate=c,rule=r,labels=labels,display=candidates,
                                     rule_first=labels[r],gold=labels[gold],messages=base.messages(user)))
    random.Random(20261107).shuffle(jobs)
    return jobs

def main():
    pilot=base.is_pilot()
    jobs=variants(pilot)
    out,meta=setup("E",MODELS,jobs,[DATA])
    rows=[]
    for k in MODELS:
        with server(out,k,k,4,4) as port:
            for seq,j in enumerate(jobs,1):
                def collect(row):
                    raw=send(port,"/v1/chat/completions",dict(**GEN,messages=j["messages"],max_tokens=96))
                    row["response"]=raw
                    if raw["usage"]["prompt_tokens_details"]["cached_tokens"]!=0:
                        raise ValueError("Unexpected cached tokens")
                fields=dict(model=k,sequence=seq,item=j["item"],candidate=j["candidate"],rule=j["rule"])
                rows.append(attempt(out,k+f"_{seq:03}",fields,collect))
    return complete(out,meta,rows,len(jobs)*len(MODELS))

if __name__=="__main__":
    raise SystemExit(main())
