"""Fixed generated length and context capacity across three exact input sizes."""
import json
from common import GEN, INPUT, MODELS, ROOT, attempt, clock, finish, is_pilot, prepare, save, send, server

LENGTHS=[64,256,768]
def rotate(seq,shift):
    n=shift%len(seq)
    return seq[n:]+seq[:n]

def main():
    pilot=is_pilot()
    rounds=1 if pilot else 5
    out,meta=prepare("B",pilot,dict(lengths=LENGTHS,context=1024,generated=32,rounds=rounds,
                                    model_order=[rotate(MODELS,r) for r in range(rounds)],
                                    timing="HTTP wall time including response parsing"))
    ids=None if pilot else json.loads((ROOT/"runs"/meta["previous_pilot"]/"token_ids.json").read_text(encoding="utf-8"))
    rows=[]
    for r in range(rounds):
        for key in rotate(MODELS,r):
            idx=MODELS.index(key)
            label=f"B{r+1}_{key}"
            with server(out,label,key) as (port,sv):
                body=send(port,"/tokenize",dict(content=INPUT["corpus"]*32,add_special=False))
                tokens=body["tokens"][:max(LENGTHS)]
                if ids is None:
                    ids=tokens
                if len(tokens)!=max(LENGTHS) or ids!=tokens:
                    raise ValueError("Token ids differ")
                save(out/"token_ids.json",ids)
                warm=send(port,"/completion",dict(**GEN,prompt="Brief backend warmup",n_predict=8,ignore_eos=True))
                save(out/(label+"_warmup.json"),warm)
                for pos,n in enumerate(rotate(LENGTHS,r+idx),1):
                    def collect(row):
                        req=dict(**GEN,prompt=ids[:n],n_predict=32,ignore_eos=True)
                        row["start_ns"]=clock()
                        response=send(port,"/completion",req)
                        row["end_ns"]=clock()
                        row["response"]=response
                        t=response["timings"]
                        if not (t["prompt_n"]==n and t["predicted_n"]==32 and t["cache_n"]==0 and not response.get("truncated",False)):
                            raise ValueError("Unexpected backend token counts or truncation: "+str(t))
                        row["e2e_ms"]=(row["end_ns"]-row["start_ns"])/1e6
                        row["prompt_tps"]=t["prompt_per_second"]
                        row["generation_tps"]=t["predicted_per_second"]
                    rows.append(attempt(out,label+f"_{n}",dict(round=r+1,model=key,length=n,position=pos),collect))
    return finish(out,meta,rows,rounds*9)

if __name__=="__main__":
    raise SystemExit(main())
