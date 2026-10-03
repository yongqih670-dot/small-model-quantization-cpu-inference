# -*- coding: utf-8 -*-
"""Stage 5 离线结果审计：从原始记录重算全部报告口径的数字。

- 显式绑定 run_id（禁止自动选最新）；
- 完整性检查不止于总数：配置标识、轮次覆盖、task_id 集合、重复、
  HTTP 失败、截断、缺失单元；
- 重算：Stage 3 质量统计与 McNemar、矩阵 B 修订口径统计与内存、
  Stage 4 bench 分档统计（含 T32 pp256 的 ±std 与 CV）、加速比、
  Stage 4 HTTP 每轮五题平均统计与分档内存/启动；
- 与"报告引用值"逐项核对，不一致列入 findings；
- 输出独立目录 results/summary/audit_stage5/（不覆盖任何历史产物）。
"""

import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from score_answer import score_item  # noqa: E402

RUNS = {
    "S3_A": "run_20261003_112023_stage3benchA_a9c6c1",
    "S3_B": "run_20261003_112135_stage3perfB_0ffb9d",
    "S3_C_Q4": "run_20261003_112320_formal_Q4_K_M_7eda40",
    "S3_C_Q5": "run_20261003_112342_formal_Q5_K_M_8b86de",
    "S3_C_Q8": "run_20261003_112411_formal_Q8_0_37181d",
    "S4_A": "run_20261003_121218_stage4bench_a2dc0f",
    "S4_B": "run_20261003_121547_stage4http_f03b1a",
}
RAW = ROOT / "results" / "raw"
OUT = ROOT / "results" / "summary" / "audit_stage5"
MODELS = ["Q4_K_M", "Q5_K_M", "Q8_0"]
TIERS = [1, 2, 4, 8, 16, 32]


def mean(xs):
    return sum(xs) / len(xs)


