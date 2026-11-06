"""Run stock/timed Q4 validation or the six predeclared balanced startup rounds."""
import argparse
import csv
import hashlib
import http.client
import itertools
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import time
import uuid
import winreg
from datetime import datetime
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
MODELS = {x["quantization"]: x for x in json.loads((ROOT / "model_manifest.json").read_text(encoding="utf-8"))["models"]}
SMOKE = json.loads((ROOT / "smoke_request.json").read_text(encoding="utf-8"))
FLAGS = ["-ngl","0","-t","4","-tb","4","-c","1024","-np","1","-b","2048","-ub","512",
         "-ctk","f16","-ctv","f16","-fa","off","-lm","mmap","--cache-reuse","0","--log-timestamps"]
POLL_S = 0.010
METRICS = ("load_ms", "init_ms", "ready_ms")
TS = re.compile(r"^\s*(\d+)\.(\d{2})\.(\d{3})\.(\d{3})\s+")
PERMUTATIONS = list(itertools.permutations(MODELS))
# Each order exactly once. Predetermined before seeing any formal measurements.
ROUND_ORDER = [0, 3, 4, 1, 5, 2]

def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def scan(path, digest=False):
    h = hashlib.sha256() if digest else None
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(4*1024*1024), b""):
            if h is not None:
                h.update(buf)
    return h.hexdigest() if h is not None else None

def private_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def power_state():
    battery = psutil.sensors_battery()
    return None if battery is None else {"plugged_in": battery.power_plugged, "percent": battery.percent}

def request(port, method, path, timeout, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn.request(method, path, body=body, headers={"Content-Type":"application/json"})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()

def timestamp_ms(line):
    m = TS.match(line)
    if m is None:
        raise ValueError("Backend timestamp missing: " + line[:120])
    minutes, seconds, ms, us = map(int, m.groups())
    return (minutes*60 + seconds)*1000 + ms + us/1000

def parse(log, row, timed):
    lines = log.splitlines()
    starts = [l for l in lines if "load_model: loading model" in l]
    ends = [l for l in lines if "llama_server: model loaded" in l]
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError(f"Expected unique init markers; found {len(starts)}/{len(ends)}")
    row["init_ms"] = timestamp_ms(ends[0]) - timestamp_ms(starts[0])
    if timed:
        calls = re.findall(r"STAGE7_LOAD ticks=(\d+) hz=(\d+) ok=(\d+) no_alloc=(\d+) vocab_only=(\d+)", log)
        row["loader_calls"] = [dict(zip(("ticks","hz","ok","no_alloc","vocab_only"), map(int, c))) for c in calls]
        full = [c for c in calls if c[3:] == ("0","0")]
        if len(full) != 1 or full[0][2] != "1":
            raise ValueError("Expected one successful full model load")
        row["load_ticks"], row["qpc_frequency"] = map(int, full[0][:2])
        row["load_ms"] = 1000 * row["load_ticks"] / row["qpc_frequency"]
        if not (0 < row["load_ms"] <= row["init_ms"]):
            raise ValueError("Loader interval outside init duration")

def launch(out, key, round_id, pos, timed, smoke):
    model = ASSETS / MODELS[key]["filename"]
    exe = (ROOT / "timed_backend" if timed else ASSETS / "backend") / "llama-server.exe"
    label = f"r{round_id}_{pos}_{key}_" + ("timed" if timed else "stock")
    logfile = out / (label + ".log")
    port = private_port()
    argv = [str(exe), "-m", str(model), *FLAGS, "--host", "127.0.0.1", "--port", str(port)]
    row = dict(round=round_id, position=pos, model=key, timed=timed, ok=False,
               load_ms=None, init_ms=None, ready_ms=None, argv=argv, log=logfile.name,
               at=datetime.now().astimezone().isoformat(), free_memory_mib=psutil.virtual_memory().available/1048576)
    proc = None
    try:
        row["power_before"] = power_state()
        scan(model)
        with logfile.open("wb") as f:
            try:
                start = time.perf_counter()
                proc = subprocess.Popen(argv, cwd=exe.parent, stdout=f, stderr=subprocess.STDOUT)
                row["pid"] = proc.pid
                while time.perf_counter() - start < 75:
                    if proc.poll() is not None:
                        raise RuntimeError(f"Server exited: {proc.returncode}")
                    try:
                        status, result = request(port, "GET", "/health", .05)
                        if status == 200 and result.get("status") == "ok":
                            row["ready_ms"] = (time.perf_counter() - start)*1000
                            break
                    except (OSError, http.client.HTTPException):
                        pass
                    time.sleep(POLL_S)
                else:
                    raise TimeoutError("Readiness timeout")
                if smoke:
                    status, result = request(port, "POST", "/v1/chat/completions", 90, SMOKE["payload"])
                    if status != 200:
                        raise ValueError("Smoke HTTP " + str(status))
                    row["smoke_content"] = result["choices"][0]["message"]["content"]
                    row["smoke_finish"] = result["choices"][0]["finish_reason"]
                time.sleep(.15)  # Let the asynchronous backend logger flush; outside all timed intervals.
            finally:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
        parse(logfile.read_text(encoding="utf-8", errors="replace"), row, timed)
        row["ok"] = True
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError, KeyError) as err:
        row["error"] = str(err)
    row["power_after"] = power_state()
    save(out / (label + ".json"), row)
    print(label, row["ok"], *(round(row[k], 3) if row[k] is not None else "-" for k in METRICS),
          row.get("error", ""), flush=True)
    return row

