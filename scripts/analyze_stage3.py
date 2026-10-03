# -*- coding: utf-8 -*-
"""Stage 3 分析：从原始记录生成汇总与图表（不修改任何原始记录）。

输入（自动定位最新 run 目录）：
- 矩阵 A: results/raw/run_*_stage3benchA_*/  (45 个工作负载测量值)
- 矩阵 B: results/raw/run_*_stage3perfB_*/requests_perf.jsonl (75 条计时请求)
- 矩阵 C: results/raw/run_*_formal_Q*/requests.jsonl (3×50 正式评测)

输出：
- results/summary/stage3_summary.json      全部汇总指标
- results/summary/stage3_bench.csv         逐(模型×工作负载)五轮样本
- results/summary/stage3_quality_items.csv 逐题三模型对照
- plots/*.png                              四张对照图
"""

import csv
import glob
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

MODELS = ["Q4_K_M", "Q5_K_M", "Q8_0"]
OUT = ROOT / "results" / "summary"
PLOTS = ROOT / "plots"


def latest(pattern: str) -> Path:
    hits = sorted(glob.glob(str(ROOT / "results/raw" / pattern)))
    if not hits:
        raise FileNotFoundError(pattern)
    return Path(hits[-1])


def mean(xs):
    return sum(xs) / len(xs)


