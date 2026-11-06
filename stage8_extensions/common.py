"""Shared controls for three serial experiments on the Stage 7 machine."""
import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "stage7_local"))
import run_experiment as previous

ASSETS = previous.ASSETS
BACKEND = ASSETS / "backend"
MODELS = list(previous.MODELS)
INPUT = json.loads((ROOT / "inputs.json").read_text(encoding="utf-8"))
GEN = dict(temperature=0, top_p=1, seed=42, cache_prompt=False)
clock = time.perf_counter_ns

def save(path, data):
    temp = path.with_suffix(path.suffix + ".writing")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)

def digest(path):
    return previous.scan(path, True)

def messages(prompt):
    return [{"role":"system","content":INPUT["system"]},{"role":"user","content":prompt}]

def send(port, route, payload):
    status, body = previous.request(port, "POST", route, 120, payload)
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {str(body)[:200]}")
    return body

def prepare(exp, pilot, controls):
    paths = [ROOT/"common.py", ROOT/f"experiment_{exp}.py", ROOT/"inputs.json", ROOT/"PROTOCOL.md",
             ROOT.parent/"stage7_local"/"run_experiment.py"]
    signature = {p.name:digest(p) for p in paths}
    archive = ASSETS/"llama-b11347-bin-win-cpu-x64.zip"
    archive_sha = digest(archive)
    if archive_sha != "f1f7198a7c1cdf13bec7c2cf960947e0bec87401837a057134186132d870aa04":
        raise ValueError("Backend archive mismatch")
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if name.endswith((".exe",".dll")) and digest(BACKEND/name) != hashlib.sha256(z.read(name)).hexdigest():
                raise ValueError(f"Backend member mismatch: {name}")
    keys = ["Q4_K_M"] if exp=="A" else MODELS
    for k in keys:
        m = previous.MODELS[k]
        if digest(ASSETS/m["filename"]) != m["sha256"]:
            raise ValueError(k)
    phase = "pilot" if pilot else "formal"
    evidence = None
    if not pilot:
        evidence = ROOT / sys.argv[2] / "completion.json"
        record = json.loads(evidence.read_text(encoding="utf-8"))
        if not (record["passed"] and record["phase"] == "pilot" and record["exp"] == exp and record["signature"] == signature):
            raise ValueError("Pilot signature mismatch")
    out = ROOT/"runs"/(exp+"_"+phase+"_"+datetime.now().strftime("%H%M%S")+"_"+uuid.uuid4().hex[:6])
    out.mkdir(parents=True)
    meta = dict(exp=exp,phase=phase,signature=signature,controls=controls,backend_sha256=archive_sha,
                model_sha256={k:previous.MODELS[k]["sha256"] for k in keys},
                previous_pilot=evidence.parent.name if evidence else None,
                local_time=datetime.now().astimezone().isoformat(), machine=previous.machine())
    save(out/"protocol.json",meta)
    print("RUN",out.name,flush=True)
    return out,meta

def finish(out,meta,rows,expected):
    passed = len(rows)==expected and all(r.get("ok",False) for r in rows)
    save(out/"completion.json",dict(**meta,passed=passed,recorded=len(rows),expected=expected))
    print("COMPLETE",out.name,len(rows),expected,passed,flush=True)
    return 0 if passed else 1

@contextmanager
def server(out,label,key,warm=True):
    port=previous.private_port()
    m=ASSETS/previous.MODELS[key]["filename"]
    cmd=[str(BACKEND/"llama-server.exe"),"-m",str(m),*previous.FLAGS]
    if not warm:
        cmd.append("--no-warmup")
    cmd.extend(["--host","127.0.0.1","--port",str(port)])
    meta=dict(model=key,flags=cmd[3:],model_file=m.name,warmup=warm,power_before=previous.power_state(),
              available_mib=previous.psutil.virtual_memory().available/1048576)
    previous.scan(m)
    with (out/(label+".log")).open("wb") as log:
        proc=None
        try:
            meta["start_ns"]=clock()
            proc=subprocess.Popen(cmd,cwd=BACKEND,stdout=log,stderr=subprocess.STDOUT)
            meta["pid"]=proc.pid
            while clock()-meta["start_ns"]<75_000_000_000:
                if proc.poll() is not None:
                    raise RuntimeError(f"Server exited {proc.returncode}")
                try:
                    status,body=previous.request(port,"GET","/health",.05)
                    if status==200 and body.get("status")=="ok":
                        meta["ready_ns"]=clock()
                        break
                except (OSError,http.client.HTTPException):
                    pass
                time.sleep(previous.POLL_S)
            else:
                raise TimeoutError("Health timeout")
            yield port,meta
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
            meta["power_after"]=previous.power_state()
            save(out/(label+"_server.json"),meta)

def attempt(out,name,fields,fn):
    row=dict(**fields,ok=False)
    try:
        fn(row)
        row["ok"]=True
    except Exception as exc:
        row["error"]=repr(exc)
    save(out/(name+".json"),row)
    return row

def is_pilot():
    if len(sys.argv)==2 and sys.argv[1]=="pilot":
        return True
    if len(sys.argv)==3 and sys.argv[1]=="formal":
        return False
    raise SystemExit("Use pilot, or formal runs/<matching_pilot_directory>")
