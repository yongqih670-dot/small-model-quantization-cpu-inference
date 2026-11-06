"""Shared Stage 9 collection and validation utilities."""
import json
import os
import subprocess
import sys
import time
import uuid
import zipfile
import hashlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
S8 = ROOT / "stage8_extensions"
sys.path.insert(0, str(S8))
import common as base
from common import GEN, MODELS, attempt, clock, digest, previous, save, send

TOKEN_FILE = S8 / "runs/B_formal_210313_349d9f/token_ids.json"
IDS = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
SIZES = [16,32,48,64,96]
THREADS = [4,8,16]
WORKLOADS = [(768,16),(64,96)]

def rotate(seq, i):
    shift=i%len(seq)
    return seq[shift:]+seq[:shift]

def read(path):
    return json.loads(path.read_text(encoding="utf-8"))

def setup(exp, models, plan, extra=()):
    pilot=base.is_pilot()
    src=[HERE/"bench.py",HERE/f"{exp}.py",HERE/"PROTOCOL.md",TOKEN_FILE,S8/"common.py",S8/"inputs.json",
         ROOT/"stage7_local/run_experiment.py",ROOT/"stage7_local/model_manifest.json",*extra]
    hashes={p.relative_to(ROOT).as_posix():digest(p) for p in src}
    archive=base.ASSETS/"llama-b11347-bin-win-cpu-x64.zip"
    archive_sha=digest(archive)
    if archive_sha!="f1f7198a7c1cdf13bec7c2cf960947e0bec87401837a057134186132d870aa04":
        raise ValueError("Official backend archive mismatch")
    with zipfile.ZipFile(archive) as z:
        for f in z.namelist():
            if f.endswith((".exe",".dll")) and digest(base.BACKEND/f)!=hashlib.sha256(z.read(f)).hexdigest():
                raise ValueError("Backend member mismatch: "+f)
    weights={}
    for k in models:
        m=previous.MODELS[k]
        weights[k]=digest(base.ASSETS/m["filename"])
        if weights[k]!=m["sha256"]:
            raise ValueError("Model mismatch: "+k)
    phase="pilot" if pilot else "formal"
    evidence=None
    if not pilot:
        pilot_dir=HERE/sys.argv[2]
        evidence=pilot_dir.name
        rec=read(pilot_dir/"completion.json")
        if not(rec["passed"] and rec["phase"]=="pilot" and rec["exp"]==exp and rec["hashes"]==hashes):
            raise ValueError("Unmatched pilot")
    out=HERE/"runs"/(f"{exp}_{phase}_"+datetime.now().strftime("%Y%m%d_%H%M%S")+"_"+uuid.uuid4().hex[:6])
    out.mkdir(parents=True)
    meta=dict(exp=exp,phase=phase,hashes=hashes,backend_sha=archive_sha,weights=weights,pilot=evidence,
              started=datetime.now().astimezone().isoformat(),machine=previous.machine())
    save(out/"protocol.json",meta)
    save(out/"schedule.json",plan)
    print("RUN",out.name,flush=True)
    return out,meta

@contextmanager
def server(out,label,key,gen_threads,batch_threads):
    port=previous.private_port()
    model=base.ASSETS/previous.MODELS[key]["filename"]
    flags=previous.FLAGS.copy()
    for switch,value in (("-t",gen_threads),("-tb",batch_threads)):
        flags[flags.index(switch)+1]=str(value)
    argv=[str(base.BACKEND/"llama-server.exe"),"-m",str(model),*flags,"--host","127.0.0.1","--port",str(port)]
    info=dict(model=key,flags=flags,port=port,power_before=previous.power_state(),
              time_before=datetime.now().astimezone().isoformat(),available_mib=previous.psutil.virtual_memory().available/1048576)
    previous.scan(model)
    with (out/(label+".log")).open("wb") as log:
        proc=subprocess.Popen(argv,cwd=base.BACKEND,stdout=log,stderr=subprocess.STDOUT)
        try:
            end=time.monotonic()+75
            while time.monotonic()<end:
                if proc.poll() is not None:
                    raise RuntimeError(f"Unexpected server exit {proc.returncode}")
                try:
                    status,body=previous.request(port,"GET","/health",.05)
                    if status==200 and body.get("status")=="ok":
                        break
                except (OSError,base.http.client.HTTPException):
                    pass
                time.sleep(.01)
            else:
                raise TimeoutError("Server readiness")
            yield port
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)
            info["power_after"]=previous.power_state()
            save(out/(label+"_server.json"),info)

def performance(exp,groups,models):
    out,meta=setup(exp,models,groups)
    rows=[]
    for gi,group in enumerate(groups,1):
        label=f"group_{gi:03}"
        with server(out,label,group["model"],group["t"],group["tb"]) as port:
            warm=send(port,"/completion",dict(**GEN,prompt="Brief backend warmup",n_predict=8,ignore_eos=True))
            save(out/(label+"_warm.json"),warm)
            for pos,(n,g) in enumerate(group["jobs"],1):
                case=f"{label}_{pos:02}"
                def collect(row):
                    req=dict(**GEN,prompt=IDS[:n],n_predict=g,ignore_eos=True)
                    row["start_ns"]=clock()
                    raw=send(port,"/completion",req)
                    row["end_ns"]=clock()
                    row["response"]=raw
                    tt=raw["timings"]
                    if (tt["prompt_n"],tt["predicted_n"],tt["cache_n"])!=(n,g,0) or raw.get("truncated"):
                        raise ValueError("Unexpected work or cache reuse")
                fields=dict(group=gi,round=group["round"],model=group["model"],t=group["t"],tb=group["tb"],
                            input_tokens=n,output_tokens=g,position=pos)
                rows.append(attempt(out,case,fields,collect))
    return complete(out,meta,rows,sum(len(g["jobs"]) for g in groups))

def complete(out,meta,rows,expected):
    ok=len(rows)==expected and all(row["ok"] for row in rows)
    save(out/"completion.json",dict(**meta,passed=ok,attempts=len(rows),expected=expected,
                                    completed=datetime.now().astimezone().isoformat()))
    print("COMPLETE",out.name,len(rows),expected,ok,flush=True)
    return 0 if ok else 1
