"""Collection helpers for Stage 10. Existing Stage 9 controls are reused."""
import hashlib
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
S9=ROOT/"stage9_deep"
sys.path.insert(0,str(S9))
import bench
from bench import GEN, MODELS, attempt, base, clock, complete, digest, previous, read, rotate, save, send, server

OLD_E=S9/"runs/E_formal_20261106_212113_018921"
OLD_D=S9/"runs/D_formal_20261106_211741_cf5902"

def prepare(exp,models,schedule,extra=()):
    pilot=base.is_pilot()
    paths=[HERE/"engine.py",HERE/f"{exp}.py",HERE/"PROTOCOL.md",S9/"bench.py",bench.TOKEN_FILE,
           bench.S8/"common.py",bench.S8/"inputs.json",ROOT/"stage7_local/run_experiment.py",
           ROOT/"stage7_local/model_manifest.json",*extra]
    hashes={p.relative_to(ROOT).as_posix():digest(p) for p in paths}
    archive=base.ASSETS/"llama-b11347-bin-win-cpu-x64.zip"
    archive_hash=digest(archive)
    if archive_hash!="f1f7198a7c1cdf13bec7c2cf960947e0bec87401837a057134186132d870aa04":
        raise ValueError("Wrong CPU distribution")
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if name.endswith((".dll",".exe")) and digest(base.BACKEND/name)!=hashlib.sha256(z.read(name)).hexdigest():
                raise ValueError(name)
    weights={}
    for k in models:
        m=previous.MODELS[k]
        weights[k]=digest(base.ASSETS/m["filename"])
        if weights[k]!=m["sha256"]:
            raise ValueError(k)
    phase="pilot" if pilot else "formal"
    bound=None
    if not pilot:
        directory=HERE/sys.argv[2];bound=directory.name
        v=read(directory/"completion.json")
        if not(v["passed"] and v["phase"]=="pilot" and v["exp"]==exp and v["hashes"]==hashes and v["weights"]==weights):
            raise ValueError("Pilot does not match")
    out=HERE/"runs"/(exp+"_"+phase+"_"+datetime.now().strftime("%Y%m%d_%H%M%S")+"_"+uuid.uuid4().hex[:6])
    out.mkdir(parents=True)
    meta=dict(exp=exp,phase=phase,pilot=bound,hashes=hashes,weights=weights,backend_sha=archive_hash,
              started=datetime.now().astimezone().isoformat(),machine=previous.machine())
    save(out/"protocol.json",meta);save(out/"schedule.json",schedule)
    print("RUN",out.name,flush=True)
    return out,meta

def perf(exp,groups,models,texts=None):
    extra=[HERE/"texts.json"] if texts else []
    out,meta=prepare(exp,models,groups,extra)
    frozen=None
    if texts and meta["pilot"]:
        frozen=read(HERE/"runs"/meta["pilot"]/"tokens.json")
    known={"stage8":bench.IDS}
    rows=[]
    for gi,group in enumerate(groups,1):
        tag=f"g{gi:03}"
        with server(out,tag,group["model"],group["t"],group["tb"]) as port:
            name=group["text"]
            if texts:
                tokenized=send(port,"/tokenize",dict(content=texts[name],add_special=False))["tokens"]
                ids=tokenized[:768]
                if len(ids)!=768 or (name in known and known[name]!=ids) or (frozen and frozen[name]!=ids):
                    raise ValueError("Short text or inconsistent tokenizer: "+name)
                known[name]=ids
                save(out/"tokens.json",{k:v for k,v in known.items() if k!="stage8"})
            warm=send(port,"/completion",dict(**GEN,prompt="Brief backend warmup",n_predict=8,ignore_eos=True))
            save(out/(tag+"_warm.json"),warm)
            for pos,(n,g) in enumerate(group["jobs"],1):
                ref=tag+f"_{pos}"
                def collect(row):
                    req=dict(**GEN,prompt=known[name][:n],n_predict=g,ignore_eos=True)
                    row["start_ns"]=clock()
                    raw=send(port,"/completion",req)
                    row["end_ns"]=clock()
                    row["response"]=raw
                    tt=raw["timings"]
                    if (tt["prompt_n"],tt["predicted_n"],tt["cache_n"])!=(n,g,0) or raw.get("truncated"):
                        raise ValueError("Workload or cache mismatch")
                fields=dict(round=group["round"],text=name,model=group["model"],t=group["t"],tb=group["tb"],n=n,g=g)
                rows.append(attempt(out,ref,fields,collect))
    return complete(out,meta,rows,sum(len(a["jobs"]) for a in groups))
