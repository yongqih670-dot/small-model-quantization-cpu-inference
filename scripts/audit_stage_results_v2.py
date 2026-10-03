# -*- coding: utf-8 -*-
"""Stage 5 收尾审计 v2：在 v1 基础上补强（输出独立目录，保留 v1 旧审计）。

新增/强化：
1. **完整组合集合比对**（不只数总数）：
   S3_A = 3 模型×3 负载×5 轮；S3_B = 3 模型×5 题×5 轮；
   S3_C = 每模型恰等于数据集 50 个 task_id；S4_A = 6 档×5 轮×3 负载；
   S4_B = 6 档×5 轮×5 题——每个键恰出现一次，缺失/多余逐一列出；
2. 配置非空且等于预期值（config_id、temperature、seed、max_tokens、tier）；
3. 失败、截断、指标有效性（performance_record_valid）、单元三态逐条检查；
4. **评分器/数据集哈希门控**：先比对 frozen_hashes_stage3.json 中的
   score_answer.py 与数据集哈希，一致才允许使用"冻结评分器重打分"表述；
5. **报告数值表**：report_tables.json 按最终报告的表格结构从原始记录
   生成——自动核对范围以该文件为界（报告其余文字数字由人工依据本
   审计结果撰写，不声称"全文逐项自动核对"）。

用法: python scripts/audit_stage_results_v2.py
"""

import hashlib
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
OUT = ROOT / "results" / "summary" / "audit_stage5_v2"
MODELS = ["Q4_K_M", "Q5_K_M", "Q8_0"]
TIERS = [1, 2, 4, 8, 16, 32]
WORKLOADS = ["pp256", "tg64", "pg256+64"]
ROUNDS = [1, 2, 3, 4, 5]
EXPECTED_CONFIG = {
    "S3": "formal_stage3_t4_c1024_v1",
    "S4": "stage4_thread_tiers_v1",
}
S3_FROZEN = ROOT / "datasets" / "frozen_hashes_stage3.json"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mean(xs):
    return sum(xs) / len(xs)


def sstd(xs):
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def r(x, n=4):
    return None if x is None else round(x, n)


def full_grid(tiers_or_models, rounds, keys_per_unit):
    """构造预注册完整组合键集合（供反例测试复用）。"""
    expected = Counter()
    for a in tiers_or_models:
        for rd in rounds:
            for k in keys_per_unit:
                expected[(a, rd, k)] += 1
    return expected


