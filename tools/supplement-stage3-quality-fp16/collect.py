"""Run the separately frozen four-model quality matrix using the Stage 3 runner controls."""
import argparse
import hashlib
import json
import logging
import time
import uuid
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import psutil

from dataset import BANKS, HERE, MODELS, OLDER, ROOT, TEMPLATE, build_user_message, read, write
from llama_server_ctl import LlamaServer, slots_idle
from run_tasks import server_argv_from_config
from score_answer import score_item
from weights import digest, inventory, verify

CONFIG = ROOT / "configs/formal_config.json"
ORIGINAL_FREEZE = ROOT / "datasets/frozen_hashes_stage3.json"
BACKEND = ROOT / "tools/llama.cpp"
ARCHIVE = ROOT / "tools/llama-b11347-bin-win-cpu-x64.zip"
ARCHIVE_SHA = "f1f7198a7c1cdf13bec7c2cf960947e0bec87401837a057134186132d870aa04"
SOURCES = [
    *(HERE / item for item in (*BANKS, "tasks.json", "pilot.json", "data_audit.json", "schedule.json",
                              "model_spec.json", "PLAN.md", "dataset.py", "weights.py", "collect.py", "analyze.py")),
    TEMPLATE, CONFIG, ORIGINAL_FREEZE, ROOT / "scripts/run_tasks.py", ROOT / "scripts/score_answer.py",
    ROOT / "scripts/llama_server_ctl.py", ROOT / "models/model_manifest_v3.json",
    ROOT / "environment/system_info_v2.json", *OLDER,
]


def stamp():
    return datetime.now().astimezone().isoformat()


def signature():
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in SOURCES}


def frozen_original():
    for key in ("datasets/prompt_template.json", "scripts/score_answer.py", "configs/formal_config.json",
                "scripts/run_tasks.py", "scripts/llama_server_ctl.py", "datasets/test/test_tasks.json"):
        if digest(ROOT / key) != read(ORIGINAL_FREEZE)["files"][key]:
            raise ValueError(f"Original Stage 3 dependency changed: {key}")


def power():
    state = psutil.sensors_battery()
    return dict(plugged=state.power_plugged, percent=state.percent) if state else None


def machine():
    import platform
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
        cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    info = dict(cpu=cpu, os=platform.platform(), python=platform.python_version(),
                cores=psutil.cpu_count(logical=False), threads=psutil.cpu_count(),
                ram_bytes=psutil.virtual_memory().total, power=power())
    reference = read(ROOT / "environment/system_info_v2.json")
    if ((info["cpu"], info["cores"], info["threads"], info["ram_bytes"]) !=
        (reference["cpu_model"], reference["physical_cores"], reference["logical_cores"],
         reference["total_ram_bytes"]) or "26100" not in info["os"] or not (info["power"] or {}).get("plugged")):
        raise ValueError("Stage 3 host or AC state differs")
    return info


def assets():
    verified = verify()
    if digest(ARCHIVE) != ARCHIVE_SHA:
        raise ValueError("Wrong official backend archive")
    with zipfile.ZipFile(ARCHIVE) as archive:
        for name in archive.namelist():
            if name.endswith((".dll", ".exe")):
                if digest(BACKEND / name) != hashlib.sha256(archive.read(name)).hexdigest():
                    raise ValueError(f"Incorrect official backend member: {name}")
    return verified


def payload(task, template, config):
    gen = config["generation"]
    return dict(messages=[dict(role="system", content=template["system_prompt"]),
                          dict(role="user", content=build_user_message(task, template))],
                temperature=gen["temperature"], top_p=gen["top_p"], max_tokens=gen["max_tokens"],
                seed=gen["seed"], cache_prompt=False, stream=False)


def score(task, status, response):
    failure = dict(format_correct=False, content_correct=False, correct=False)
    if status != "ok":
        return failure
    text = response["body"]["choices"][0]["message"].get("content") or ""
    values = score_item(task, text)
    return {key: values[key] for key in failure}


