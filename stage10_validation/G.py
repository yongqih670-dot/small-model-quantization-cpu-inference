"""Three thread configurations under two new and two anchor workloads."""
from itertools import permutations
from engine import base,perf,rotate

CONFIGS=[(8,8),(8,16),(16,16)]
LOADS=[(256,64),(512,48),(768,16),(64,96)]
def schedule(pilot):
    groups=[]
    orders=list(permutations(CONFIGS))
    for i,order in enumerate(orders[:1] if pilot else orders):
        for t,tb in order:
            groups.append(dict(round=i+1,text="stage8",model="Q4_K_M",t=t,tb=tb,
                               jobs=[list(x) for x in rotate(LOADS,i)]))
    return groups

if __name__=="__main__":
    raise SystemExit(perf("G",schedule(base.is_pilot()),["Q4_K_M"]))
