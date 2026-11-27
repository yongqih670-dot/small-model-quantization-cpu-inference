"""Run the frozen Stage 10 G confirmation using the existing serial backend."""
import argparse
import hashlib
import itertools
import json
import random
import re
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "stage9_deep"))
import bench

CONFIGS = ((8, 8), (8, 16), (16, 16))
LOADS = ((256, 64), (512, 48), (768, 16), (64, 96))
MODEL = "Q4_K_M"
POWER = "381b4222-f694-41f0-9685-ff5bb260df2e"
ARCHIVE_SHA = "f1f7198a7c1cdf13bec7c2cf960947e0bec87401837a057134186132d870aa04"
SOURCE = [
    HERE / "collect.py", HERE / "analyze.py", HERE / "PLAN.md", HERE / "schedule.json",
    Path(bench.__file__), bench.TOKEN_FILE, Path(bench.base.__file__),
    ROOT / "stage8_extensions/inputs.json", ROOT / "stage8_extensions/results/summary.json",
    Path(bench.previous.__file__), ROOT / "stage7_local/model_manifest.json",
    ROOT / "environment/system_info_v2.json",
    ROOT / "stage10_validation/runs/G_formal_20261127_230001_c5eb06/completion.json",
    ROOT / "stage10_validation/results/G_paired.csv",
]


def now():
    return datetime.now().astimezone().isoformat()


def make_schedule():
    rng = random.Random(2026112710)
    jobs = []
    for block in range(1, 11):
        orders = list(itertools.permutations(CONFIGS))
        rng.shuffle(orders)
        for order in orders:
            number = len(jobs) + 1
            jobs.append(dict(block=block, round=number, order=order, loads=bench.rotate(LOADS, number - 1)))
    return json.loads(json.dumps(jobs))


def signature():
    return {p.relative_to(ROOT).as_posix(): bench.digest(p) for p in SOURCE}


def power_scheme():
    out = subprocess.check_output(["powercfg.exe", "/getactivescheme"])
    match = re.search(rb"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", out)
    return match[0].decode("ascii").lower() if match else None


def power_snapshot():
    return dict(scheme=power_scheme(), battery=bench.previous.power_state(), at=now())


def host():
    state = bench.previous.machine()
    reference = bench.read(ROOT / "environment/system_info_v2.json")
    if (
        state["cpu"].strip() != reference["cpu_model"] or
        state["physical_cores"] != reference["physical_cores"] or
        state["logical_cores"] != reference["logical_cores"] or
        round(state["ram_gib"] * 1024**3) != reference["total_ram_bytes"] or
        "26100" not in state["os"] or state["plugged_in"] is not True or power_scheme() != POWER
    ):
        raise ValueError("Hardware, build, power scheme or AC state differs from the plan")
    return state


def assets():
    archive = bench.base.ASSETS / "llama-b11347-bin-win-cpu-x64.zip"
    if bench.digest(archive) != ARCHIVE_SHA:
        raise ValueError("Official archive hash mismatch")
    with zipfile.ZipFile(archive) as official:
        for name in official.namelist():
            if name.endswith((".dll", ".exe")):
                if bench.digest(bench.base.BACKEND / name) != hashlib.sha256(official.read(name)).hexdigest():
                    raise ValueError(f"Official backend member changed: {name}")
    model = bench.previous.MODELS[MODEL]
    if bench.digest(bench.base.ASSETS / model["filename"]) != model["sha256"]:
        raise ValueError("Incorrect Q4 weights")
    return model["sha256"]