def response_body(server, endpoint, data, timeout):
    result = server.request(endpoint, data, timeout)
    if not result["ok"]:
        raise RuntimeError(f"{endpoint}: {result['error']}")
    return result["body"]


def proof(server, data, timeout):
    rendered = response_body(server, "/apply-template",
                             dict(messages=data["messages"], add_generation_prompt=True), timeout)["prompt"]
    tokens = response_body(server, "/tokenize", dict(content=rendered, add_special=True, parse_special=True),
                           timeout)["tokens"]
    if not isinstance(rendered, str) or not tokens or any(type(i) is not int for i in tokens):
        raise ValueError("Malformed input proof")
    return dict(rendered=rendered, tokens=tokens)


def warm(server, config):
    request = dict(messages=[dict(role="user", content="1")], max_tokens=1,
                   temperature=0.0, cache_prompt=False, stream=False)
    attempts = [server.request("/v1/chat/completions", request, config["request_timeout_s"]) for _ in range(3)]
    timings = [((entry.get("body") or {}).get("timings") or {}) for entry in attempts]
    valid = (all(row["ok"] and t.get("cache_n") == 0 for row, t in zip(attempts, timings)) and
             len({t.get("prompt_n") for t in timings}) == 1 and timings[0].get("prompt_n", 0) > 1)
    return dict(passed=valid, requests=attempts)


def trial(server, task, config, template, prior, row):
    data = payload(task, template, config)
    row["payload"] = data
    current = proof(server, data, config["request_timeout_s"])
    row["input_proof"] = current
    if prior.setdefault(task["id"], current) != current:
        raise ValueError("Cross-model prompt or token IDs differ")
    row.update(status="request_failed", attempted=True, started=stamp())
    result = server.request("/v1/chat/completions", data, config["request_timeout_s"])
    row["response"] = result
    if not result["ok"]:
        if "timeout" in (result.get("error") or "").lower() or "timed out" in (result.get("error") or "").lower():
            row["status"] = "timeout"
            row["timeout_recovery"] = slots_idle(server, config["timeout_recovery"]["wait_timeout_s"])
            if not row["timeout_recovery"]["freed"]:
                return False
        elif result.get("http_status") is None:
            return False
        return True
    body = result["body"]
    timings = body.get("timings") or {}
    usage = body.get("usage") or {}
    choice = (body.get("choices") or [{}])[0]
    if (timings.get("cache_n") != 0 or usage.get("prompt_tokens") != len(current["tokens"])
            or timings.get("prompt_n") != len(current["tokens"])):
        row["status"] = "input_or_cache_mismatch"
        return False
    if choice.get("finish_reason") == "length":
        row["status"] = "truncated"
    elif choice.get("finish_reason") == "stop" and isinstance((choice.get("message") or {}).get("content"), str):
        row["status"] = "ok"
    return True


