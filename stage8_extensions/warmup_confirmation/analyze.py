"""Audit the 100 planned pairs and analyze only the new primary measurements."""
import argparse
import csv
import math
import statistics
from collections import Counter
from pathlib import Path

import scipy
from scipy.stats import t

from run import ARCHIVE_SHA, FROZEN, HERE, MODEL, PLAN, STAGE, common, read, schedule

METRICS = ("startup_text_ms", "startup_ready_ms", "first_ttft_ms", "ready_to_request_ms")


def summarize(values, confidence):
    n = len(values)
    result = {"n": n, "mean": None, "sd": None, "median": None, "min": None, "max": None, "ci": None}
    if n:
        result.update(mean=statistics.mean(values), median=statistics.median(values),
                      min=min(values), max=max(values))
    if n >= 2:
        sd = statistics.stdev(values)
        margin = float(t.ppf((1 + confidence) / 2, n - 1)) * sd / math.sqrt(n)
        result.update(sd=sd, ci=[result["mean"] - margin, result["mean"] + margin])
    return result


def verify_stream(response):
    expected = dict(**common.GEN, messages=common.messages(common.INPUT["first"]),
                    max_tokens=96, stream=True)
    if response["payload"] != expected:
        raise ValueError("Unexpected first request payload")
    events = response["events"]
    visible = [ev["ns"] for ev in events for choice in ev["chunk"]["choices"]
               if (choice["delta"].get("content") or "").strip()]
    if (not visible or visible[0] != response["first_ns"] or
            not response["start_ns"] <= visible[0] <= events[-1]["ns"] <= response["done_ns"]):
        raise ValueError("Inconsistent first visible event")
    if not any(choice.get("finish_reason") == "stop" for ev in events for choice in ev["chunk"]["choices"]):
        return None
    return "".join(choice["delta"].get("content") or "" for ev in events for choice in ev["chunk"]["choices"])


def measure(source, job, position, warm, batch_ok):
    label = f"r{job['round']:03}_{position}"
    row = read(source / f"{label}.json")
    if (row["batch"], row["round"], row["position"], row["warmup"]) != (job["batch"], job["round"], position, warm):
        raise ValueError(f"Schedule mismatch for {label}")
    record = dict(batch=job["batch"], round=job["round"], position=position, warmup=warm,
                  valid=False, reason="", first_output="", second_error=row.get("second_error", ""),
                  **dict.fromkeys(METRICS))
    if not row["ok"]:
        record["reason"] = "capture_failed"
        return record
    server = read(source / f"{label}_server.json")
    flags = server["flags"]
    expected = [*common.previous.FLAGS]
    if not warm:
        expected.append("--no-warmup")
    if (server["model"] != MODEL or server["warmup"] != warm or
            flags[:-1] != [*expected, "--host", "127.0.0.1", "--port"]):
        raise ValueError(f"Backend control mismatch for {label}")
    if not batch_ok or any((server[key] or {}).get("plugged_in") is not True
                           for key in ("power_before", "power_after")):
        record["reason"] = "power_changed"
        return record
    response = row["first"]
    output = verify_stream(response)
    if output is None:
        record["reason"] = "first_stream_not_stopped_normally"
        return record
    started, ready = server["start_ns"], server["ready_ns"]
    request, visible = response["start_ns"], response["first_ns"]
    if not started < ready <= request <= visible:
        raise ValueError(f"Invalid clock order for {label}")
    record.update(valid=True, first_output=output,
                  startup_text_ms=(visible - started) / 1e6, startup_ready_ms=(ready - started) / 1e6,
                  first_ttft_ms=(visible - request) / 1e6, ready_to_request_ms=(request - ready) / 1e6)
    return record