def requests_for(out, group, row, config):
    t, tb = config
    base = [dict(block=row["block"], round=row["round"], group=group, t=t, tb=tb,
                 position=position, n=n, g=g, status="not_attempted")
            for position, (n, g) in enumerate(row["loads"], 1)]
    status = dict(group=group, block=row["block"], round=row["round"], config=config, ok=False)
    try:
        with bench.server(out, group, MODEL, t, tb) as port:
            status["reached_ready"] = True
            warm = bench.send(port, "/completion", dict(**bench.GEN, prompt="Brief backend warmup",
                                                       n_predict=8, ignore_eos=True))
            bench.save(out / f"{group}_warm.json", warm)
            if (warm["timings"]["predicted_n"], warm["timings"]["cache_n"]) != (8, 0) or warm.get("truncated"):
                raise ValueError("Invalid warmup")
            for case in base:
                n, g = case["n"], case["g"]
                payload = dict(**bench.GEN, prompt=bench.IDS[:n], n_predict=g, ignore_eos=True)
                case["request"] = payload
                case["start_ns"] = bench.clock()
                try:
                    reply = bench.send(port, "/completion", payload)
                except Exception as error:
                    case.update(status="request_error", error=repr(error), end_ns=bench.clock())
                    raise
                case["end_ns"] = bench.clock()
                case["response"] = reply
                timing = reply.get("timings", {})
                actual = tuple(timing.get(name) for name in ("prompt_n", "predicted_n", "cache_n"))
                case["status"] = "ok" if actual == (n, g, 0) and not reply.get("truncated") else "protocol_error"
        status["ok"] = True
    except Exception as error:
        status["error"] = repr(error)
    finally:
        bench.save(out / f"{group}_status.json", status)
        for case in base:
            bench.save(out / f"{group}_{case['position']:02}.json", case)
    return status, base


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["freeze", "pilot", "formal"])
    parser.add_argument("--pilot", type=Path)
    args = parser.parse_args()
    schedule = make_schedule()
    if args.phase == "freeze":
        if any((HERE / filename).exists() for filename in ("schedule.json", "freeze.json")):
            raise ValueError("A freeze exists")
        bench.save(HERE / "schedule.json", schedule)
        bench.save(HERE / "freeze.json", dict(at=now(), hashes=signature()))
        print("FROZEN", bench.digest(HERE / "freeze.json"), flush=True)
        return
    frozen = bench.read(HERE / "freeze.json")
    if bench.read(HERE / "schedule.json") != schedule or frozen["hashes"] != signature():
        raise ValueError("Frozen schedule or input changed")
    machine = host()
    weight = assets()
    if args.phase == "formal":
        if args.pilot is None:
            parser.error("formal requires --pilot")
        pilot = bench.read(args.pilot / "completion.json")
        binding = bench.read(args.pilot / "protocol.json")
        if (binding["phase"] != "pilot" or binding["hashes"] != frozen["hashes"] or
                binding["weight"] != weight or pilot["requests"] != {"ok": 12} or
                pilot["groups_ok"] != 3 or not pilot["power_ok"]):
            raise ValueError("Pilot failed or differs from freeze")
    else:
        schedule = schedule[:1]
    if any(proc.info["name"] in ("llama-server.exe", "llama-bench.exe")
           for proc in bench.previous.psutil.process_iter(["name"])):
        raise ValueError("Another backend process is active")
    out = HERE / "runs" / (args.phase + "_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    out.mkdir(parents=True)
    bench.save(out / "protocol.json", dict(phase=args.phase, started=now(), machine=machine, weight=weight,
                                        backend_sha=ARCHIVE_SHA, freeze_sha=bench.digest(HERE / "freeze.json"),
                                        hashes=frozen["hashes"], pilot=args.pilot.name if args.pilot else None))
    bench.save(out / "schedule.json", schedule)
    print("RUN", out.name, flush=True)
    counts = {}
    groups_ok = 0
    power_ok = True
    for row in schedule:
        if (row["round"] - 1) % 6 == 0:
            before = power_snapshot()
            bench.save(out / f"block_{row['block']:02}_before.json", before)
            power_ok &= before["scheme"] == POWER and (before["battery"] or {}).get("plugged_in") is True
        for position, config in enumerate(row["order"], 1):
            label = f"r{row['round']:02}_c{position}"
            group, requests = requests_for(out, label, row, config)
            groups_ok += int(group["ok"])
            for case in requests:
                value = case["status"]
                counts[value] = counts.get(value, 0) + 1
        if row["round"] % 6 == 0 or args.phase == "pilot":
            after = power_snapshot()
            bench.save(out / f"block_{row['block']:02}_after.json", after)
            power_ok &= after["scheme"] == POWER and (after["battery"] or {}).get("plugged_in") is True
            print(f"BLOCK {row['block']}: groups_ok={groups_ok}, requests={counts}", flush=True)
    bench.save(out / "completion.json", dict(finished=now(), rounds=len(schedule), groups_ok=groups_ok,
                                            requests=counts, power_ok=power_ok))
    return 0 if args.phase == "formal" or (power_ok and groups_ok == 3 and counts == {"ok": 12}) else 1


if __name__ == "__main__":
    raise SystemExit(main())
