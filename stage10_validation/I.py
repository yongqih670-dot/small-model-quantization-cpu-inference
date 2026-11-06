"""Compare Q4 and Q8 inside each text, output length, and round."""
from engine import HERE,base,perf,rotate,read

TEXT=HERE/"texts.json"
KEYS=["Q4_K_M","Q8_0"]
def schedule(pilot):
    names=list(read(TEXT))
    groups=[]
    for r in range(1 if pilot else 6):
        for name in rotate(names,r):
            idx=names.index(name)
            for k in rotate(KEYS,r+idx):
                groups.append(dict(round=r+1,text=name,model=k,t=4,tb=4,
                                   jobs=[[768,g] for g in rotate([16,32,48],r+idx)]))
    return groups

if __name__=="__main__":
    raise SystemExit(perf("I",schedule(base.is_pilot()),KEYS,read(TEXT)))