def machine():
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
        cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0]
    power = psutil.sensors_battery()
    return dict(os=platform.platform(), python=platform.python_version(), cpu=cpu,
                physical_cores=psutil.cpu_count(logical=False), logical_cores=psutil.cpu_count(),
                ram_gib=psutil.virtual_memory().total/1024**3, plugged_in=power.power_plugged if power else None)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["smoke","formal"])
    parser.add_argument("--smoke-evidence", type=Path)
    args = parser.parse_args()
    smoke = args.phase == "smoke"
    assets = {str(p.relative_to(ROOT)):scan(p,True) for p in (
        ROOT/"timed_backend"/"llama.dll", ROOT/"timed_backend"/"llama_original.dll",
        ROOT/"timed_backend"/"llama-server.exe", ROOT/"run_experiment.py")}
    if not smoke:
        if args.smoke_evidence is None:
            parser.error("Formal collection requires a passed smoke record")
        evidence = json.loads(args.smoke_evidence.read_text(encoding="utf-8"))
        if evidence.get("phase") != "smoke" or not evidence.get("passed") or evidence["assets"] != assets:
            parser.error("Smoke evidence does not match the instrument and runner")
    wanted = ["Q4_K_M"] if smoke else list(MODELS)
    for key in wanted:
        m = MODELS[key]
        if scan(ASSETS/m["filename"], True) != m["sha256"]:
            raise ValueError("Model hash mismatch: " + key)
    out = ROOT/"runs"/(datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + args.phase + "_" + uuid.uuid4().hex[:6])
    out.mkdir(parents=True,exist_ok=False)
    metadata = dict(phase=args.phase, machine=machine(), assets=assets, flags=FLAGS, health_sleep_s=POLL_S,
                    model_sha256={k:MODELS[k]["sha256"] for k in wanted},
                    smoke_evidence=str(args.smoke_evidence) if args.smoke_evidence else None,
                    rounds=[PERMUTATIONS[i] for i in ROUND_ORDER] if not smoke else None)
    save(out/"protocol.json", metadata)
    if smoke:
        rows = [launch(out,"Q4_K_M",0,i,t,True) for i,t in enumerate([False, True],1)]
        passed = (all(r["ok"] for r in rows) and rows[0]["smoke_content"] == rows[1]["smoke_content"]
                  and rows[0]["smoke_finish"] == rows[1]["smoke_finish"])
    else:
        rows = [launch(out,k,r,i,True,False) for r,idx in enumerate(ROUND_ORDER,1)
                for i,k in enumerate(PERMUTATIONS[idx],1)]
        passed = len(rows) == 18 and all(row["ok"] for row in rows)
    summary = {}
    for key in wanted:
        group = [r for r in rows if r["model"] == key and r["timed"]]
        metrics = {}
        for field in METRICS:
            values = [r[field] for r in group if r["ok"] and r[field] is not None]
            metrics[field] = dict(n=len(values), mean=statistics.mean(values) if values else None,
                                  sd=statistics.stdev(values) if len(values)>1 else None)
        summary[key] = dict(attempts=len(group), valid=sum(r["ok"] for r in group), metrics=metrics)
    with (out/"measurements.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["round","position","model","timed","ok",*METRICS,"log","error"],extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    save(out/"summary.json",dict(**metadata,passed=passed,summary=summary))
    print(out/"summary.json", flush=True)
    return 0 if passed else 1

if __name__ == "__main__":
    raise SystemExit(main())