def sstd(xs):
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def r(x, n=4):
    return None if x is None else round(x, n)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    audit = {"bound_run_ids": RUNS, "checks": [], "findings": []}

    def check(name, ok, detail=""):
        audit["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            audit["findings"].append(f"{name}: {detail}")

    # ============ Stage 3 A ============
    res = json.loads((RAW / RUNS["S3_A"] / "result.json").read_text(encoding="utf-8"))
    invs = res["invocations"]
    n_meas = sum(i["tests_parsed"] for i in invs if i["ok"])
    check("S3_A 15 次调用", len(invs) == 15, str(len(invs)))
    check("S3_A 45 个测量值", n_meas == 45, str(n_meas))
    cfg_ids = {i.get("argv", ["?"])[0] and "llama-bench" for i in invs}
    rounds = Counter(i["round"] for i in invs)
    check("S3_A 每轮 3 次调用×5 轮", all(c == 3 for c in rounds.values())
          and len(rounds) == 5, str(dict(rounds)))
    s3a = {}
    for inv in invs:
        if not inv["ok"]:
            continue
        for t in inv["bench_json"]:
            if t.get("n_prompt") and t.get("n_gen"):
                wl = "pg256+64"
            elif t.get("n_prompt"):
                wl = "pp256"
            else:
                wl = "tg64"
            s3a.setdefault((inv["model"], wl), []).append(t["samples_ts"][0])
    stage3_a = {f"{m}|{wl}": {"n": len(v), "mean": r(mean(v), 3),
                              "std": r(sstd(v), 3)}
                for (m, wl), v in sorted(s3a.items())}
    audit["S3_A_stats"] = stage3_a

    # ============ Stage 3 B ============
    reqs = [json.loads(l) for l in (RAW / RUNS["S3_B"] / "requests_perf.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]
    check("S3_B 75 条请求", len(reqs) == 75, str(len(reqs)))
    check("S3_B 无 HTTP 失败", all(x.get("http_status") == 200 for x in reqs))
    check("S3_B 无截断", all(x.get("finish_reason") != "length" for x in reqs))
    seen = Counter((x["model"], x["task_id"], x["round"]) for x in reqs)
    check("S3_B 无重复(model,task,round)", all(c == 1 for c in seen.values()),
          str([k for k, c in seen.items() if c > 1][:3]))
    check("S3_B 15 个启动单元", len(list((RAW / RUNS["S3_B"]).glob(
        "startup_*.json"))) == 15)
    per_round = {}
    for x in reqs:
        per_round.setdefault(x["model"], {}).setdefault(
            x["round"], []).append(x["end_to_end_client_s"])
    s3b_combo = {}
    for m in MODELS:
        vals = [mean(v) for _, v in sorted(per_round[m].items())]
        s3b_combo[m] = {"rounds": len(vals), "mean": r(mean(vals)),
                        "std": r(sstd(vals))}
    mem_b = {}
    for u in (RAW / RUNS["S3_B"]).glob("startup_*.json"):
        d = json.loads(u.read_text(encoding="utf-8"))
        pw = (d.get("memory") or {}).get("peak_wset_mib")
        if d.get("ok") and pw:
            mem_b.setdefault(d["model"], []).append(pw)
    s3b_mem = {m: {"n": len(v), "mean": r(mean(v), 2), "std": r(sstd(v), 2)}
               for m, v in mem_b.items()}
    audit["S3_B_stats"] = {"fixed_combo_latency": s3b_combo,
                           "memory_5_processes": s3b_mem}

    # ============ Stage 3 C ============
    ds = json.loads((ROOT / "datasets/test/test_tasks.json")
                    .read_text(encoding="utf-8"))
    tasks_by_id = {t["id"]: t for t in ds["tasks"]}
    qual = {}
    for m in MODELS:
        recs = [json.loads(l) for l in (RAW / RUNS[f"S3_C_{m.split('_')[0]}"] / "requests.jsonl")
                .read_text(encoding="utf-8").splitlines() if l.strip()]
        ids = [x["task_id"] for x in recs]
        check(f"S3_C[{m}] 50 个唯一 task_id",
              len(set(ids)) == 50 and len(ids) == 50, f"{len(set(ids))}/{len(ids)}")
        check(f"S3_C[{m}] 配置标识一致",
              len({x.get("config_id") for x in recs}) == 1)
        check(f"S3_C[{m}] 覆盖全部测试集 task_id",
              set(ids) == set(tasks_by_id))
        mismatch = 0
        for x in recs:
            sc = score_item(tasks_by_id[x["task_id"]], x.get("raw_output") or "")
            if (bool(sc["format_correct"]), bool(sc["content_correct"]),
                    bool(sc["correct"])) != (x["score"]["format_correct"],
                                             x["score"]["content_correct"],
                                             x["score"]["correct"]):
                mismatch += 1
        check(f"S3_C[{m}] 冻结评分器重打分一致", mismatch == 0, str(mismatch))
        qual[m] = {
            "format": sum(1 for x in recs if x["score"]["format_correct"]),
            "content": sum(1 for x in recs if x["score"]["content_correct"]),
            "joint": sum(1 for x in recs if x["score"]["correct"]),
            "truncated": sum(1 for x in recs
                             if x.get("finish_reason") == "length"),
            "http_failed": sum(1 for x in recs if x.get("http_status") != 200)}
        n = 50
        p = qual[m]["joint"] / n
        half = 1.96 * math.sqrt(p * (1 - p) / n + 1.96 ** 2 / (4 * n * n)) / \
            (1 + 1.96 ** 2 / n)
        center = (p + 1.96 ** 2 / (2 * n)) / (1 + 1.96 ** 2 / n)
        qual[m]["joint_wilson95"] = (r(center - half), r(center + half))
    audit["S3_C_quality"] = qual

    def mcnemar(b, c):
        n = b + c
        if n == 0:
            return 1.0
        k = min(b, c)
        return min(1.0, 2 * sum(math.comb(n, i) / 2 ** n
                                for i in range(k + 1)))

    def joint_map(m):
        return {x["task_id"]: bool(x["score"]["correct"]) for x in
                [json.loads(l) for l in
                 (RAW / RUNS[f"S3_C_{m.split('_')[0]}"] / "requests.jsonl")
                 .read_text(encoding="utf-8").splitlines() if l.strip()]}
    mcn = {}
    for a, b in [("Q4_K_M", "Q5_K_M"), ("Q4_K_M", "Q8_0"), ("Q5_K_M", "Q8_0")]:
        ja, jb = joint_map(a), joint_map(b)
        aonly = sum(1 for t in ja if ja[t] and not jb[t])
        bonly = sum(1 for t in ja if jb[t] and not ja[t])
        mcn[f"{a}_vs_{b}"] = {"a_only": aonly, "b_only": bonly,
                              "p": r(mcnemar(aonly, bonly), 6)}
    audit["S3_C_mcnemar"] = mcn

    # ============ Stage 4 A ============
    res4 = json.loads((RAW / RUNS["S4_A"] / "result.json").read_text(encoding="utf-8"))
    rb = json.loads((RAW / RUNS["S4_A"] / "result_rebuilt.json").is_file() and
                    (RAW / RUNS["S4_A"] / "result_rebuilt.json").read_text(
                        encoding="utf-8") or "{}")
    units = [json.loads(m.read_text(encoding="utf-8"))
             for m in sorted((RAW / RUNS["S4_A"]).glob("unit_*.json"))
             if "_partial" not in m.name]
    check("S4_A 30 个完整单元", len(units) == 30, str(len(units)))
    check("S4_A 重建汇总 90 个测量值",
          rb.get("measurements_total") == 90, str(rb.get("measurements_total")))
    tseen = Counter((u["round"], u["tier"]) for u in units)
    check("S4_A 每轮6档×5轮", all(c == 1 for c in tseen.values())
          and len(tseen) == 30, str([k for k, c in tseen.items() if c > 1]))
    check("S4_A 单元三态全真",
          all(u.get("execution_complete") and u.get("metrics_valid")
              and u.get("cleanup_success") for u in units))
    s4a = {}
    for u in units:
        for t in u.get("tests", []):
            if t.get("valid"):
                s4a.setdefault((u["tier"], t["workload"]), []).append(t["tok_s"])
    s4a_stats = {}
    for (tier, wl), v in sorted(s4a.items()):
        s4a_stats[f"T{tier}|{wl}"] = {
            "n": len(v), "mean": r(mean(v), 3), "std": r(sstd(v), 3),
            "cv_percent": r(sstd(v) / mean(v) * 100, 2)
            if sstd(v) is not None else None}
    check("S4_A 各档各负载 5 个样本",
          all(d["n"] == 5 for d in s4a_stats.values()),
          str({k: d["n"] for k, d in s4a_stats.items() if d["n"] != 5}))
    audit["S4_A_stats"] = s4a_stats
    speed = {}
    for wl in ("pp256", "tg64", "pg256+64"):
        base = s4a_stats[f"T1|{wl}"]["mean"]
        speed[wl] = {f"T{t}": r(s4a_stats[f"T{t}|{wl}"]["mean"] / base, 3)
                     for t in TIERS}
    audit["S4_A_speedup_vs_T1"] = speed

    # ============ Stage 4 B ============
    hreqs = [json.loads(l) for l in (RAW / RUNS["S4_B"] / "requests_threads.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    check("S4_B 150 条计时请求", len(hreqs) == 150, str(len(hreqs)))
    check("S4_B 无 HTTP 失败", all(x.get("http_status") == 200 for x in hreqs))
    check("S4_B 无重复(unit,task)",
          all(c == 1 for c in Counter((x["unit_key"], x["task_id"])
                                      for x in hreqs).values()))
    check("S4_B 输出逐档一致（贪心）",
          all(len({(x["raw_output"] or "").strip()
                   for x in hreqs if x["task_id"] == tid}) == 1
              for tid in {x["task_id"] for x in hreqs}))
    hunits = [json.loads(m.read_text(encoding="utf-8"))
              for m in sorted((RAW / RUNS["S4_B"]).glob("unit_*.json"))
              if "_partial" not in m.name]
    check("S4_B 30 个完整单元", len(hunits) == 30, str(len(hunits)))
    s4b_combo = {}
    for t in TIERS:
        by_round = {}
        for x in hreqs:
            if x["tier"] == t and x.get("http_status") == 200:
                by_round.setdefault(x["round"], []).append(
                    x["end_to_end_client_s"])
        vals = [mean(v) for _, v in sorted(by_round.items())]
        s4b_combo[f"T{t}"] = {"rounds": len(vals), "mean": r(mean(vals)),
                              "std": r(sstd(vals))}
    s4b_mem, s4b_start = {}, {}
    for u in hunits:
        pw = (u.get("memory") or {}).get("peak_wset_mib")
        if u.get("ok") and pw:
            s4b_mem.setdefault(u["tier"], []).append(pw)
        st = (u.get("server_startup") or {}).get("server_startup_to_ready_s")
        if st is not None:
            s4b_start.setdefault(u["tier"], []).append(st)
    audit["S4_B_stats"] = {
        "fixed_combo_latency": s4b_combo,
        "memory_per_tier": {f"T{t}": {"n": len(v), "mean": r(mean(v), 2),
                                      "std": r(sstd(v), 2)}
                            for t, v in sorted(s4b_mem.items())},
        "startup_per_tier": {f"T{t}": {"n": len(v), "mean": r(mean(v), 3)}
                             for t, v in sorted(s4b_start.items())}}

    # ============ 报告引用值核对 ============
    claims = {
        "S3 质量联合 16/17/15": [qual[m]["joint"] for m in MODELS] == [16, 17, 15],
        "S3 质量格式 21/22/20": [qual[m]["format"] for m in MODELS] == [21, 22, 20],
        "S3 质量内容 21/24/22": [qual[m]["content"] for m in MODELS] == [21, 24, 22],
        "S3 B 组合延迟 Q4≈0.3591": abs(s3b_combo["Q4_K_M"]["mean"] - 0.3591) < 5e-4,
        "S3 B 内存 Q4≈518.93": abs(s3b_mem["Q4_K_M"]["mean"] - 518.93) < 0.05,
        "S3 tg Q4 105.36±0.71": abs(stage3_a["Q4_K_M|tg64"]["mean"] - 105.36) < 5e-3
        and abs(stage3_a["Q4_K_M|tg64"]["std"] - 0.71) < 5e-3,
        "S4 tg T8 121.08": abs(s4a_stats["T8|tg64"]["mean"] - 121.08) < 5e-3,
        "S4 延迟 T16 0.2492": abs(s4b_combo["T16"]["mean"] - 0.2492) < 5e-4,
        "S4 pp 加速 T32 7.572": abs(speed["pp256"]["T32"] - 7.572) < 5e-3,
        "S4 内存 T32 534.86": abs(mean(s4b_mem[32]) - 534.86) < 0.05,
    }
    audit["report_claim_checks"] = claims
    for k, ok in claims.items():
        check(f"报告引用值: {k}", ok)

    (OUT / "audit_results.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"检查 {len(audit['checks'])} 项, 发现问题 {len(audit['findings'])} 项")
    for f in audit["findings"]:
        print(" -", f)
    print("关键重算值:")
    print("  S4 T32|pp256:", s4a_stats.get("T32|pp256"))
    print("  S4 tg 分档:", {f"T{t}": s4a_stats[f"T{t}|tg64"]["mean"] for t in TIERS})
    print("  S4 延迟分档:", {k: v["mean"] for k, v in s4b_combo.items()})
    return 0 if not audit["findings"] else 1


if __name__ == "__main__":
    sys.exit(main())