def write_table(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.run
    plan = read(PLAN)
    jobs = schedule(plan)
    protocol = read(source / "protocol.json")
    complete = read(source / "completion.json")
    hashes = {p.relative_to(STAGE.parent).as_posix(): common.digest(p) for p in FROZEN}
    if (read(source / "schedule.json") != jobs or protocol["hashes"] != hashes or
            protocol["expected_attempts"] != 2 * len(jobs) or complete["attempted"] != 2 * len(jobs) or
            protocol["backend_sha256"] != ARCHIVE_SHA or
            protocol["model_sha256"] != common.previous.MODELS[MODEL]["sha256"]):
        raise ValueError("Run or frozen file mismatch")

    previous_power_ok = protocol["power_scheme"] == plan["power_scheme"] and protocol["machine"]["plugged_in"]
    batch_power = {}
    for batch in range(1, 11):
        checkpoint = read(source / f"batch_{batch:02}.json")
        if (checkpoint["batch"], checkpoint["attempted"]) != (batch, batch * 20):
            raise ValueError(f"Bad batch checkpoint {batch}")
        power_ok = (checkpoint["power_scheme"] == plan["power_scheme"] and
                    (checkpoint["power"] or {}).get("plugged_in") is True)
        batch_power[batch] = previous_power_ok and power_ok
        previous_power_ok = power_ok

    sessions, pairs = [], []
    for job in jobs:
        batch_ok = batch_power[job["batch"]]
        pair = [measure(source, job, position, warm, batch_ok)
                for position, warm in enumerate(job["warmup"], 1)]
        sessions.extend(pair)
        on, off = sorted(pair, key=lambda row: not row["warmup"])
        valid = on["valid"] and off["valid"]
        pairs.append(dict(batch=job["batch"], round=job["round"], on_first=job["warmup"][0],
                          valid=valid, on_reason=on["reason"], off_reason=off["reason"],
                          **{name: off[name] - on[name] if valid else None for name in METRICS}))
    if sum(s["reason"] != "capture_failed" for s in sessions) != complete["capture_ok"]:
        raise ValueError("Capture count mismatch")

    valid = [row for row in pairs if row["valid"]]
    valid_rounds = {row["round"] for row in valid}
    confidence = plan["analysis"]["confidence"]
    paired = {name: summarize([row[name] for row in valid], confidence) for name in METRICS}
    primary = [row[METRICS[0]] for row in valid]
    interval = paired[METRICS[0]]["ci"]
    upper = interval[1] if interval else None
    threshold = plan["analysis"]["meaningful_improvement_ms"]
    summary = dict(
        run=source.name, machine=protocol["machine"], preregistration=protocol["preregistration"],
        scipy=scipy.__version__, schedule_sha256=common.digest(PLAN), confidence=confidence,
        planned_pairs=len(jobs), attempted_sessions=len(sessions), valid_pairs=len(valid),
        invalid_sessions=dict(Counter(s["reason"] for s in sessions if not s["valid"])),
        secondary_capture_errors=sum(bool(s["second_error"]) for s in sessions),
        by_warmup={
            key: {name: summarize([s[name] for s in sessions if s["round"] in valid_rounds
                                   and s["warmup"] == warm], confidence) for name in METRICS}
            for key, warm in (("on", True), ("off", False))
        },
        off_minus_on=paired,
        off_faster=sum(v < 0 for v in primary), on_faster=sum(v > 0 for v in primary),
        ties=sum(v == 0 for v in primary),
        upper_below_zero=upper < 0 if upper is not None else None,
        upper_below_minus_threshold=upper < -threshold if upper is not None else None,
        by_batch=[dict(batch=batch, **summarize([r[METRICS[0]] for r in valid if r["batch"] == batch], confidence))
                  for batch in range(1, 11)],
    )
    args.output.mkdir(parents=True, exist_ok=False)
    write_table(args.output / "sessions.csv", sessions)
    write_table(args.output / "pairs.csv", pairs)
    common.save(args.output / "summary.json", summary)
    print(f"Verified {len(sessions)} attempts and {len(valid)} valid pairs", flush=True)


if __name__ == "__main__":
    main()
