# -*- coding: utf-8 -*-
"""Stage 4 分析：线程数实验汇总与图表。

规则（docs/STAGE4_PLAN.md §分析与推荐规则）：
- 每档每工作负载：5 轮原始值、均值、样本标准差、变异系数（CV）；
- 加速比以本阶段 T=1 为基准；效率≠CPU 利用率，SMT≠物理核；
- HTTP 按题统计 + 每轮五题平均的 5 轮统计；内存/启动按独立进程统计；
- 各指标分别报告最佳观测档位；3% 实用推荐规则（非统计等价）；
- 缺失测量不填 0：单元/轮缺失时在 missing 中如实报告，均值只按现有样本计。

用法（显式绑定 run_id，禁止自动选最新）:
    python scripts/analyze_stage4.py --run-bench <run_id> --run-http <run_id>
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

TIERS = [1, 2, 4, 8, 16, 32]
OUT = ROOT / "results" / "summary"
PLOTS = ROOT / "plots"


def mean(xs):
    return sum(xs) / len(xs)


def sstd(xs):
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def workload_of(entry: dict) -> str:
    if entry.get("n_prompt") and entry.get("n_gen"):
        return "pg256+64"
    if entry.get("n_prompt"):
        return "pp256"
    return "tg64"


def aggregate_bench_units(units: list) -> dict:
    """从已完成 bench 单元聚合分档统计；缺失档位如实报告（不填 0）。"""
    per = {t: {wl: [] for wl in ("pp256", "tg64", "pg256+64")}
           for t in TIERS}
    rounds_seen = {t: [] for t in TIERS}
    for u in units:
        if not u.get("ok"):
            continue
        t = u["tier"]
        rounds_seen[t].append(u["round"])
        for test in u.get("tests", []):
            if test.get("valid"):
                per[t][test["workload"]].append(
                    {"round": u["round"], "tok_s": test["tok_s"]})
    stats = {}
    missing = []
    for t in TIERS:
        for wl in ("pp256", "tg64", "pg256+64"):
            rows = per[t][wl]
            vals = [r["tok_s"] for r in rows]
            if not vals:
                missing.append({"tier": t, "workload": wl, "n": 0})
                stats[f"T{t}|{wl}"] = {"n": 0, "missing": True}
                continue
            m, s = mean(vals), sstd(vals)
            stats[f"T{t}|{wl}"] = {
                "n": len(vals), "rounds": sorted(r["round"] for r in rows),
                "samples_tok_s": vals, "mean_tok_s": round(m, 3),
                "sample_std_tok_s": round(s, 4) if s is not None else None,
                "cv_percent": round(s / m * 100, 2) if s is not None and m else None}
            if len(vals) < 5:
                missing.append({"tier": t, "workload": wl, "n": len(vals)})
    return {"stats": stats, "missing": missing}


def recommend(stats: dict, workload: str, threshold: float = 0.03) -> dict:
    """预注册推荐规则：较少线程均值距最佳观测 ≤3% 则优先推荐较少线程。"""
    key = lambda t: stats.get(f"T{t}|{workload}", {})
    valid = [(t, key(t)["mean_tok_s"]) for t in TIERS
             if key(t).get("n", 0) > 0]
    if not valid:
        return {"workload": workload, "recommendation": None,
                "reason": "无有效样本"}
    best_tier, best_mean = max(valid, key=lambda x: x[1])
    candidates = [(t, m) for t, m in valid
                  if t <= best_tier and m >= best_mean * (1 - threshold)]
    rec_tier = min(t for t, _ in candidates)
    return {"workload": workload, "best_observed_tier": best_tier,
            "best_mean_tok_s": round(best_mean, 2),
            "recommended_tier": rec_tier,
            "recommended_mean_tok_s": round(dict(valid)[rec_tier], 2),
            "note": "3% 为实用选择阈值，不是统计等价证明"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-bench", required=True)
    ap.add_argument("--run-http", required=True)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    PLOTS.mkdir(exist_ok=True)
    rb = ROOT / "results/raw" / args.run_bench
    rh = ROOT / "results/raw" / args.run_http
    for p in (rb, rh):
        if not p.is_dir():
            print(f"run 目录不存在: {p}")
            return 1

    summary = {"bound_run_ids": {"bench": args.run_bench, "http": args.run_http},
               "note": "全部数字来自显式绑定的 run；缺失测量不填 0"}

    # ---------- 矩阵 A ----------
    res_b = json.loads((rb / "result.json").read_text(encoding="utf-8"))
    bench_units = []
    for m in sorted(rb.glob("unit_*.json")):
        if "_partial" in m.name:
            continue
        d = json.loads(m.read_text(encoding="utf-8"))
        if d.get("matrix") is None and d.get("tests") is not None:
            d.setdefault("tier", d.get("tier"))
            bench_units.append(d)
    # unit 文件里没有显式 matrix 字段，用 tests 结构判断（bench 单元含 tests）
    agg = aggregate_bench_units(bench_units)
    summary["matrix_A"] = {
        "run_id": args.run_bench,
        "units_completed": res_b.get("units_completed"),
        "units_expected": res_b.get("units_expected"),
        "measurements": res_b.get("measurements_total"),
        "integrity_problems": agg["missing"],
        "per_tier": agg["stats"]}
    recs = {wl: recommend(agg["stats"], wl)
            for wl in ("pp256", "tg64", "pg256+64")}
    summary["matrix_A"]["recommendations"] = recs
    # 加速比（以本阶段 T=1 为基准）
    speedup = {}
    for wl in ("pp256", "tg64", "pg256+64"):
        base = agg["stats"][f"T1|{wl}"].get("mean_tok_s")
        if not base:
            continue
        speedup[wl] = {t: round(agg["stats"][f"T{t}|{wl}"]["mean_tok_s"] / base, 3)
                       for t in TIERS
                       if agg["stats"][f"T{t}|{wl}"].get("n", 0) > 0}
    summary["matrix_A"]["speedup_vs_T1"] = {
        "baseline": "本阶段 T=1", "values": speedup,
        "note": "按线程数归一化的效率不是 CPU 利用率；SMT 逻辑线程≠物理核"}
    with open(OUT / "stage4_bench.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["tier", "workload", "round", "tok_s"])
        for t in TIERS:
            for wl in ("pp256", "tg64", "pg256+64"):
                for r in agg["stats"][f"T{t}|{wl}"].get("samples_tok_s", []):
                    pass
        # 直接按样本写（保留轮次）
        for t in TIERS:
            for wl in ("pp256", "tg64", "pg256+64"):
                st = agg["stats"][f"T{t}|{wl}"]
                for i, v in enumerate(st.get("samples_tok_s", [])):
                    w.writerow([t, wl, (st.get("rounds") or [None])[i], v])

    # ---------- 矩阵 B ----------
    res_h = json.loads((rh / "result.json").read_text(encoding="utf-8"))
    http_reqs = [json.loads(l) for l in
                 (rh / "requests_threads.jsonl").read_text(encoding="utf-8")
                 .splitlines() if l.strip()]
    per_task = {}
    per_round_avg = {}
    for t in TIERS:
        recs_t = [r for r in http_reqs if r["tier"] == t
                  and r.get("http_status") == 200]
        for tid in set(r["task_id"] for r in recs_t):
            vals = [r["end_to_end_client_s"] for r in recs_t if r["task_id"] == tid]
            per_task[f"T{t}|{tid}"] = {
                "n": len(vals), "mean_s": round(mean(vals), 4),
                "sample_std_s": round(sstd(vals), 4) if sstd(vals) is not None else None}
        by_round = {}
        for r in recs_t:
            by_round.setdefault(r["round"], []).append(r["end_to_end_client_s"])
        vals = [mean(v) for _, v in sorted(by_round.items())]
        if vals:
            per_round_avg[t] = {"rounds": len(vals), "mean_s": round(mean(vals), 4),
                                "sample_std_s": round(sstd(vals), 4)}
        else:
            summary.setdefault("http_missing", []).append({"tier": t})
    # 内存与启动（独立进程，每档 5 个）
    mem, startup = {}, {}
    for m in sorted(rh.glob("unit_*.json")):
        if "_partial" in m.name:
            continue
        d = json.loads(m.read_text(encoding="utf-8"))
        if not d.get("ok"):
            continue
        t = d["tier"]
        pw = (d.get("memory") or {}).get("peak_wset_mib")
        if pw:
            mem.setdefault(t, []).append(pw)
        st = (d.get("server_startup") or {}).get("server_startup_to_ready_s")
        if st is not None:
            startup.setdefault(t, []).append(st)
    mem_stats = {t: {"n": len(v), "mean_mib": round(mean(v), 2),
                     "sample_std_mib": round(sstd(v), 2) if sstd(v) else None,
                     "peaks_mib": v} for t, v in sorted(mem.items())}
    startup_stats = {t: {"n": len(v), "mean_s": round(mean(v), 3),
                         "sample_std_s": round(sstd(v), 3) if sstd(v) else None}
                     for t, v in sorted(startup.items())}
    summary["matrix_B"] = {
        "run_id": args.run_http,
        "units_completed": res_h.get("units_completed"),
        "timed_requests": res_h.get("timed_requests_total"),
        "aux_requests": res_h.get("aux_requests_total"),
        "per_task": per_task,
        "per_round_5task_avg": per_round_avg,
        "memory_per_tier_5processes": mem_stats,
        "startup_to_ready_per_tier": startup_stats,
        "note": "输出长度随档位可能不同（贪心解码下同题输出应一致，如不同将如实呈现）"}
    with open(OUT / "stage4_http_latency.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["tier", "round", "task_id", "e2e_s", "http_status"])
        for r in http_reqs:
            w.writerow([r["tier"], r["round"], r["task_id"],
                        r["end_to_end_client_s"], r["http_status"]])

    # ---------- 图 ----------
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2))
    for ax, wl, title in zip(
            axes, ("pp256", "tg64", "pg256+64"),
            ("提示处理 pp256", "生成 tg64", "组合 pg256+64（组合指标）")):
        xs = [t for t in TIERS if agg["stats"][f"T{t}|{wl}"].get("n", 0) > 0]
        ms = [agg["stats"][f"T{t}|{wl}"]["mean_tok_s"] for t in xs]
        ss = [agg["stats"][f"T{t}|{wl}"]["sample_std_tok_s"] or 0 for t in xs]
        ax.errorbar(xs, ms, yerr=ss, marker="o", capsize=4)
        ax.set_xscale("log", base=2)
        ax.set_xticks(TIERS)
        ax.set_xticklabels(TIERS)
        ax.set_xlabel("线程数 T（统一线程预算，32=SMT 逻辑线程）")
        ax.set_ylabel("tok/s")
        ax.set_title(f"{title}\n5轮均值±样本标准差")
    fig.suptitle("矩阵A：Q4_K_M 吞吐随线程数变化（llama.cpp b11347, CPU only）")
    fig.tight_layout()
    fig.savefig(PLOTS / "stage4_throughput_vs_threads.png", dpi=150)

    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    for wl, color in zip(("pp256", "tg64", "pg256+64"),
                         ("#4c72b0", "#55a868", "#c44e52")):
        if wl not in speedup:
            continue
        xs = sorted(int(k) for k in speedup[wl])
        ax.plot(xs, [speedup[wl][str(t)] if str(t) in speedup[wl]
                     else speedup[wl][t] for t in xs],
                marker="o", label=wl, color=color)
    ax.axhline(1.0, color="gray", linestyle="--", linewidth=1)
    ax.set_xscale("log", base=2)
    ax.set_xticks(TIERS)
    ax.set_xticklabels(TIERS)
    ax.set_xlabel("线程数 T")
    ax.set_ylabel("相对本阶段 T=1 的加速比")
    ax.set_title("吞吐加速比（基准=本阶段 T=1；效率≠CPU利用率，SMT≠物理核）")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "stage4_speedup.png", dpi=150)

    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    xs = [t for t in TIERS if t in per_round_avg]
    ms = [per_round_avg[t]["mean_s"] for t in xs]
    ss = [per_round_avg[t]["sample_std_s"] or 0 for t in xs]
    ax.errorbar(xs, ms, yerr=ss, marker="o", capsize=4, color="#8172b2")
    ax.set_xscale("log", base=2)
    ax.set_xticks(TIERS)
    ax.set_xticklabels(TIERS)
    ax.set_xlabel("线程数 T")
    ax.set_ylabel("秒")
    ax.set_title("矩阵B：固定任务组合端到端延迟\n每轮五题平均后再取5轮统计"
                 "（每档25条请求/5轮；另有90条辅助预热/缓存请求不计入）")
    fig.tight_layout()
    fig.savefig(PLOTS / "stage4_latency_vs_threads.png", dpi=150)

    fig, ax1 = plt.subplots(figsize=(7.5, 4.4))
    xs = [t for t in TIERS if t in mem_stats]
    ms = [mem_stats[t]["mean_mib"] for t in xs]
    ss = [mem_stats[t]["sample_std_mib"] or 0 for t in xs]
    ax1.errorbar(xs, ms, yerr=ss, marker="s", capsize=4, color="#c44e52",
                 label="进程峰值内存 (MiB)")
    ax1.set_xscale("log", base=2)
    ax1.set_xticks(TIERS)
    ax1.set_xticklabels(TIERS)
    ax1.set_xlabel("线程数 T")
    ax1.set_ylabel("MiB")
    ax2 = ax1.twinx()
    xs2 = [t for t in TIERS if t in startup_stats]
    ax2.plot(xs2, [startup_stats[t]["mean_s"] for t in xs2], "o--",
             color="#4c72b0", label="启动到就绪 (s)")
    ax2.set_ylabel("秒", color="#4c72b0")
    ax1.set_title("内存与启动耗时（各档 5 个独立服务进程，均值±样本标准差）")
    fig.tight_layout()
    fig.savefig(PLOTS / "stage4_memory_startup.png", dpi=150)

    (OUT / "stage4_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("矩阵A 缺失:", agg["missing"] if agg["missing"] else "无")
    for wl in ("pp256", "tg64", "pg256+64"):
        s = summary["matrix_A"]["recommendations"][wl]
        print(f"推荐[{wl}]: 最佳档={s.get('best_observed_tier')} "
              f"({s.get('best_mean_tok_s')} tok/s) -> 推荐档={s.get('recommended_tier')}")
    print("加速比:", json.dumps(speedup, ensure_ascii=False))
    print("B 每轮五题平均:", json.dumps(per_round_avg, ensure_ascii=False))
    print("内存:", json.dumps(mem_stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
