"""Audit every scheduled request, then recompute paired Stage 10 G comparisons."""
import argparse
import csv
import itertools
import math
import statistics
from collections import Counter
from pathlib import Path

import scipy
from scipy.stats import binomtest, t

from collect import ARCHIVE_SHA, CONFIGS, HERE, LOADS, MODEL, POWER, bench, make_schedule, signature


def name(config):
    return f"{config[0]}/{config[1]}"


def ac(record):
    return bool(record and record.get("plugged_in") is True)


def interval(values):
    result = dict(n=len(values), mean=None, sd=None, upper95=None, ci95=None)
    if values:
        result["mean"] = statistics.mean(values)
    if len(values) > 1:
        df = len(values) - 1
        sd = statistics.stdev(values)
        se = sd / math.sqrt(len(values))
        mean = result["mean"]
        two = float(t.ppf(.975, df)) * se
        result.update(sd=sd, upper95=mean + float(t.ppf(.95, df)) * se,
                      ci95=[mean - two, mean + two])
    return result


def table(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def audit(run):
    protocol = bench.read(run / "protocol.json")
    complete = bench.read(run / "completion.json")
    frozen = bench.read(HERE / "freeze.json")
    jobs = make_schedule()
    previous = bench.read(run.parent / protocol["pilot"] / "completion.json")
    pilot = bench.read(run.parent / protocol["pilot"] / "protocol.json")
    if (protocol["phase"] != "formal" or complete["rounds"] != 60 or
            bench.read(run / "schedule.json") != jobs or bench.read(HERE / "schedule.json") != jobs or
            protocol["hashes"] != frozen["hashes"] or signature() != frozen["hashes"] or
            bench.digest(HERE / "freeze.json") != protocol["freeze_sha"] or
            protocol["backend_sha"] != ARCHIVE_SHA or
            protocol["weight"] != bench.previous.MODELS[MODEL]["sha256"] or
            pilot["hashes"] != protocol["hashes"] or pilot["phase"] != "pilot" or
            not previous["power_ok"] or previous["requests"] != {"ok": 12}):
        raise ValueError("Run, pilot or frozen inputs do not agree")

    blocks = {}
    for block in range(1, 11):
        endpoints = [bench.read(run / f"block_{block:02}_{point}.json") for point in ("before", "after")]
        blocks[block] = all(x["scheme"] == POWER and ac(x["battery"]) for x in endpoints)
    rows = []
    cleanups = 0
    for job in jobs:
        for slot, config in enumerate(job["order"], 1):
            label = f"r{job['round']:02}_c{slot}"
            status = bench.read(run / f"{label}_status.json")
            if status["config"] != config or status["group"] != label:
                raise ValueError(f"Group mismatch {label}")
            cleanups += int(status["ok"])
            path = run / f"{label}_server.json"
            service = bench.read(path) if path.exists() else None
            flags = bench.previous.FLAGS.copy()
            for switch, value in zip(("-t", "-tb"), config):
                flags[flags.index(switch) + 1] = str(value)
            if service and (service["flags"] != flags or service["model"] != MODEL):
                raise ValueError(f"Server config mismatch {label}")
            wall_power = bool(service and ac(service["power_before"]) and ac(service["power_after"]))
            for position, (n, g) in enumerate(job["loads"], 1):
                raw = bench.read(run / f"{label}_{position:02}.json")
                if (raw["block"], raw["round"], raw["group"], raw["t"], raw["tb"], raw["position"],
                    raw["n"], raw["g"]) != (job["block"], job["round"], label, *config, position, n, g):
                    raise ValueError(f"Unexpected scheduled workload {label}, {position}")
                eligible = raw["status"] == "ok" and wall_power and blocks[job["block"]]
                if raw["status"] == "ok":
                    timings = raw["response"]["timings"]
                    expected = dict(**bench.GEN, prompt=bench.IDS[:n], n_predict=g, ignore_eos=True)
                    if (raw["request"] != expected or
                            (timings["prompt_n"], timings["predicted_n"], timings["cache_n"]) != (n, g, 0) or
                            raw["response"].get("truncated") or raw["end_ns"] <= raw["start_ns"]):
                        raise ValueError(f"Invalid successful request {label}, {position}")
                rows.append(dict(block=job["block"], round=job["round"], config=name(config),
                                 n=n, g=g, position=position, status=raw["status"], eligible=eligible,
                                 reason="" if eligible else raw["status"] if raw["status"] != "ok" else "power",
                                 elapsed_ms=(raw["end_ns"] - raw["start_ns"]) / 1e6 if eligible else None))
    if (len(rows) != 720 or dict(Counter(row["status"] for row in rows)) != complete["requests"] or
            cleanups != complete["groups_ok"]):
        raise ValueError("Run accounting mismatch")
    return protocol, rows, blocks


def compare(rows, candidate, reference, n, g):
    pair_rows = []
    for number in range(1, 61):
        c = rows[number, candidate, n, g]
        r = rows[number, reference, n, g]
        valid = c["eligible"] and r["eligible"]
        a, b = c["elapsed_ms"], r["elapsed_ms"]
        pair_rows.append(dict(block=c["block"], round=number, candidate=candidate, reference=reference,
                              n=n, g=g, complete=valid, candidate_ms=a, reference_ms=b,
                              candidate_status=c["reason"], reference_status=r["reason"],
                              delta_ms=a - b if valid else None, margin_ms=a - 1.03 * b if valid else None))
    valid = [row for row in pair_rows if row["complete"]]
    margin = interval([row["margin_ms"] for row in valid])
    summary = dict(candidate=candidate, reference=reference, n=n, g=g, pairs=len(valid), missing=60-len(valid),
                   candidate_mean_ms=statistics.mean(row["candidate_ms"] for row in valid) if valid else None,
                   reference_mean_ms=statistics.mean(row["reference_ms"] for row in valid) if valid else None,
                   delta_ms=interval([row["delta_ms"] for row in valid]), margin_ms=margin,
                   passes=margin["upper95"] is not None and margin["upper95"] < 0)
    summary["mean_ratio"] = summary["candidate_mean_ms"] / summary["reference_mean_ms"] if valid else None
    return summary, pair_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    protocol, rows, blocks = audit(args.run)
    lookup = {(r["round"], r["config"], r["n"], r["g"]): r for r in rows}
    comparisons, pairs = [], []
    for n, g in LOADS:
        for ref in ("8/8", "8/16"):
            summary, detail = compare(lookup, "16/16", ref, n, g)
            comparisons.append(summary)
            pairs.extend(detail)
    secondary, detail = compare(lookup, "8/16", "8/8", 64, 96)
    pairs.extend(detail)
    failures, imbalance = [], []
    for config in map(name, CONFIGS):
        for n, g in LOADS:
            group = [r for r in rows if (r["config"], r["n"], r["g"]) == (config, n, g)]
            failures.append(dict(config=config, n=n, g=g, total=len(group),
                                 statuses=dict(Counter(r["reason"] or "ok" for r in group))))
    failed = {(r["round"], r["config"]) for r in rows if not r["eligible"]}
    if failed:
        for a, b in itertools.combinations(map(name, CONFIGS), 2):
            discord = [((number, a) in failed, (number, b) in failed) for number in range(1, 61)]
            x = discord.count((True, False))
            y = discord.count((False, True))
            imbalance.append(dict(a=a, b=b, a_only=x, b_only=y,
                                  p_exact=float(binomtest(x, x+y).pvalue) if x+y else None))
    by_block = []
    for case in comparisons:
        for block in range(1, 11):
            values = [row["margin_ms"] for row in pairs if row["complete"] and
                      (row["candidate"], row["reference"], row["n"], row["g"], row["block"]) ==
                      (case["candidate"], case["reference"], case["n"], case["g"], block)]
            by_block.append(dict(block=block, candidate="16/16", reference=case["reference"],
                                 n=case["n"], g=case["g"], pairs=len(values),
                                 mean_margin_ms=statistics.mean(values) if values else None))
    args.output.mkdir(parents=True, exist_ok=False)
    table(args.output / "requests.csv", rows)
    table(args.output / "pairs.csv", pairs)
    table(args.output / "blocks.csv", by_block)
    bench.save(args.output / "summary.json", dict(run=args.run.name, machine=protocol["machine"],
               scipy=scipy.__version__, eligible=sum(row["eligible"] for row in rows),
               block_power=blocks, primary=comparisons, secondary=secondary,
               confirmed=all(row["passes"] for row in comparisons), failures=failures,
               missingness_diagnostic=imbalance))
    print("Verified", len(rows), "planned rows;", sum(row["eligible"] for row in rows), "eligible", flush=True)


if __name__ == "__main__":
    main()
