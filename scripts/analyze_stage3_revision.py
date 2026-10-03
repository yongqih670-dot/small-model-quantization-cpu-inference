# -*- coding: utf-8 -*-
"""Stage 3 分析修订（版本化，不覆盖原产物）。

修订要点（用户验收要求）：
- 显式绑定 Stage 3 的 run_id，禁止自动选最新目录，禁止 0 填充缺失；
- 分析前完整性核验：A=15 调用/45 测量、B=15 启动单元/75 请求、
  C=每模型恰 50 个唯一 task_id；报告失败/缺失/重复，不静默跳过；
- 用冻结评分器从原始输出**重打分**并与记录得分核对；
- 矩阵 B 统计重构：模型×题目五轮统计 + 每轮五题平均后的五轮统计；
  原 25 条混合标准差保留但标注"任务混合分布"；
- 内存主比较改用 B 的 5 次独立进程峰值（均值/标准差/散点）；
  C 单次内存仅作补充；
- 三组 2×2 配对表 + 双侧精确 McNemar + 逐题分歧列表（事后探索性分析，
  3 组比较，注明 Bonferroni 口径 α=0.05/3）；
- 共同输出模式统计（分类题首标签选择、JSON 围栏、未提供触发）。

输出：
- results/summary/stage3rev_summary.json
- results/summary/stage3rev_matrixB_latency.csv
- results/summary/stage3rev_mcnemar.csv
- plots/stage3rev_*.png
"""

import csv
import json
import math
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from score_answer import score_item  # noqa: E402  （冻结评分器，未改动）

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

# ---- 显式 run_id 绑定（Stage 3 正式产物，禁止自动选最新） ----
RUN_A = "run_20261003_112023_stage3benchA_a9c6c1"
RUN_B = "run_20261003_112135_stage3perfB_0ffb9d"
RUN_C = {
    "Q4_K_M": "run_20261003_112320_formal_Q4_K_M_7eda40",
    "Q5_K_M": "run_20261003_112342_formal_Q5_K_M_8b86de",
    "Q8_0": "run_20261003_112411_formal_Q8_0_37181d",
}
MODELS = ["Q4_K_M", "Q5_K_M", "Q8_0"]
RAW = ROOT / "results" / "raw"
OUT = ROOT / "results" / "summary"
PLOTS = ROOT / "plots"
TEST_SET = ROOT / "datasets/test/test_tasks.json"


def mean(xs):
    return sum(xs) / len(xs)


