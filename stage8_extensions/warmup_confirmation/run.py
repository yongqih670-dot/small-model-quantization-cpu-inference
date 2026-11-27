"""Collect the frozen Stage 8 A warmup confirmation on one AC-powered host."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGE = HERE.parent
sys.path.insert(0, str(STAGE))
import common
from experiment_A import stream

PLAN = HERE / "plan.json"
ARCHIVE_SHA = "f1f7198a7c1cdf13bec7c2cf960947e0bec87401837a057134186132d870aa04"
MODEL = "Q4_K_M"
FROZEN = [
    PLAN, HERE / "run.py", HERE / "analyze.py", STAGE / "experiment_A.py",
    STAGE / "common.py", STAGE / "inputs.json", STAGE / "PROTOCOL.md",
    STAGE.parent / "stage7_local/run_experiment.py",
    STAGE.parent / "stage7_local/model_manifest.json",
]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def schedule(plan):
    rows = plan["schedule"]
    if len(rows) != 100:
        raise ValueError("Expected 100 frozen pairs")
    for number, row in enumerate(rows, 1):
        if (row["round"], row["batch"]) != (number, (number - 1) // 10 + 1):
            raise ValueError("Unexpected round or batch")
        if sorted(row["warmup"]) != [False, True]:
            raise ValueError("Every pair needs both conditions")
    for offset in range(0, len(rows), 10):
        if sum(row["warmup"][0] for row in rows[offset:offset + 10]) != 5:
            raise ValueError("Unbalanced order in batch")
    return rows


def power_scheme():
    raw = subprocess.check_output(["powercfg.exe", "/getactivescheme"])
    match = re.search(rb"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", raw)
    return match.group().decode("ascii").lower() if match else None


def check_host(plan):
    actual = common.previous.machine()
    expected = plan["machine"]
    if (actual["cpu"].strip() != expected["cpu"] or
            actual["physical_cores"] != expected["physical_cores"] or
            actual["logical_cores"] != expected["logical_cores"] or
            round(actual["ram_gib"] * 1024**3) != expected["ram_bytes"] or
            expected["windows_build"] not in actual["os"] or
            actual["plugged_in"] is not True or power_scheme() != plan["power_scheme"]):
        raise ValueError("Host, Windows build or power differs from the frozen plan")
    return actual


def verify_assets():
    archive = common.ASSETS / "llama-b11347-bin-win-cpu-x64.zip"
    if common.digest(archive) != ARCHIVE_SHA:
        raise ValueError("Backend archive mismatch")
    with zipfile.ZipFile(archive) as official:
        for name in official.namelist():
            if name.endswith((".exe", ".dll")):
                if common.digest(common.BACKEND / name) != hashlib.sha256(official.read(name)).hexdigest():
                    raise ValueError(f"Backend member mismatch: {name}")
    model = common.previous.MODELS[MODEL]
    if common.digest(common.ASSETS / model["filename"]) != model["sha256"]:
        raise ValueError("Q4 weight hash mismatch")
    return model["sha256"]


def capture(out, label, job, position, warm):
    def collect(row):
        with common.server(out, label, MODEL, warm) as (port, _):
            row["first"] = stream(port, common.INPUT["first"])
            try:
                row["second"] = stream(port, common.INPUT["second"])
            except Exception as error:
                row["second_error"] = repr(error)

    return common.attempt(out, label, dict(batch=job["batch"], round=job["round"],
                                          position=position, warmup=warm), collect)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preregistration", help="Optional commit containing the frozen plan")
    parser.add_argument("--check", action="store_true", help="Verify inputs and machine without starting a service")
    args = parser.parse_args()
    plan = read(PLAN)
    jobs = schedule(plan)
    host = check_host(plan)
    weight = verify_assets()
    if args.check:
        print("Frozen schedule, machine, AC power, backend and model verified", flush=True)
        return

    now = datetime.now().astimezone()
    out = HERE / "runs" / (now.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    out.mkdir(parents=True, exist_ok=False)
    protocol = {
        "started": now.isoformat(), "machine": host, "power_scheme": power_scheme(),
        "backend_sha256": ARCHIVE_SHA, "model_sha256": weight,
        "preregistration": args.preregistration, "expected_attempts": 2 * len(jobs),
        "hashes": {p.relative_to(STAGE.parent).as_posix(): common.digest(p) for p in FROZEN},
    }
    common.save(out / "protocol.json", protocol)
    common.save(out / "schedule.json", jobs)
    print("RUN", out.name, flush=True)
    attempted = ok = 0
    for job in jobs:
        number = job["round"]
        for position, warm in enumerate(job["warmup"], 1):
            label = f"r{number:03}_{position}"
            result = capture(out, label, job, position, warm)
            attempted += 1
            ok += int(result["ok"])
        if number % 10 == 0:
            common.save(out / f"batch_{job['batch']:02}.json", {
                "batch": job["batch"], "attempted": attempted, "capture_ok": ok,
                "finished": datetime.now().astimezone().isoformat(),
                "power_scheme": power_scheme(), "power": common.previous.power_state(),
            })
            print(f"BATCH {job['batch']}/10  attempts={attempted}  capture_ok={ok}", flush=True)
    common.save(out / "completion.json", {
        "finished": datetime.now().astimezone().isoformat(), "attempted": attempted,
        "capture_ok": ok, "power_scheme": power_scheme(), "power": common.previous.power_state(),
    })


if __name__ == "__main__":
    main()
