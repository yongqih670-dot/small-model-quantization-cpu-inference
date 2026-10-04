# -*- coding: utf-8 -*-
"""Stage 6 共同错误诊断分析（独立脚本，冻结后运行于正式数据）。

预注册指标全部实现于本脚本（configs/stage6_plan.json::scoring_and_metrics）：
- 分支A：选择显示位置0比例(Wilson CI)、位置分布、不可映射率、按题配对的
  语义恒定性/首位置跟随/输出变化、原评分器三档(rot0/rot1)、rot0vsrot1 精确McNemar；
- 分支B：每模型×条件的围栏率/直接可解析率/原评分器三档、completion_tokens、
  截断数、逐题转移(J0→J1/J1→J2/J0→J2 × 格式/内容/联合)、族内Holm校正的
  精确McNemar；
- 跨阶段基线一致性：rot0/J0 与 Stage 3 矩阵C 原始输出逐字符比较；
- 同题变体不作为独立样本；全部结论为事后探索性。

运行绑定：从 results/stage6/formal_run_binding.json 显式读取 run_id
（禁止自动选最新目录，沿用 Stage 3 修订的绑定纪律）。

用法: C:/Anaconda/python.exe scripts/stage6_analyze.py
"""

import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from score_answer import normalize_text, parse_json_output  # noqa: E402

STAGE6 = ROOT / "results" / "stage6"
BINDING = STAGE6 / "formal_run_binding.json"
STAGE3_RUNS = {
    "Q4_K_M": "results/raw/run_20261003_112320_formal_Q4_K_M_7eda40",
    "Q5_K_M": "results/raw/run_20261003_112342_formal_Q5_K_M_8b86de",
    "Q8_0": "results/raw/run_20261003_112411_formal_Q8_0_37181d",
}
MODELS = ["Q4_K_M", "Q5_K_M", "Q8_0"]
CONDITIONS = ["J0", "J1", "J2"]
OUT_SUMMARY = STAGE6 / "stage6_summary.json"
OUT_CSV = ROOT / "results" / "summary"
PLOTS = ROOT / "plots"


def wilson(k, n, z=1.96):
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def mcnemar_exact(b: int, c: int) -> dict:
    n = b + c
    if n == 0:
        return {"b": b, "c": c, "n_discordant": 0, "p_value": 1.0}
    k = min(b, c)
    pmf_le = sum(math.comb(n, i) / 2 ** n for i in range(0, k + 1))
    return {"b": b, "c": c, "n_discordant": n,
            "p_value": round(min(1.0, 2 * pmf_le), 6)}


def holm(pvals: dict) -> dict:
    """族内 Holm 步降校正；输入 {name: p}，返回 {name: p_holm}。"""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    out, running = {}, 0.0
    for i, (name, p) in enumerate(items):
        adj = min(1.0, p * (m - i))
        running = max(running, adj)
        out[name] = round(running, 6)
    return out