def sstd(xs):
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def mcnemar_exact(b: int, c: int):
    """双侧精确 McNemar：p = min(1, 2*P(X<=min(b,c))), X~Bin(b+c, .5)。"""
    n = b + c
    if n == 0:
        return {"b": b, "c": c, "p_value": 1.0, "n_discordant": 0}
    k = min(b, c)
    pmf_le = sum(math.comb(n, i) / 2 ** n for i in range(0, k + 1))
    return {"b": b, "c": c, "n_discordant": n,
            "p_value": round(min(1.0, 2 * pmf_le), 6)}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    PLOTS.mkdir(exist_ok=True)
    problems = []
    summary = {"bound_run_ids": {"A": RUN_A, "B": RUN_B, "C": RUN_C},
               "revision_note": "本文件为 Stage 3 分析的版本化修订；"
                                "原 stage3_summary.json 保留不覆盖"}

    # ---------- A 完整性 ----------
    res_a = json.loads((RAW / RUN_A / "result.json").read_text(encoding="utf-8"))
    invs = res_a["invocations"]
    ok_inv = [i for i in invs if i["ok"]]
    n_meas = sum(i["tests_parsed"] for i in invs if i["ok"])
    summary["A_integrity"] = {
        "invocations": len(invs), "invocations_ok": len(ok_inv),
        "measurements": n_meas, "expected": 45}
    if len(invs) != 15 or n_meas != 45:
        problems.append(f"A: 调用 {len(invs)}/15, 测量 {n_meas}/45")

    # ---------- B 完整性 + 重构统计 ----------
    reqs = [json.loads(l) for l in (RAW / RUN_B / "requests_perf.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]
    units = sorted((RAW / RUN_B).glob("startup_*.json"))
    failed_reqs = [r for r in reqs if r.get("http_status") != 200]
    per_model_task_round = {}
    for r in reqs:
        if r.get("http_status") != 200:
            continue
        per_model_task_round.setdefault((r["model"], r["task_id"]), {})[
            r["round"]] = r["end_to_end_client_s"]
    # 重复检测：同 (model, task, round) 多条
    seen = {}
    duplicates = []
    for r in reqs:
        key = (r["model"], r["task_id"], r["round"])
        if key in seen:
            duplicates.append(key)
        seen[key] = True
    b_stats = {"units": len(units), "requests": len(reqs),
               "expected_requests": 75, "http_failed": len(failed_reqs),
               "duplicates": duplicates,
               "units_ok": sum(1 for u in units
                               if json.loads(u.read_text(encoding="utf-8"))
                               .get("ok") is True)}
    if len(reqs) != 75 or len(units) != 15:
        problems.append(f"B: 请求 {len(reqs)}/75, 单元 {len(units)}/15")
    # 模型×题目 五轮统计
    per_task = {}
    for (model, tid), rounds in sorted(per_model_task_round.items()):
        vals = list(rounds.values())
        per_task[f"{model}|{tid}"] = {
            "rounds_present": sorted(rounds.keys()),
            "n": len(vals), "mean_s": round(mean(vals), 4),
            "sample_std_s": round(sstd(vals), 4) if sstd(vals) is not None else None}
    # 每轮五题平均 → 五轮统计（固定任务组合延迟）
    per_round_avg = {}
    for model in MODELS:
        by_round = {}
        for r in reqs:
            if r["model"] == model and r.get("http_status") == 200:
                by_round.setdefault(r["round"], []).append(
                    r["end_to_end_client_s"])
        round_avgs = {rd: mean(v) for rd, v in sorted(by_round.items())}
        vals = list(round_avgs.values())
        per_round_avg[model] = {
            "round_avg_s": {str(k): round(v, 4) for k, v in round_avgs.items()},
            "mean_s": round(mean(vals), 4), "sample_std_s": round(sstd(vals), 4),
            "rounds": len(vals)}
    # 任务混合分布（保留但改标签）
    mixed = {}
    for model in MODELS:
        vals = [r["end_to_end_client_s"] for r in reqs
                if r["model"] == model and r.get("http_status") == 200]
        mixed[model] = {"n": len(vals), "mean_s": round(mean(vals), 4),
                        "std_label": "任务混合分布(含题间差异), 非重复运行波动",
                        "std_s": round(sstd(vals), 4)}
    # 内存：B 的 5 次独立进程峰值
    mem_b = {}
    for model in MODELS:
        peaks = []
        for u in units:
            d = json.loads(u.read_text(encoding="utf-8"))
            if d.get("model") == model and d.get("ok"):
                pw = (d.get("memory") or {}).get("peak_wset_mib")
                if pw:
                    peaks.append(pw)
        mem_b[model] = {"n": len(peaks), "peaks_mib": peaks,
                        "mean_mib": round(mean(peaks), 2) if peaks else None,
                        "sample_std_mib": round(sstd(peaks), 2)
                        if sstd(peaks) is not None else None}
    summary["B_revised"] = {"integrity": b_stats,
                            "per_model_task": per_task,
                            "per_round_5task_avg": per_round_avg,
                            "mixed_distribution_kept_labeled": mixed,
                            "memory_from_B_5_processes": mem_b}
    with open(OUT / "stage3rev_matrixB_latency.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["model", "task_id", "round", "e2e_s"])
        for r in reqs:
            w.writerow([r["model"], r["task_id"], r["round"],
                        r["end_to_end_client_s"]])

    # ---------- C 完整性 + 重打分核对 + 配对分析 ----------
    ds = json.loads(TEST_SET.read_text(encoding="utf-8"))
    tasks_by_id = {t["id"]: t for t in ds["tasks"]}
    labels_order = {t["id"]: t.get("labels") for t in ds["tasks"]}
    c_integrity, rescore_mismatch, per_model_records = {}, {}, {}
    for model in MODELS:
        recs = [json.loads(l) for l in (RAW / RUN_C[model] / "requests.jsonl")
                .read_text(encoding="utf-8").splitlines() if l.strip()]
        ids = [r["task_id"] for r in recs]
        uniq = set(ids)
        c_integrity[model] = {
            "records": len(recs), "unique_task_ids": len(uniq),
            "expected": 50,
            "missing": sorted(set(tasks_by_id) - uniq),
            "duplicates": sorted({i for i in ids if ids.count(i) > 1})}
        if len(uniq) != 50:
            problems.append(f"C[{model}]: 唯一 task_id {len(uniq)}/50")
        mismatch = 0
        for r in recs:
            sc = score_item(tasks_by_id[r["task_id"]], r.get("raw_output") or "")
            if (bool(sc["format_correct"]), bool(sc["content_correct"]),
                    bool(sc["correct"])) != (
                    r["score"]["format_correct"],
                    r["score"]["content_correct"], r["score"]["correct"]):
                mismatch += 1
        rescore_mismatch[model] = mismatch
        per_model_records[model] = {r["task_id"]: r for r in recs}
    summary["C_integrity_and_rescore"] = {
        "integrity": c_integrity, "rescore_mismatch_count": rescore_mismatch,
        "rescore_note": "用冻结评分器 score_answer.py 从原始输出重打分"}
    if any(rescore_mismatch.values()):
        problems.append(f"C: 重打分不一致 {rescore_mismatch}")

    # 配对 McNemar（联合正确），事后探索性
    pairs = {}
    for a, b in [("Q4_K_M", "Q5_K_M"), ("Q4_K_M", "Q8_0"),
                 ("Q5_K_M", "Q8_0")]:
        both_right = both_wrong = a_only = b_only = 0
        disagree = []
        for tid in sorted(set(per_model_records[a]) & set(per_model_records[b])):
            ja = per_model_records[a][tid]["score"]["correct"]
            jb = per_model_records[b][tid]["score"]["correct"]
            if ja and jb:
                both_right += 1
            elif not ja and not jb:
                both_wrong += 1
            elif ja:
                a_only += 1
                disagree.append({"task_id": tid, "winner": a})
            else:
                b_only += 1
                disagree.append({"task_id": tid, "winner": b})
        m = mcnemar_exact(a_only, b_only)
        pairs[f"{a}_vs_{b}"] = {
            "table_2x2": {"both_right": both_right, "both_wrong": both_wrong,
                          f"{a}_only": a_only, f"{b}_only": b_only},
            "mcnemar_exact_two_sided": m,
            "disagreement_items": disagree}
    summary["C_paired_quality"] = {
        "exploratory_note": ("事后探索性分析；3 组比较，Bonferroni 口径 "
                            "α=0.05/3≈0.0167；Wilson 区间仅作描述，"
                            "区间重叠不能证明等价；未检出差异≠质量相同/量化无损；"
                            "本测试集为小型自建任务集，不泛化为整体能力"),
        "pairs": pairs}
    with open(OUT / "stage3rev_mcnemar.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["pair", "both_right", "both_wrong", "a_only", "b_only",
                    "p_exact_two_sided"])
        for k, v in pairs.items():
            t = v["table_2x2"]
            w.writerow([k, t["both_right"], t["both_wrong"],
                        list(t.values())[2], list(t.values())[3],
                        v["mcnemar_exact_two_sided"]["p_value"]])

    # 共同输出模式
    patterns = {"classification_first_label": {}, "json_parse_methods": {},
                "unprovided_outputs": {}}
    for model in MODELS:
        first_label = total_cls = 0
        parse_m = {}
        unprov = 0
        for tid, r in per_model_records[model].items():
            out = (r.get("raw_output") or "").strip()
            if tasks_by_id[tid]["task_type"] == "classification":
                total_cls += 1
                labs = labels_order.get(tid) or []
                if labs and out == labs[0]:
                    first_label += 1
            if tasks_by_id[tid]["task_type"] == "json_extraction":
                pm = (r["score"].get("detail") or {}).get("parse_method")
                parse_m[pm] = parse_m.get(pm, 0) + 1
            if out == "未提供":
                unprov += 1
        patterns["classification_first_label"][model] = {
            "first_label_selected": first_label, "total": total_cls}
        patterns["json_parse_methods"][model] = parse_m
        patterns["unprovided_outputs"][model] = unprov
    summary["common_output_patterns"] = patterns

    # ---------- 修订图 1: B 固定任务组合延迟（每轮五题平均） ----------
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    x = range(len(MODELS))
    means = [per_round_avg[m]["mean_s"] for m in MODELS]
    stds = [per_round_avg[m]["sample_std_s"] for m in MODELS]
    bars = ax.bar(MODELS, means, yerr=stds, capsize=5,
                  color=["#8ab4d8", "#a8c8a0", "#d8b48a"], width=0.5)
    ax.bar_label(bars, fmt="%.3f")
    ax.set_ylabel("秒")
    ax.set_title("矩阵B 修订：固定任务组合端到端延迟\n"
                 "每模型 25 条请求/5 轮；柱=每轮五题平均后再取五轮均值，"
                 "误差条=五轮样本标准差（非任务混合分布）")
    fig.tight_layout()
    fig.savefig(PLOTS / "stage3rev_latency.png", dpi=150)

    # ---------- 修订图 2: B 独立进程峰值内存散点+均值 ----------
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    for i, model in enumerate(MODELS):
        peaks = mem_b[model]["peaks_mib"]
        ax.scatter([i] * len(peaks), peaks, alpha=0.75,
                   color=["#4c72b0", "#55a868", "#c44e52"][i],
                   label=f"{model} (n={len(peaks)})")
        if peaks:
            ax.hlines(mem_b[model]["mean_mib"], i - 0.2, i + 0.2,
                      color="black", linewidth=2)
    ax.set_xticks(range(len(MODELS)))
    ax.set_xticklabels(MODELS)
    ax.set_ylabel("峰值工作集 MiB")
    ax.set_title("矩阵B 修订：独立服务进程生命周期峰值内存\n"
                 "每模型 5 次独立启动（黑线=均值）；C 单次运行内存仅作补充不混用")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "stage3rev_memory.png", dpi=150)

    summary["problems_found"] = problems
    (OUT / "stage3rev_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("完整性问题:", problems if problems else "无")
    print("重打分不一致:", rescore_mismatch)
    print("McNemar:")
    for k, v in pairs.items():
        print(f"  {k}: {v['table_2x2']} p={v['mcnemar_exact_two_sided']['p_value']}")
    print("分类题首标签选择:", patterns["classification_first_label"])
    print("JSON 解析方式:", patterns["json_parse_methods"])
    print("未提供 输出次数:", patterns["unprovided_outputs"])
    print("B 固定组合延迟:", {m: per_round_avg[m]["mean_s"] for m in MODELS})
    print("B 内存:", {m: mem_b[m]["mean_mib"] for m in MODELS})
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
