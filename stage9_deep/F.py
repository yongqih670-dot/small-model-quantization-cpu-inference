"""Factorial generation/batch thread tuning for two contrasting workloads."""
import itertools
import random
from bench import THREADS, WORKLOADS, base, performance, rotate

def schedule(pilot):
    configs=list(itertools.product(THREADS,repeat=2))
    random.Random(20261108).shuffle(configs)
    configs=rotate(configs,configs.index((4,4)))
    groups=[]
    for r in range(1 if pilot else 9):
        for t,tb in rotate(configs,r):
            idx=configs.index((t,tb))
            groups.append(dict(round=r+1,model="Q4_K_M",t=t,tb=tb,
                jobs=[list(w) for w in rotate(WORKLOADS,r+idx)]))
    return groups

if __name__=="__main__":
    raise SystemExit(performance("F",schedule(base.is_pilot()),["Q4_K_M"]))
