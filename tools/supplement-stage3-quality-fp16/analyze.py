"""Audit the first responses and recompute the stratified paired quality analysis."""
import argparse
import csv
import math
import statistics
from collections import Counter
from pathlib import Path

import scipy
from scipy.stats import t

from collect import ARCHIVE_SHA, CONFIG, HERE, TEMPLATE, payload, score, signature
from dataset import KINDS, MODELS, read, write

METRICS = ("format_correct", "content_correct", "correct")


def paired(a, b, metric, tasks, data):
    strata, components = [], []
    for kind in KINDS:
        pairs = [(data[a, task["id"]], data[b, task["id"]]) for task in tasks if task["task_type"] == kind]
        values = [int(x[metric]) - int(y[metric]) for x, y in pairs if x[metric] is not None and y[metric] is not None]
        n = len(values)
        strata.append(dict(type=kind, pairs=n, planned=len(pairs), plus=values.count(1),
                           minus=values.count(-1), ties=values.count(0),
                           mean=statistics.mean(values) if n else None))
        if n >= 2:
            components.append((statistics.variance(values) / (16*n), n-1))
    outcome = dict(candidate=a, reference=b, metric=metric, strata=strata,
                   total_pairs=sum(s["pairs"] for s in strata), estimate=None, lower95=None, ci95=None)
    if all(row["pairs"] > 0 for row in strata):
        estimate = statistics.mean(row["mean"] for row in strata)
        outcome["estimate"] = estimate
        if len(components) == len(KINDS):
            variance = sum(value for value, _ in components)
            if variance:
                df = variance**2 / sum(v*v / d for v, d in components)
                lower = estimate - float(t.ppf(.95, df)) * math.sqrt(variance)
                half = float(t.ppf(.975, df)) * math.sqrt(variance)
            else:
                lower, half = estimate, 0.0
            outcome.update(lower95=lower, ci95=[estimate-half, estimate+half])
    return outcome


def csv_save(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        format_ = csv.DictWriter(stream, fieldnames=list(rows[0]))
        format_.writeheader()
        format_.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    protocol = read(args.run / "protocol.json")
    completion = args.run / "completion.json"
    completed = read(completion) if completion.exists() else None
    pilot_protocol = read(args.run.parent / protocol["pilot"] / "protocol.json")
    pilot_completion = read(args.run.parent / protocol["pilot"] / "completion.json")
    frozen = read(HERE / "freeze.json")
    tasks = read(HERE / "tasks.json")
    by_id = {task["id"]: task for task in tasks}
    plan = read(HERE / "schedule.json")
    if (protocol["phase"] != "formal" or protocol["hashes"] != frozen["hashes"] or
            signature() != frozen["hashes"] or read(args.run / "schedule.json") != plan or
            protocol["backend_sha"] != ARCHIVE_SHA or
            pilot_protocol["assets"] != protocol["assets"] or
            pilot_protocol["hashes"] != protocol["hashes"] or
            pilot_completion["requests"] != {"ok": 16}):
        raise ValueError("Frozen input, backend or pilot mismatch")
    config, template = read(CONFIG), read(TEMPLATE)
    rows, clean_services = [], 0
    data, input_proofs = {}, {}
    for batch in plan:
        for model in batch["models"]:
            label = f"batch{batch['batch']:02}_{model}"
            service_file = args.run / f"{label}_service.json"
            if service_file.exists():
                service = read(service_file)
                clean_services += (service["cleanup"]["confirmed_exit"] and
                                   (service["power_before"] or {}).get("plugged") is True and
                                   (service["power_after"] or {}).get("plugged") is True)
            for identifier in batch["tasks"]:
                expected = payload(by_id[identifier], template, config)
                file = args.run / f"{label}_{identifier}.json"
                raw = read(file) if file.exists() else dict(attempted=False, status="not_attempted")
                attempted = raw["attempted"]
                if attempted:
                    if not service_file.exists() or (raw["model"], raw["batch"], raw["id"]) != (model, batch["batch"], identifier):
                        raise ValueError(f"Record binding mismatch: {file}")
                    if raw["payload"] != expected:
                        raise ValueError(f"Payload mismatch: {file}")
                    proof = raw["input_proof"]
                    if proof != input_proofs.setdefault(identifier, proof):
                        raise ValueError(f"Same-task token stream differs: {file}")
                    grade = score(by_id[identifier], raw["status"], raw["response"])
                    if grade != raw["score"]:
                        raise ValueError(f"Offline grading mismatch: {file}")
                else:
                    grade = dict.fromkeys(METRICS)
                row = dict(batch=batch["batch"], model=model, id=identifier,
                           type=by_id[identifier]["task_type"], status=raw["status"], attempted=attempted, **grade)
                rows.append(row)
                data[model, identifier] = row
    if len(rows) != 2560 or len(data) != 2560:
        raise ValueError("Unexpected planned coverage")
    if completed and completed["requests"] != dict(Counter(row["status"] for row in rows)):
        raise ValueError("Completion counts do not match first response records")
    primary = paired("Q4_K_M", "FP16", "correct", tasks, data)
    primary["confirmed"] = (primary["total_pairs"] == 640 and clean_services == 64 and
                            all(row["status"] != "input_or_cache_mismatch" for row in rows) and
                            primary["lower95"] is not None and primary["lower95"] > -.05)
    others = [paired(model, "FP16", metric, tasks, data) for model in MODELS[:-1] for metric in METRICS
              if (model, metric) != ("Q4_K_M", "correct")]
    per_model = {}
    for model in MODELS:
        selected = [row for row in rows if row["model"] == model]
        attempted = sum(row["attempted"] for row in selected)
        per_model[model] = dict(planned=len(selected), attempted=attempted,
                                counts=dict(Counter(row["status"] for row in selected)),
                                **{metric: sum(row[metric] is True for row in selected) for metric in METRICS})
    args.output.mkdir(parents=True, exist_ok=False)
    csv_save(args.output / "tasks_scored.csv", rows)
    write(args.output / "summary.json", dict(run=args.run.name, machine=protocol["machine"], scipy=scipy.__version__,
          completed_record=bool(completed), clean_services=clean_services, per_model=per_model,
          planned=2560, attempted=sum(row["attempted"] for row in rows), primary=primary, secondary=others))
    print("Recomputed first responses for", sum(row["attempted"] for row in rows), "/ 2560 attempts", flush=True)


if __name__ == "__main__":
    main()