def group(out, number, model, tasks, config, template, prior):
    label = f"batch{number:02}_{model}"
    model_path = ROOT / inventory()[model]["path"]
    logger = logging.getLogger(label)
    rows = [dict(batch=number, model=model, id=task["id"], status="not_attempted", attempted=False) for task in tasks]
    meta = dict(batch=number, model=model, started=stamp(), power_before=power(), cache_check=None)
    with (out / f"{label}.log").open("w", encoding="utf-8") as logfile:
        server = LlamaServer(BACKEND / "llama-server.exe", server_argv_from_config(config, model_path), logfile, logger,
                             memory_sample_interval_s=config["memory_sample_interval_s"])
        try:
            if not (meta["power_before"] or {}).get("plugged"):
                raise RuntimeError("AC state changed")
            server.start()
            ready = server.wait_ready(config["load_timeout_s"], config["health_poll_interval_s"])
            meta.update(ready=ready, pid=server.proc.pid, argv=server.full_argv)
            if not ready["ok"]:
                raise RuntimeError("Service did not become ready")
            meta["cache_check"] = warm(server, config)
            if not meta["cache_check"]["passed"]:
                raise RuntimeError("Startup cache check failed")
            for task, row in zip(tasks, rows):
                try:
                    go_on = trial(server, task, config, template, prior, row)
                except Exception as error:
                    row["reason"] = repr(error)
                    go_on = False
                if row["attempted"]:
                    try:
                        row["score"] = score(task, row["status"], row.get("response"))
                    except Exception as error:
                        row.update(status="scoring_error", reason=repr(error),
                                   score=score(task, "scoring_error", None))
                write(out / f"{label}_{task['id']}.json", row)
                if not go_on:
                    raise RuntimeError(row.get("reason", row["status"]))
        except Exception as error:
            meta["error"] = repr(error)
        finally:
            meta["cleanup"] = server.stop()
            meta["power_after"] = power()
            meta["finished"] = stamp()
            for row in rows:
                path = out / f"{label}_{row['id']}.json"
                if not path.exists():
                    row["reason"] = meta.get("error", "not scheduled")
                    write(path, row)
            write(out / f"{label}_service.json", meta)
    if meta["cleanup"]["attempted"] and not meta["cleanup"]["confirmed_exit"]:
        raise RuntimeError("Process exit unconfirmed; do not start another service")
    return Counter(row["status"] for row in rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["freeze", "pilot", "formal"])
    parser.add_argument("--pilot", type=Path)
    args = parser.parse_args()
    frozen_original()
    if args.phase == "freeze":
        if (HERE / "freeze.json").exists():
            raise ValueError("Freeze already exists")
        write(HERE / "model_spec.json", inventory())
        write(HERE / "freeze.json", dict(at=stamp(), hashes=signature()))
        print("FROZEN", digest(HERE / "freeze.json"), flush=True)
        return
    frozen = read(HERE / "freeze.json")
    if signature() != frozen["hashes"]:
        raise ValueError("Frozen inputs changed")
    state = machine()
    weights = assets()
    if args.phase == "formal":
        if not args.pilot:
            parser.error("formal requires --pilot")
        pilot = read(args.pilot / "completion.json")
        prior_meta = read(args.pilot / "protocol.json")
        if (prior_meta["phase"] != "pilot" or prior_meta["hashes"] != frozen["hashes"]
                or prior_meta["assets"] != weights or pilot["requests"] != {"ok": 16}):
            raise ValueError("Formal run needs a passed, matching pilot")
    if any(p.info["name"] in ("llama-server.exe", "llama-bench.exe") for p in psutil.process_iter(["name"])):
        raise ValueError("Other backend processes are active")
    tasks = read(HERE / ("tasks.json" if args.phase == "formal" else "pilot.json"))
    by_id = {task["id"]: task for task in tasks}
    jobs = read(HERE / "schedule.json") if args.phase == "formal" else [
        dict(batch=0, tasks=list(by_id), models=list(MODELS))
    ]
    out = HERE / "runs" / (args.phase + "_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    out.mkdir(parents=True)
    write(out / "protocol.json", dict(phase=args.phase, started=stamp(), machine=state, assets=weights,
                                     backend_sha=ARCHIVE_SHA, hashes=frozen["hashes"],
                                     pilot=args.pilot.name if args.pilot else None))
    write(out / "schedule.json", jobs)
    print("RUN", out.name, flush=True)
    config, template = read(CONFIG), read(TEMPLATE)
    counts = Counter()
    for slot in jobs:
        prior = {}
        for name in slot["models"]:
            counts.update(group(out, slot["batch"], name, [by_id[i] for i in slot["tasks"]], config, template, prior))
        print(f"BATCH {slot['batch']}: {dict(counts)}", flush=True)
    write(out / "completion.json", dict(finished=stamp(), blocks=len(jobs), requests=dict(counts)))
    return 0 if args.phase == "formal" or counts == {"ok": 16} else 1


if __name__ == "__main__":
    raise SystemExit(main())