def load_run(run_dir: Path) -> list:
    recs = [json.loads(l) for l in
            (run_dir / "requests.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    return recs


def map_to_label(raw_output: str, labels_displayed: list) -> dict:
    """预注册映射规则：strip 精确 -> normalize_text -> 不可映射。"""
    out = raw_output or ""
    s = out.strip()
    if s in labels_displayed:
        return {"mapped_label": s, "mapped_position": labels_displayed.index(s),
                "mapping_level": "exact"}
    n = normalize_text(out)
    for i, lab in enumerate(labels_displayed):
        if n and n == normalize_text(lab):
            return {"mapped_label": lab, "mapped_position": i,
                    "mapping_level": "normalized"}
    return {"mapped_label": None, "mapped_position": None,
            "mapping_level": "unmappable"}


def main() -> int:
    binding = json.loads(BINDING.read_text(encoding="utf-8"))
    run_dirs = {m: STAGE6 / binding["runs"][m] for m in MODELS}
    summary = {"bound_runs": binding["runs"],
               "positioning": "事后探索性诊断；同题变体不作为独立样本；"
                              "不得写成新的正式测试集成绩",
               "integrity": {}, "branch_A": {}, "branch_B": {},
               "baseline_consistency": {}, "cross_model": {}}

    # ---------- 完整性 ----------
    all_records = {}
    for m in MODELS:
        res = json.loads((run_dirs[m] / "result.json").read_text(encoding="utf-8"))
        recs = load_run(run_dirs[m])
        all_records[m] = recs
        ok = (res["requests_sent"] == 65 and len(recs) == 65
              and res["counts"]["ok"] == 65 and res["scoring_errors"] == 0
              and res["quality_evaluation_valid"] and res["cleanup_success"]
              and res["cache_reuse_check"]["passed"]
              and res["config_id"] == "stage6_diag_t4_c1024_v1")
        summary["integrity"][m] = {
            "run_id": res["run_id"], "requests_sent": res["requests_sent"],
            "counts": res["counts"], "scoring_errors": res["scoring_errors"],
            "quality_evaluation_valid": res["quality_evaluation_valid"],
            "truncated": res["counts"]["truncated"],
            "max_prompt_tokens_seen": res["max_prompt_tokens_seen"],
            "backend": res["backend_version"]["stderr"].strip().splitlines()[0]
            if res["backend_version"]["stderr"] else None,
            "model_sha256": res["model"]["sha256"],
            "environment_start": res["environment_at_start"],
            "environment_end": res["environment_at_end"],
            "checks_pass": bool(ok)}
        if not ok:
            raise RuntimeError(f"{m} 运行完整性检查未通过，拒绝分析")

    # ---------- 分支A ----------
    cls_rows = []
    for m in MODELS:
        by_vid = {r["variant_id"]: r for r in all_records[m] if r["branch"] == "A"}
        for r in all_records[m]:
            if r["branch"] != "A":
                continue
            mp = map_to_label(r["raw_output"], r["labels_displayed"])
            row = {"model": m, "parent_task_id": r["parent_task_id"],
                   "variant_id": r["variant_id"], "rotation": r["rotation"],
                   "labels_displayed": r["labels_displayed"],
                   "gold": r["gold"],
                   "gold_position": r["gold_position_in_display"],
                   "raw_output": r["raw_output"], **mp,
                   "chose_first": int(mp["mapped_position"] == 0),
                   "format_correct": int(bool(r["score"]["format_correct"])),
                   "content_correct": int(bool(r["score"]["content_correct"])),
                   "joint_correct": int(bool(r["score"]["correct"])),
                   "finish_reason": r["finish_reason"]}
            cls_rows.append(row)
    # 按模型聚合
    for m in MODELS:
        rows = [r for r in cls_rows if r["model"] == m]
        n = len(rows)
        first = sum(r["chose_first"] for r in rows)
        pos_dist = {0: sum(1 for r in rows if r["mapped_position"] == 0),
                    1: sum(1 for r in rows if r["mapped_position"] == 1),
                    "unmappable": sum(1 for r in rows
                                      if r["mapped_position"] is None)}
        # 按题配对（每题 rot0/rot1）
        per_task = {}
        for r in rows:
            per_task.setdefault(r["parent_task_id"], {})[r["rotation"]] = r
        stability = {"semantic_constant": 0, "follows_first_both": 0,
                     "output_changed": 0, "n_tasks": len(per_task)}
        for tid, rots in per_task.items():
            r0, r1 = rots[0], rots[1]
            if r0["mapped_label"] is not None and \
                    r0["mapped_label"] == r1["mapped_label"]:
                stability["semantic_constant"] += 1
            if r0["chose_first"] and r1["chose_first"]:
                stability["follows_first_both"] += 1
            if r0["raw_output"] != r1["raw_output"]:
                stability["output_changed"] += 1
        # rot0 vs rot1 联合正确 McNemar（按题配对）
        b = sum(1 for rots in per_task.values()
                if rots[0]["joint_correct"] and not rots[1]["joint_correct"])
        c = sum(1 for rots in per_task.values()
                if not rots[0]["joint_correct"] and rots[1]["joint_correct"])
        summary["branch_A"][m] = {
            "n_requests": n,
            "chose_first": first,
            "chose_first_rate": round(first / n, 4),
            "chose_first_wilson95": wilson(first, n),
            "position_distribution": pos_dist,
            "unmappable_rate": round(pos_dist["unmappable"] / n, 4),
            "per_task_paired": stability,
            "joint_correct_by_rotation": {
                "rot0": sum(r["joint_correct"] for r in rows if r["rotation"] == 0),
                "rot1": sum(r["joint_correct"] for r in rows if r["rotation"] == 1)},
            "format_correct_by_rotation": {
                "rot0": sum(r["format_correct"] for r in rows if r["rotation"] == 0),
                "rot1": sum(r["format_correct"] for r in rows if r["rotation"] == 1)},
            "mcnemar_rot0_vs_rot1_joint": mcnemar_exact(b, c)}

    # ---------- 分支B ----------
    jsn_rows = []
    for m in MODELS:
        for r in all_records[m]:
            if r["branch"] != "B":
                continue
            raw = r["raw_output"] or ""
            pj = parse_json_output(raw)
            jsn_rows.append({
                "model": m, "parent_task_id": r["parent_task_id"],
                "variant_id": r["variant_id"], "condition": r["condition"],
                "raw_output": r["raw_output"],
                "fence_present": int("```" in raw),
                "parse_method": pj["parse_method"],
                "strict_direct_object": int(pj["parse_method"] == "direct"
                                            and isinstance(pj["obj"], dict)),
                "format_correct": int(bool(r["score"]["format_correct"])),
                "content_correct": int(bool(r["score"]["content_correct"])),
                "joint_correct": int(bool(r["score"]["correct"])),
                "completion_tokens": (r["usage_raw"] or {}).get("completion_tokens"),
                "prompt_tokens": (r["usage_raw"] or {}).get("prompt_tokens"),
                "truncated": int(r.get("truncated") or False),
                "finish_reason": r["finish_reason"]})
    by_mc = {}
    for r in jsn_rows:
        by_mc.setdefault((r["model"], r["condition"]), []).append(r)
    for m in MODELS:
        cond_stats = {}
        for cond in CONDITIONS:
            rows = by_mc[(m, cond)]
            n = len(rows)
            ct = [r["completion_tokens"] for r in rows
                  if isinstance(r["completion_tokens"], int)]
            cond_stats[cond] = {
                "n": n,
                "format_correct": sum(r["format_correct"] for r in rows),
                "content_correct": sum(r["content_correct"] for r in rows),
                "joint_correct": sum(r["joint_correct"] for r in rows),
                "fence_present": sum(r["fence_present"] for r in rows),
                "strict_direct_object": sum(r["strict_direct_object"] for r in rows),
                "truncated": sum(r["truncated"] for r in rows),
                "completion_tokens_mean": round(sum(ct) / len(ct), 1) if ct else None,
                "completion_tokens_min": min(ct) if ct else None,
                "completion_tokens_max": max(ct) if ct else None,
                "prompt_tokens_max": max(r["prompt_tokens"] for r in rows
                                         if isinstance(r["prompt_tokens"], int))}
        # 逐题转移（按题配对 J0→J1、J1→J2、J0→J2）
        transitions = {}
        for metric in ("format_correct", "content_correct", "joint_correct"):
            for ca, cb in (("J0", "J1"), ("J1", "J2"), ("J0", "J2")):
                a = {r["parent_task_id"]: r for r in by_mc[(m, ca)]}
                b_ = {r["parent_task_id"]: r for r in by_mc[(m, cb)]}
                good_bad = sorted(t for t in a if a[t][metric] and not b_[t][metric])
                bad_good = sorted(t for t in a if not a[t][metric] and b_[t][metric])
                transitions[f"{ca}->{cb}:{metric}"] = {
                    "correct_to_wrong": good_bad, "wrong_to_correct": bad_good,
                    "n_correct_to_wrong": len(good_bad),
                    "n_wrong_to_correct": len(bad_good)}
        # McNemar + Holm（每模型×每指标族内 3 比较）
        pvals = {}
        raw_mc = {}
        for metric in ("format_correct", "joint_correct"):
            for ca, cb in (("J0", "J1"), ("J1", "J2"), ("J0", "J2")):
                a = {r["parent_task_id"]: r for r in by_mc[(m, ca)]}
                b_ = {r["parent_task_id"]: r for r in by_mc[(m, cb)]}
                mc = mcnemar_exact(
                    sum(1 for t in a if a[t][metric] and not b_[t][metric]),
                    sum(1 for t in a if not a[t][metric] and b_[t][metric]))
                raw_mc[f"{ca}->{cb}:{metric}"] = mc
                pvals[f"{ca}->{cb}:{metric}"] = mc["p_value"]
        adj = holm({k: v for k, v in pvals.items()})
        for k in raw_mc:
            raw_mc[k]["p_holm_within_family"] = adj[k]
        summary["branch_B"][m] = {"by_condition": cond_stats,
                                  "transitions": transitions,
                                  "mcnemar": raw_mc}

    # ---------- 跨阶段基线一致性（rot0/J0 vs Stage 3 原始输出） ----------
    for m in MODELS:
        s3 = load_run(ROOT / STAGE3_RUNS[m])
        s3_out = {r["task_id"]: r["raw_output"] for r in s3}
        mismatches, compared = [], 0
        for r in all_records[m]:
            comparable = (r["branch"] == "A" and r["rotation"] == 0) or \
                         (r["branch"] == "B" and r["condition"] == "J0")
            if not comparable:
                continue
            compared += 1
            if (r["raw_output"] or "") != (s3_out.get(r["parent_task_id"]) or ""):
                mismatches.append({
                    "variant_id": r["variant_id"],
                    "stage6_output": r["raw_output"],
                    "stage3_output": s3_out.get(r["parent_task_id"])})
        summary["baseline_consistency"][m] = {
            "stage3_run": STAGE3_RUNS[m], "compared": compared,
            "identical": compared - len(mismatches),
            "mismatches": mismatches}

    # ---------- 跨模型共同/不同反应 ----------
    pos_by = {}
    for r in cls_rows:
        pos_by.setdefault(r["parent_task_id"], {}).setdefault(
            r["rotation"], {})[r["model"]] = r["mapped_label"]
    agree_tasks = sum(
        1 for tid in pos_by for rot in pos_by[tid]
        if len({pos_by[tid][rot][m] for m in MODELS}) == 1)
    out_by = {}
    for r in jsn_rows:
        out_by.setdefault(r["parent_task_id"], {}).setdefault(
            r["condition"], {})[r["model"]] = r["raw_output"]
    agree_out = sum(
        1 for tid in out_by for cond in out_by[tid]
        if len({out_by[tid][cond][m] for m in MODELS}) == 1)
    n_pos = len(pos_by) * 2
    n_out = len(out_by) * 3
    summary["cross_model"] = {
        "branch_A_mapped_label_agreement": f"{agree_tasks}/{n_pos} (题×轮换)",
        "branch_B_raw_output_agreement": f"{agree_out}/{n_out} (题×条件)"}

    # ---------- CSV ----------
    OUT_CSV.mkdir(exist_ok=True)
    with open(OUT_CSV / "stage6_cls_items.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(cls_rows[0].keys()))
        w.writeheader()
        w.writerows(cls_rows)
    with open(OUT_CSV / "stage6_json_items.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(jsn_rows[0].keys()))
        w.writeheader()
        w.writerows(jsn_rows)
    trans_rows = []
    for m in MODELS:
        for key, tv in summary["branch_B"][m]["transitions"].items():
            cond_pair, metric = key.split(":")
            trans_rows.append({"model": m, "condition_pair": cond_pair,
                               "metric": metric,
                               **{k: "|".join(v) if isinstance(v, list) else v
                                  for k, v in tv.items()}})
    with open(OUT_CSV / "stage6_transitions.csv", "w", encoding="utf-8",
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trans_rows[0].keys()))
        w.writeheader()
        w.writerows(trans_rows)

    (OUT_SUMMARY).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")

    # ---------- 图 ----------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False
    test_tasks_all = json.loads(
        (ROOT / "datasets" / "test" / "test_tasks.json")
        .read_text(encoding="utf-8"))["tasks"]

    # 图1：分支A
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6))
    ax = axes[0]
    for i, m in enumerate(MODELS):
        s = summary["branch_A"][m]
        lo, hi = s["chose_first_wilson95"]
        rate = s["chose_first_rate"]
        ax.bar(i, rate, 0.55, color=f"C{i}", alpha=0.85)
        ax.errorbar(i, (lo + hi) / 2, yerr=(hi - lo) / 2, fmt="none",
                    ecolor="k", capsize=4)
        ax.text(i, max(rate, hi) + 0.05, f"{s['chose_first']}/{s['n_requests']}",
                ha="center", fontsize=10)
    ax.set_xticks(range(3), MODELS)
    ax.set_ylim(0, 1.18)
    ax.axhline(0.5, ls="--", c="gray", lw=1)
    ax.set_ylabel("选择显示位置0标签的比例")
    ax.set_title("分支A：首位置选择比例（Wilson 95% CI）")
    # 金标位置取自数据集原序（不随轮换变化），独立于运行记录
    gold_pos_ds = {}
    for t in test_tasks_all:
        if t["task_type"] == "classification":
            gold_pos_ds[t["id"]] = t["labels"].index(t["gold"])
    for pi, rot in ((1, 0), (2, 1)):
        ax = axes[pi]
        tasks = sorted({r["parent_task_id"] for r in cls_rows})
        grid = [[next(r["mapped_position"] for r in cls_rows
                      if r["model"] == m and r["parent_task_id"] == t
                      and r["rotation"] == rot)
                 for m in MODELS] for t in tasks]
        golds = [gold_pos_ds[t] for t in tasks]
        mat = [[(2 if v is None else v) for v in row] for row in grid]
        im = ax.imshow(mat, cmap="coolwarm", vmin=-0.4, vmax=2.4, aspect="auto")
        ax.set_xticks(range(3), MODELS, fontsize=9)
        ax.set_yticks(range(len(tasks)),
                      [f"{t} (金标位{g})" for t, g in zip(tasks, golds)], fontsize=8)
        for yi in range(len(tasks)):
            for xi in range(3):
                v = grid[yi][xi]
                ax.text(xi, yi, "—" if v is None else str(v), ha="center",
                        va="center", fontsize=10,
                        color="white" if mat[yi][xi] in (0, 2) else "black")
        ax.set_title(f"分支A：rot{rot} 选择位置（0=首, 1=次, —=不可映射）")
    fig.tight_layout()
    fig.savefig(PLOTS / "stage6_cls_position.png", dpi=150)
    plt.close(fig)

    # 图2：分支B
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.4))
    x = range(3)
    width = 0.26
    ax = axes[0]
    for ci, cond in enumerate(CONDITIONS):
        vals = [summary["branch_B"][m]["by_condition"][cond]["format_correct"]
                / summary["branch_B"][m]["by_condition"][cond]["n"] * 15
                for m in MODELS]
        ax.bar([i + (ci - 1) * width for i in x], vals, width, label=cond)
    ax.set_xticks(x, MODELS)
    ax.set_ylim(0, 15.6)
    ax.set_ylabel("格式正确数 /15")
    ax.set_title("分支B：格式正确（原评分器）")
    ax.legend()
    ax = axes[1]
    for ci, cond in enumerate(CONDITIONS):
        vals = [summary["branch_B"][m]["by_condition"][cond]["joint_correct"]
                for m in MODELS]
        ax.bar([i + (ci - 1) * width for i in x], vals, width, label=cond)
    ax.set_xticks(x, MODELS)
    ax.set_ylim(0, 15.6)
    ax.set_ylabel("联合正确数 /15")
    ax.set_title("分支B：联合正确（原评分器）")
    ax.legend()
    ax = axes[2]
    for ci, cond in enumerate(CONDITIONS):
        fr = [summary["branch_B"][m]["by_condition"][cond]["fence_present"]
              for m in MODELS]
        sd = [summary["branch_B"][m]["by_condition"][cond]["strict_direct_object"]
              for m in MODELS]
        ax.plot(x, fr, "o--", color=f"C{ci}", label=f"{cond} 围栏")
        ax.plot(x, sd, "s:", color=f"C{ci}", label=f"{cond} 直接可解析")
    ax.set_xticks(x, MODELS)
    ax.set_ylim(-0.5, 15.5)
    ax.set_ylabel("计数 /15")
    ax.set_title("分支B：围栏 vs 直接可解析（诊断指标）")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(PLOTS / "stage6_json_conditions.png", dpi=150)
    plt.close(fig)

    print(json.dumps({"integrity": {m: summary["integrity"][m]["checks_pass"]
                                    for m in MODELS},
                      "branch_A_chose_first": {m: summary["branch_A"][m]["chose_first"]
                                               for m in MODELS},
                      "branch_B_joint": {m: {c: summary["branch_B"][m]["by_condition"][c]["joint_correct"]
                                             for c in CONDITIONS} for m in MODELS},
                      "baseline": {m: summary["baseline_consistency"][m]["identical"]
                                   for m in MODELS},
                      "outputs": [str(OUT_SUMMARY), "stage6_cls_items.csv",
                                  "stage6_json_items.csv", "stage6_transitions.csv",
                                  "stage6_cls_position.png",
                                  "stage6_json_conditions.png"]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