def compare_counter(observed: Counter, expected: Counter, label: str,
                    findings: list) -> dict:
    """组合集合逐键比对；返回缺失/多余明细。"""
    missing = sorted(expected - observed)
    extra = sorted(observed - expected)
    dup = sorted(k for k, c in observed.items() if c > 1)
    if missing:
        findings.append(f"{label}: 缺失 {len(missing)} 键, 例 {missing[:3]}")
    if extra:
        findings.append(f"{label}: 多余 {len(extra)} 键, 例 {extra[:3]}")
    if dup:
        findings.append(f"{label}: 重复 {len(dup)} 键, 例 {dup[:3]}")
    return {"label": label, "expected_keys": sum(expected.values()),
            "observed_keys": sum(observed.values()),
            "missing": missing, "extra": extra, "duplicates": dup,
            "complete": not (missing or extra or dup)}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    audit = {"bound_run_ids": RUNS, "checks": [], "findings": [],
             "scope_note": ("自动核对范围 = 本文件 checks 与 report_tables；"
                            "最终报告其余文字数字由人工依据本审计撰写，"
                            "不声称全文逐项自动核对")}

    def check(name, ok, detail=""):
        audit["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            audit["findings"].append(f"{name}: {detail}")

    # ---------- 0. 评分器/数据集哈希门控 ----------
    try:
        frozen3 = json.loads(S3_FROZEN.read_text(encoding="utf-8"))
        gated = {}
        for rel in ("scripts/score_answer.py", "datasets/dev/dev_tasks.json",
                    "datasets/test/test_tasks.json",
                    "datasets/prompt_template.json"):
            expected = frozen3.get("files", {}).get(rel)
            actual = sha256(ROOT / rel)
            gated[rel] = (expected == actual)
        hash_ok = all(gated.values())
        check("评分器/数据集与 Stage 3 冻结清单哈希一致", hash_ok,
              str({k: v for k, v in gated.items() if not v}))
        audit["rescore_claim_gate"] = {
            "verdict": ("冻结评分器重打分（哈希已核验）" if hash_ok
                        else "重打分仍执行，但评分器/数据集哈希与冻结清单"
                             "不一致，不得称'冻结评分器'"),
            "files": gated}
    except (OSError, json.JSONDecodeError) as e:
        audit["rescore_claim_gate"] = {"verdict": f"冻结清单不可读: {e}"}
        check("评分器/数据集哈希门控", False, repr(e))
        hash_ok = False
    rescore_label = audit["rescore_claim_gate"]["verdict"]

    # ---------- S3_A：完整组合 45 键 ----------
    res_a = json.loads((RAW / RUNS["S3_A"] / "result.json").read_text(
        encoding="utf-8"))
    obs = Counter()
    for inv in res_a["invocations"]:
        if not inv["ok"]:
            continue
        for t in inv["bench_json"]:
            wl = ("pg256+64" if t.get("n_prompt") and t.get("n_gen")
                  else "pp256" if t.get("n_prompt") else "tg64")
            obs[(inv["model"], inv["round"], wl)] += 1
    exp = full_grid(MODELS, ROUNDS, WORKLOADS)
    audit["S3_A_combos"] = compare_counter(obs, exp, "S3_A(模型,轮,负载)",
                                           audit["findings"])
    check("S3_A 完整组合 45 键", audit["S3_A_combos"]["complete"])
    s3a_vals = {}
    for inv in res_a["invocations"]:
        if not inv["ok"]:
            continue
        for t in inv["bench_json"]:
            wl = ("pg256+64" if t.get("n_prompt") and t.get("n_gen")
                  else "pp256" if t.get("n_prompt") else "tg64")
            s3a_vals.setdefault((inv["model"], wl), []).append(t["samples_ts"][0])
    s3a_stats = {f"{m}|{wl}": {"n": len(v), "mean": r(mean(v), 3),
                               "std": r(sstd(v), 3)}
                 for (m, wl), v in sorted(s3a_vals.items())}

    # ---------- S3_B：完整组合 75 键 + 配置 + 有效性 ----------
    brecs = [json.loads(l) for l in (RAW / RUNS["S3_B"] / "requests_perf.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    plan_tasks = ["dev-001", "dev-003", "dev-004", "dev-007", "dev-009"]
    obs = Counter((x["model"], x["round"], x["task_id"]) for x in brecs)
    exp = full_grid(MODELS, ROUNDS, plan_tasks)
    audit["S3_B_combos"] = compare_counter(obs, exp, "S3_B(模型,轮,题)",
                                           audit["findings"])
    check("S3_B 完整组合 75 键", audit["S3_B_combos"]["complete"])
    check("S3_B config 非空且等于预期",
          all(x.get("config_id") == EXPECTED_CONFIG["S3"] for x in brecs))
    check("S3_B 无 HTTP 失败", all(x.get("http_status") == 200 for x in brecs))
    check("S3_B 无截断", all(x.get("finish_reason") != "length" for x in brecs))
    check("S3_B 指标有效性全真",
          all((x.get("performance") or {}).get("performance_record_valid")
              for x in brecs))
    s3b_combo = {}
    for m in MODELS:
        by_round = {}
        for x in brecs:
            if x["model"] == m:
                by_round.setdefault(x["round"], []).append(
                    x["end_to_end_client_s"])
        vals = [mean(v) for _, v in sorted(by_round.items())]
        s3b_combo[m] = {"rounds": len(vals), "mean_s": r(mean(vals)),
                        "sample_std_s": r(sstd(vals))}
    s3b_mem = {}
    for u in (RAW / RUNS["S3_B"]).glob("startup_*.json"):
        d = json.loads(u.read_text(encoding="utf-8"))
        pw = (d.get("memory") or {}).get("peak_wset_mib")
        if d.get("ok") and pw:
            s3b_mem.setdefault(d["model"], []).append(pw)
    s3b_mem = {m: {"n": len(v), "mean_mib": r(mean(v), 2),
                   "sample_std_mib": r(sstd(v), 2)} for m, v in s3b_mem.items()}

    # ---------- S3_C：每模型恰 50 题集合 + 配置 + 重打分（哈希门控） ----------
    ds = json.loads((ROOT / "datasets/test/test_tasks.json")
                    .read_text(encoding="utf-8"))
    tasks_by_id = {t["id"]: t for t in ds["tasks"]}
    qual, resc = {}, {}
    for m in MODELS:
        recs = [json.loads(l) for l in
                (RAW / RUNS[f"S3_C_{m.split('_')[0]}"] / "requests.jsonl")
                .read_text(encoding="utf-8").splitlines() if l.strip()]
        ids = Counter(x["task_id"] for x in recs)
        expected_ids = Counter(tasks_by_id.keys())
        audit[f"S3_C_combos_{m}"] = compare_counter(
            ids, expected_ids, f"S3_C[{m}] task_id 集合", audit["findings"])
        check(f"S3_C[{m}] 恰为数据集 50 题（无缺失/多余/重复）",
              audit[f"S3_C_combos_{m}"]["complete"])
        check(f"S3_C[{m}] config 非空且等于预期",
              all(x.get("config_id") == EXPECTED_CONFIG["S3"] for x in recs))
        check(f"S3_C[{m}] 生成参数等于预期(temp/seed/max_tokens)",
              all(x["generation_params"]["temperature"] == 0.0
                  and x["generation_params"]["seed"] == 42
                  and x["generation_params"]["max_tokens"] == 96 for x in recs))
        check(f"S3_C[{m}] 无截断无失败",
              all(x.get("finish_reason") != "length"
                  and x.get("http_status") == 200 for x in recs))
        mismatch = 0
        for x in recs:
            sc = score_item(tasks_by_id[x["task_id"]], x.get("raw_output") or "")
            if (bool(sc["format_correct"]), bool(sc["content_correct"]),
                    bool(sc["correct"])) != (x["score"]["format_correct"],
                                             x["score"]["content_correct"],
                                             x["score"]["correct"]):
                mismatch += 1
        resc[m] = mismatch
        qual[m] = {"format": sum(1 for x in recs if x["score"]["format_correct"]),
                   "content": sum(1 for x in recs
                                  if x["score"]["content_correct"]),
                   "joint": sum(1 for x in recs if x["score"]["correct"]),
                   "n": len(recs)}
        p = qual[m]["joint"] / 50
        d_ = 1 + 1.96 ** 2 / 50
        c_ = (p + 1.96 ** 2 / 100) / d_
        h_ = 1.96 * math.sqrt(p * (1 - p) / 50 + 1.96 ** 2 / (4 * 50 * 50)) / d_
        qual[m]["joint_wilson95"] = (r(c_ - h_), r(c_ + h_))
    check("重打分与记录一致（三类口径）",
          all(v == 0 for v in resc.values()), str(resc))
    audit["S3_C_rescore"] = {"label": rescore_label, "mismatch": resc}

    def joint_map(m):
        return {x["task_id"]: bool(x["score"]["correct"]) for x in
                [json.loads(l) for l in
                 (RAW / RUNS[f"S3_C_{m.split('_')[0]}"] / "requests.jsonl")
                 .read_text(encoding="utf-8").splitlines() if l.strip()]}

    def mcnemar(b, c):
        n = b + c
        if n == 0:
            return 1.0
        k = min(b, c)
        return min(1.0, 2 * sum(math.comb(n, i) / 2 ** n for i in range(k + 1)))

    mcn = {}
    for a, b in [("Q4_K_M", "Q5_K_M"), ("Q4_K_M", "Q8_0"), ("Q5_K_M", "Q8_0")]:
        ja, jb = joint_map(a), joint_map(b)
        ao = sum(1 for t in ja if ja[t] and not jb[t])
        bo = sum(1 for t in ja if jb[t] and not ja[t])
        mcn[f"{a}_vs_{b}"] = {"a_only": ao, "b_only": bo,
                              "discordant": ao + bo,
                              "p": r(mcnemar(ao, bo), 6)}
    audit["S3_C_mcnemar"] = mcn

    # ---------- S4_A：完整组合 90 键 + 单元三态 ----------
    units = [json.loads(m.read_text(encoding="utf-8"))
             for m in sorted((RAW / RUNS["S4_A"]).glob("unit_*.json"))
             if "_partial" not in m.name]
    obs = Counter()
    for u in units:
        for t in u.get("tests", []):
            if t.get("valid"):
                obs[(u["tier"], u["round"], t["workload"])] += 1
    exp = full_grid(TIERS, ROUNDS, WORKLOADS)
    audit["S4_A_combos"] = compare_counter(obs, exp, "S4_A(档,轮,负载)",
                                           audit["findings"])
    check("S4_A 完整组合 90 键", audit["S4_A_combos"]["complete"])
    res4_a = json.loads((RAW / RUNS["S4_A"] / "result.json").read_text(
        encoding="utf-8"))
    check("S4_A config 非空且等于预期",
          res4_a.get("config_id") == EXPECTED_CONFIG["S4"],
          str(res4_a.get("config_id")))
    check("S4_A argv 线程参数与档位一致（-t/-fa/-ngl）",
          all(f"-t {u['tier']}" in " ".join(u.get("argv") or [])
              and "-fa off" in " ".join(u.get("argv") or [])
              and "-ngl 0" in " ".join(u.get("argv") or [])
              for u in units))
    check("S4_A 单元三态全真",
          all(u.get("execution_complete") and u.get("metrics_valid")
              and u.get("cleanup_success") for u in units))
    # 实际测量条目 vs 字段交叉核对
    actual_valid = sum(1 for u in units for t in u.get("tests", [])
                       if t.get("valid"))
    field_sum = sum(u.get("tests_valid_count") or 0 for u in units)
    check("S4_A 实际有效测试条目 == 字段合计 == 90",
          actual_valid == field_sum == 90, f"{actual_valid}/{field_sum}")
    s4a_vals = {}
    for u in units:
        for t in u.get("tests", []):
            if t.get("valid"):
                s4a_vals.setdefault((u["tier"], t["workload"]), []).append(
                    t["samples_ts"][0] if t.get("samples_ts") else t["tok_s"])
    s4a_stats = {}
    for (tier, wl), v in sorted(s4a_vals.items()):
        s4a_stats[f"T{tier}|{wl}"] = {"n": len(v), "mean": r(mean(v), 3),
                                      "std": r(sstd(v), 3),
                                      "cv_percent": r(sstd(v) / mean(v) * 100, 2)
                                      if sstd(v) is not None else None}
    audit["S4_A_stats"] = s4a_stats
    speed = {}
    for wl in WORKLOADS:
        base = s4a_stats[f"T1|{wl}"]["mean"]
        speed[wl] = {f"T{t}": r(s4a_stats[f"T{t}|{wl}"]["mean"] / base, 3)
                     for t in TIERS}
    audit["S4_A_speedup_vs_T1"] = speed

    # ---------- S4_B：完整组合 150 键 + 配置 + 三态 + 辅助口径 ----------
    hreqs = [json.loads(l) for l in (RAW / RUNS["S4_B"] / "requests_threads.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    obs = Counter((x["tier"], x["round"], x["task_id"]) for x in hreqs)
    exp = full_grid(TIERS, ROUNDS, plan_tasks)
    audit["S4_B_combos"] = compare_counter(obs, exp, "S4_B(档,轮,题)",
                                           audit["findings"])
    check("S4_B 完整组合 150 键", audit["S4_B_combos"]["complete"])
    check("S4_B config 非空且等于预期",
          all(x.get("config_id") == EXPECTED_CONFIG["S4"] for x in hreqs))
    check("S4_B run_id 与绑定一致",
          all(x.get("run_id") == RUNS["S4_B"] for x in hreqs))
    check("S4_B 无 HTTP 失败/截断",
          all(x.get("http_status") == 200
              and x.get("finish_reason") != "length" for x in hreqs))
    check("S4_B 指标有效性全真",
          all((x.get("performance") or {}).get("performance_record_valid")
              for x in hreqs))
    hunits = [json.loads(m.read_text(encoding="utf-8"))
              for m in sorted((RAW / RUNS["S4_B"]).glob("unit_*.json"))
              if "_partial" not in m.name]
    check("S4_B 30 单元三态全真",
          len(hunits) == 30 and all(
              u.get("execution_complete") and u.get("metrics_valid")
              and u.get("cleanup_success") for u in hunits))
    s4b_combo, s4b_mem, s4b_start = {}, {}, {}
    for t in TIERS:
        by_round = {}
        for x in hreqs:
            if x["tier"] == t and x.get("http_status") == 200:
                by_round.setdefault(x["round"], []).append(
                    x["end_to_end_client_s"])
        vals = [mean(v) for _, v in sorted(by_round.items())]
        s4b_combo[f"T{t}"] = {"rounds": len(vals), "mean_s": r(mean(vals)),
                              "sample_std_s": r(sstd(vals))}
    for u in hunits:
        pw = (u.get("memory") or {}).get("peak_wset_mib")
        if u.get("ok") and pw:
            s4b_mem.setdefault(u["tier"], []).append(pw)
        st = (u.get("server_startup") or {}).get("server_startup_to_ready_s")
        if st is not None:
            s4b_start.setdefault(u["tier"], []).append(st)
    audit["S4_B_stats"] = {
        "fixed_combo_latency": s4b_combo,
        "memory_per_tier_5processes": {
            f"T{t}": {"n": len(v), "mean_mib": r(mean(v), 2),
                      "sample_std_mib": r(sstd(v), 2)}
            for t, v in sorted(s4b_mem.items())},
        "startup_to_ready_per_tier": {
            f"T{t}": {"n": len(v), "mean_s": r(mean(v), 3)}
            for t, v in sorted(s4b_start.items())}}

    # ---------- 报告数值表（自动核对范围到此为止） ----------
    tables = {
        "s3_quality": {m: {"format": f"{qual[m]['format']}/50",
                           "content": f"{qual[m]['content']}/50",
                           "joint": f"{qual[m]['joint']}/50",
                           "wilson95": qual[m]["joint_wilson95"]}
                       for m in MODELS},
        "s3_mcnemar": mcn,
        "s3_latency_fixed_combo": s3b_combo,
        "s3_memory_5proc": s3b_mem,
        "s3_bench": s3a_stats,
        "s4_bench": s4a_stats,
        "s4_speedup": speed,
        "s4_latency_fixed_combo": s4b_combo,
        "s4_memory_per_tier": audit["S4_B_stats"]["memory_per_tier_5processes"],
        "file_sizes_mib": {"Q4_K_M": 468.64, "Q5_K_M": 498.00, "Q8_0": 644.42},
    }
    audit["report_tables"] = tables

    (OUT / "audit_results_v2.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report_tables.json").write_text(
        json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"检查 {len(audit['checks'])} 项, 发现问题 {len(audit['findings'])} 项")
    for f in audit["findings"]:
        print(" -", f)
    print("报告数值表键:", sorted(tables.keys()))
    return 0 if not audit["findings"] else 1


if __name__ == "__main__":
    sys.exit(main())
