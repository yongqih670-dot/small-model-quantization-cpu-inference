"""Quantization crossover with fixed input size, paired within round."""
from bench import SIZES, base, performance, rotate

KEYS=["Q4_K_M","Q8_0"]

def schedule(pilot):
    groups=[]
    for r in range(1 if pilot else 6):
        for k in rotate(KEYS,r):
            groups.append(dict(round=r+1,model=k,t=4,tb=4,jobs=[[768,g] for g in rotate(SIZES,r)]))
    return groups

if __name__=="__main__":
    raise SystemExit(performance("D",schedule(base.is_pilot()),KEYS))