def sample_std(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def wilson(k, n, z=1.96):
    """Wilson 比例置信区间。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(center - half, 4), round(center + half, 4))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    PLOTS.mkdir(exist_ok=True)
    summary = {"analysis_note": "全部数字来自 results/raw 原始记录；"
                                "文件大小用 manifest 实际字节数，不用 bench 的 model_size"}

    # ---------- 矩阵 A ----------
    dir_a = latest("run_*_stage3benchA_*")
    res_a = json.loads((dir_a / "result.json").read_text(encoding="utf-8"))
    per_mu_wl = {}
    for inv in res_a["invocations"]:
        if not inv["ok"]:
            continue
        for t in inv["bench_json"]:
            if t.get("n_prompt") and t.get("n_gen"):
                wl = "pg256+64"
            elif t.get("n_prompt"):
                wl = "pp256"
            else:
                wl = "tg64"
            per_mu_wl.setdefault((inv["model"], wl), []).append(
                {"round": inv["round"], "tok_s": t["samples_ts"][0],
                 "avg_ts": t["avg_ts"], "samples_ns": t["samples_ns"]})
    bench_summary = {}
    for (model, wl), rows in sorted(per_mu_wl.items()):
        vals = [r["tok_s"] for r in rows]
        bench_summary[f"{model}|{wl}"] = {
            "rounds": [r["round"] for r in rows],
            "samples_tok_s": vals,
            "mean_tok_s": round(mean(vals), 3),
            "sample_std_tok_s": round(sample_std(vals), 3),
            "n": len(vals)}
    summary["matrix_A"] = {"run_id": res_a["run_id"],
                           "measurement_count": res_a["workload_measurements"],
                           "per_model_workload": bench_summary}

    with open(OUT / "stage3_bench.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["model", "workload", "round", "tok_s", "n"])
        for (model, wl), rows in sorted(per_mu_wl.items()):
            for r in rows:
                w.writerow([model, wl, r["round"], r["tok_s"], len(rows)])

    # ---------- 矩阵 B ----------
    dir_b = latest("run_*_stage3perfB_*")
    b_records = [json.loads(l) for l in (dir_b / "requests_perf.jsonl")
                 .read_text(encoding="utf-8").splitlines() if l.strip()]
    b_summary = {}
    for model in MODELS:
        recs = [r for r in b_records if r["model"] == model]
        e2e = [r["end_to_end_client_s"] for r in recs if r.get("http_status") == 200]
        b_summary[model] = {
            "timed_requests": len(recs),
            "http_ok": len(e2e),
            "e2e_mean_s": round(mean(e2e), 4) if e2e else None,
            "e2e_sample_std_s": round(sample_std(e2e), 4) if e2e else None,
            "e2e_all_samples_s": [round(x, 4) for x in e2e],
        }
    summary["matrix_B"] = {"run_id": dir_b.name, "per_model": b_summary}

    # ---------- 矩阵 C：正式质量 ----------
    quality = {}
    items_table = []
    for model in MODELS:
        d = latest(f"run_*_formal_{model}_*")
        res = json.loads((d / "result.json").read_text(encoding="utf-8"))
        recs = [json.loads(l) for l in (d / "requests.jsonl")
                .read_text(encoding="utf-8").splitlines() if l.strip()]
        n = len(recs)
        fmt = sum(1 for r in recs if r["score"]["format_correct"])
        con = sum(1 for r in recs if r["score"]["content_correct"])
        cor = sum(1 for r in recs if r["score"]["correct"])
        per_type = {}
        for r in recs:
            pt = per_type.setdefault(r["task_type"], {
                "total": 0, "format": 0, "content": 0, "joint": 0})
            pt["total"] += 1
            pt["format"] += int(r["score"]["format_correct"])
            pt["content"] += int(r["score"]["content_correct"])
            pt["joint"] += int(r["score"]["correct"])
        truncated = sum(1 for r in recs if r.get("truncated"))
        failed = sum(1 for r in recs if r.get("http_status") != 200)
        quality[model] = {
            "run_id": res["run_id"], "n_tasks": n,
            "execution_complete": res.get("execution_complete"),
            "quality_evaluation_valid": res.get("quality_evaluation_valid"),
            "format_correct": fmt, "content_correct": con, "joint_correct": cor,
            "joint_wilson95": wilson(cor, n),
            "truncated": truncated, "request_failed": failed,
            "peak_process_memory_mib": (res.get("memory") or {}).get("peak_wset_mib"),
            "sampled_peak_rss_mib": (res.get("memory") or {})
            .get("sampled_peak_rss_mib"),
            "per_type": per_type}
        for r in recs:
            items_table.append({
                "task_id": r["task_id"], "task_type": r["task_type"],
                "model": model,
                "format": int(r["score"]["format_correct"]),
                "content": int(r["score"]["content_correct"]),
                "joint": int(r["score"]["correct"]),
                "truncated": int(bool(r.get("truncated"))),
                "raw_output": (r.get("raw_output") or "")[:80]})

    summary["matrix_C"] = quality
    with open(OUT / "stage3_quality_items.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["task_id", "task_type", "model",
                                          "format", "content", "joint",
                                          "truncated", "raw_output"])
        w.writeheader()
        w.writerows(items_table)

    # ---------- 文件大小（manifest 实际字节数） ----------
    man = json.loads((ROOT / "models/model_manifest_v3.json")
                     .read_text(encoding="utf-8"))
    sizes = {m["quantization"]: {"size_bytes": m["size_bytes"],
                                 "size_mib": m["size_mib"]} for m in man["models"]}
    summary["file_sizes_actual_bytes"] = sizes

    # ---------- 图 1: 文件大小 + 进程峰值内存 ----------
    fig, ax = plt.subplots(figsize=(7, 4.2))
    x = range(len(MODELS))
    w = 0.38
    size_mib = [sizes[m]["size_mib"] for m in MODELS]
    mem_mib = [quality[m]["peak_process_memory_mib"] or 0 for m in MODELS]
    b1 = ax.bar([i - w / 2 for i in x], size_mib, w, label="模型文件大小 (MiB)")
    b2 = ax.bar([i + w / 2 for i in x], mem_mib, w,
                label="进程峰值内存 peak_wset (MiB)")
    ax.bar_label(b1, fmt="%.0f"); ax.bar_label(b2, fmt="%.0f")
    ax.set_xticks(list(x)); ax.set_xticklabels(MODELS)
    ax.set_ylabel("MiB"); ax.set_title(
        "文件大小 vs 进程峰值内存（矩阵C运行, t=4, ctx=1024）")
    ax.legend(); fig.tight_layout()
    fig.savefig(PLOTS / "stage3_size_vs_memory.png", dpi=150)

    # ---------- 图 2: pp/tg/pg 均值±标准差 ----------
    fig, axes = plt.subplots(1, 3, figsize=(12, 4))
    for ax, wl, title in zip(axes, ["pp256", "tg64", "pg256+64"],
                             ["提示处理 pp256", "生成 tg64",
                              "组合 pg256+64（组合指标，非生成速度）"]):
        means = [bench_summary[f"{m}|{wl}"]["mean_tok_s"] for m in MODELS]
        stds = [bench_summary[f"{m}|{wl}"]["sample_std_tok_s"] for m in MODELS]
        bars = ax.bar(MODELS, means, yerr=stds, capsize=5,
                      color=["#8ab4d8", "#a8c8a0", "#d8b48a"])
        ax.bar_label(bars, fmt="%.1f")
        ax.set_title(f"{title}\n(5轮均值±样本标准差, n=5)")
        ax.set_ylabel("tok/s")
    fig.suptitle("矩阵A: 固定工作量 llama-bench（t=4, fa=off, r=5轮×r1）")
    fig.tight_layout()
    fig.savefig(PLOTS / "stage3_bench_pp_tg_pg.png", dpi=150)

    # ---------- 图 3: 实际请求端到端延迟 ----------
    fig, ax = plt.subplots(figsize=(7, 4.2))
    means = [b_summary[m]["e2e_mean_s"] for m in MODELS]
    stds = [b_summary[m]["e2e_sample_std_s"] for m in MODELS]
    bars = ax.bar(MODELS, means, yerr=stds, capsize=5,
                  color=["#8ab4d8", "#a8c8a0", "#d8b48a"])
    ax.bar_label(bars, fmt="%.3f")
    ax.set_ylabel("秒"); ax.set_title(
        "矩阵B: 实际任务端到端延迟（5题×5轮, n=75, 均值±样本标准差）\n"
        "各模型输出长度不同，属实际任务响应比较")
    fig.tight_layout()
    fig.savefig(PLOTS / "stage3_e2e_latency.png", dpi=150)

    # ---------- 图 4: 质量-速度/内存对照 ----------
    fig, ax1 = plt.subplots(figsize=(8, 4.6))
    xs = range(len(MODELS))
    joint = [quality[m]["joint_correct"] for m in MODELS]
    lo = [quality[m]["joint_wilson95"][0] * 50 for m in MODELS]
    hi = [quality[m]["joint_wilson95"][1] * 50 for m in MODELS]
    err_lo = [j - l for j, l in zip(joint, lo)]
    err_hi = [h - j for j, h in zip(joint, hi)]
    bars = ax1.bar(xs, joint, yerr=[err_lo, err_hi], capsize=5,
                   color=["#8ab4d8", "#a8c8a0", "#d8b48a"], width=0.45)
    ax1.bar_label(bars, fmt="%.0f")
    ax1.set_ylabel("联合正确题数 / 50（误差条=Wilson 95% CI）")
    ax1.set_ylim(0, 55)
    ax1.set_xticks(list(xs)); ax1.set_xticklabels(MODELS)
    ax2 = ax1.twinx()
    tg = [bench_summary[f"{m}|tg64"]["mean_tok_s"] for m in MODELS]
    ax2.plot(xs, tg, "o--", color="#c0392b", label="tg64 生成速度 (tok/s)")
    for i, v in enumerate(tg):
        ax2.annotate(f"{v:.0f}", (i, v), textcoords="offset points",
                     xytext=(8, 6), color="#c0392b")
    ax2.set_ylabel("tg64 tok/s", color="#c0392b")
    ax1.set_title("质量—速度对照（矩阵C 正式50题 × 矩阵A tg64）\n"
                  "配置: t=4, ctx=1024, temp=0, max_tokens=96")
    fig.tight_layout()
    fig.savefig(PLOTS / "stage3_quality_vs_speed.png", dpi=150)

    (OUT / "stage3_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 控制台摘要
    print("=== 矩阵 A（tok/s, 5轮均值±样本标准差）===")
    for m in MODELS:
        for wl in ("pp256", "tg64", "pg256+64"):
            s = bench_summary[f"{m}|{wl}"]
            print(f"  {m:7s} {wl:9s} {s['mean_tok_s']:9.2f} ± {s['sample_std_tok_s']:.2f} (n={s['n']})")
    print("=== 矩阵 B（端到端秒）===")
    for m in MODELS:
        s = b_summary[m]
        print(f"  {m:7s} {s['e2e_mean_s']:.4f} ± {s['e2e_sample_std_s']:.4f} (n={s['http_ok']}/{s['timed_requests']})")
    print("=== 矩阵 C（正式质量 /50）===")
    for m in MODELS:
        s = quality[m]
        print(f"  {m:7s} 格式={s['format_correct']:2d} 内容={s['content_correct']:2d} "
              f"联合={s['joint_correct']:2d} Wilson95={s['joint_wilson95']} "
              f"截断={s['truncated']} 失败={s['request_failed']} "
              f"峰值内存={s['peak_process_memory_mib']}MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
