# -*- coding: utf-8 -*-
"""Stage 6 离线分析修订（零推理；替代旧分析中与预注册冲突的部分）。

修订范围（见 docs/STAGE6_ANALYSIS_REVISION.md）：
1. 分支A：撤销把同题两次轮换当独立样本的 Wilson 区间与图中误差条
   （预注册文本内部冲突：统计单位声明为"题"，而 CI 以 n=20 汇总二项计算）；
   保留描述性计数；主要展示按题配对行为；限定 McNemar 口径（比较的是
   rot0/rot1 正确率变化，p=1.0 不能证明位置偏好）。
2. 分支B：Holm 校正按预注册"每模型×每指标×3 个比较"分族
   （旧脚本误把 format+joint 合并为 6 值族）；补齐内容指标 McNemar；
   保存每族成员/原始 p/校正值；围栏等仅描述性，不与正确率检验混述。
3. 执行账目：按 7 个 run 核算任务请求与辅助请求；逐 run 报告
   HTTP失败/超时/截断/评分异常/开发重跑；辅助请求未落盘字段标"未记录"。
4. 结论措辞修正：不再写"剂量响应成立"等（替代对照见汇总
   replaced_conclusions 字段与修订报告）。

只写 results/stage6/rev/；不修改任何历史文件、旧脚本、旧汇总、冻结清单；
不重新冻结任何材料。所有输出 LF 行尾。

用法: C:/Anaconda/python.exe scripts/stage6_analyze_revision.py
"""

import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from score_answer import normalize_text, parse_json_output, score_item  # noqa: E402

STAGE6 = ROOT / "results" / "stage6"
REV = STAGE6 / "rev"
BINDING = STAGE6 / "formal_run_binding.json"
STAGE3_RUNS = {
    "Q4_K_M": "results/raw/run_20261003_112320_formal_Q4_K_M_7eda40",
    "Q5_K_M": "results/raw/run_20261003_112342_formal_Q5_K_M_8b86de",
    "Q8_0": "results/raw/run_20261003_112411_formal_Q8_0_37181d",
}
MODELS = ["Q4_K_M", "Q5_K_M", "Q8_0"]
CONDITIONS = ["J0", "J1", "J2"]
PAIRS = [("J0", "J1"), ("J1", "J2"), ("J0", "J2")]
METRICS = ["format_correct", "content_correct", "joint_correct"]


def mcnemar_exact(b: int, c: int) -> dict:
    """双侧精确 McNemar：p = min(1, 2*P(X<=min(b,c))), X~Bin(b+c, .5)。

    口径限定：比较的是**配对二元结果（正确率）的变化**；
    p 值不用于证明/证伪与正确率无关的行为模式（如位置偏好）。
    """
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


def load_binding_runs() -> dict:
    binding = json.loads(BINDING.read_text(encoding="utf-8"))
    return binding["runs"]


def main() -> int:
    REV.mkdir(parents=True, exist_ok=False)
    bound = load_binding_runs()
    summary = {
        "revision_id": "stage6_analysis_revision_v1",
        "generated_by": "scripts/stage6_analyze_revision.py（离线，零推理）",
        "bound_runs": dict(bound),
        "positioning": ("事后探索性诊断的离线分析修订；不产生新成绩、不改历史、"
                        "不重新冻结；替代关系见 replaced_conclusions"),
        "supersedes_targets": [
            "docs/STAGE6_DIAGNOSIS_REPORT.md §3.1/§3.2（Wilson 区间与误差条）",
            "docs/STAGE6_DIAGNOSIS_REPORT.md §3.3 判读列（语义理解表述）",
            "docs/STAGE6_DIAGNOSIS_REPORT.md §3.4（无实质差异表述）",
            "docs/STAGE6_DIAGNOSIS_REPORT.md §4.2/§4.3（Holm 分族与剂量响应表述）",
            "docs/STAGE6_DIAGNOSIS_REPORT.md §1 执行概况账目（请求数/启动次数）"],
        "replaced_conclusions": [],
        "branch_A": {}, "branch_B": {}, "ledger": {},
        "baseline_consistency": {}, "cross_model": {}}

    # ---------- 载入正式运行 ----------
    all_records = {}
    for m in MODELS:
        run_dir = STAGE6 / bound[m]
        res = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        recs = [json.loads(l) for l in
                (run_dir / "requests.jsonl").read_text(encoding="utf-8").splitlines()
                if l.strip()]
        assert res["requests_sent"] == 65 and len(recs) == 65, m
        assert res["counts"]["ok"] == 65 and res["scoring_errors"] == 0, m
        all_records[m] = recs

    # ================= 分支A =================
    cls_rows = []
    for m in MODELS:
        for r in all_records[m]:
            if r["branch"] != "A":
                continue
            sc = score_item({"scoring": "label", "gold": r["gold"],
                             "labels": r["labels_displayed"]},
                            r["raw_output"] or "")
            mp = map_to_label(r["raw_output"], r["labels_displayed"])
            cls_rows.append({
                "model": m, "parent_task_id": r["parent_task_id"],
                "variant_id": r["variant_id"], "rotation": r["rotation"],
                "labels_displayed": r["labels_displayed"], "gold": r["gold"],
                "gold_position_dataset": None,  # 由数据集回填（下方）
                "raw_output": r["raw_output"], **mp,
                "chose_first": int(mp["mapped_position"] == 0),
                "format_correct": int(bool(sc["format_correct"])),
                "content_correct": int(bool(sc["content_correct"])),
                "joint_correct": int(bool(sc["correct"]))})
    test_tasks = {t["id"]: t for t in json.loads(
        (ROOT / "datasets" / "test" / "test_tasks.json")
        .read_text(encoding="utf-8"))["tasks"]}
    for row in cls_rows:
        row["gold_position_dataset"] = \
            test_tasks[row["parent_task_id"]]["labels"].index(
                test_tasks[row["parent_task_id"]]["gold"])

    for m in MODELS:
        rows = [r for r in cls_rows if r["model"] == m]
        n = len(rows)
        first = sum(r["chose_first"] for r in rows)
        pos_dist = {0: sum(1 for r in rows if r["mapped_position"] == 0),
                    1: sum(1 for r in rows if r["mapped_position"] == 1),
                    "unmappable": sum(1 for r in rows
                                      if r["mapped_position"] is None)}
        per_task = {}
        for r in rows:
            per_task.setdefault(r["parent_task_id"], {})[r["rotation"]] = r
        follows, constants = [], []
        for tid, rots in sorted(per_task.items()):
            r0, r1 = rots[0], rots[1]
            if r0["chose_first"] and r1["chose_first"]:
                follows.append(tid)
            if r0["mapped_label"] is not None and \
                    r0["mapped_label"] == r1["mapped_label"]:
                constants.append({"task": tid, "constant_label": r0["mapped_label"],
                                  "correct_rot0": r0["joint_correct"],
                                  "correct_rot1": r1["joint_correct"]})
        b = sum(1 for rots in per_task.values()
                if rots[0]["joint_correct"] and not rots[1]["joint_correct"])
        c = sum(1 for rots in per_task.values()
                if not rots[0]["joint_correct"] and rots[1]["joint_correct"])
        summary["branch_A"][m] = {
            "n_requests": n,
            "chose_first_descriptive": {"count": first, "n": n,
                                        "note": "仅描述计数；同题两次轮换不独立，"
                                                "不附区间（预注册单位冲突修正）"},
            "position_distribution": pos_dist,
            "per_task_paired": {
                "follows_first_both": {"count": len(follows), "n_tasks": 10,
                                       "tasks": follows},
                "semantic_constant": {"count": len(constants), "n_tasks": 10,
                                      "detail": constants},
                "note": "K=2 下二者互斥且并集=全部10题"},
            "joint_correct_by_rotation": {
                "rot0": sum(r["joint_correct"] for r in rows if r["rotation"] == 0),
                "rot1": sum(r["joint_correct"] for r in rows if r["rotation"] == 1)},
            "format_correct_by_rotation": {
                "rot0": sum(r["format_correct"] for r in rows if r["rotation"] == 0),
                "rot1": sum(r["format_correct"] for r in rows if r["rotation"] == 1)},
            "mcnemar_rot0_vs_rot1_joint": {
                **mcnemar_exact(b, c),
                "interpretation": ("仅比较 rot0/rot1 正确率变化；p=1.0 应表述为"
                                   "未检出两种顺序下正确率的显著差异（本样本 Q8 联合正确 5→6，并非无净变化），不能据此证明或证伪位置偏好；"
                                   "位置偏好的证据是描述性逐题模式")}}

    # 跨模型（具体计数）
    cells = {}
    for r in cls_rows:
        cells.setdefault((r["parent_task_id"], r["rotation"]), {})[r["model"]] = \
            r["mapped_label"]
    disagree = [{"task": k[0], "rotation": k[1], "labels": v}
                for k, v in sorted(cells.items())
                if len(set(v.values())) > 1]
    summary["cross_model"]["branch_A"] = {
        "same_mapped_label_cells": f"{len(cells) - len(disagree)}/{len(cells)}",
        "disagreeing_cells": disagree,
        "deviation_requests_per_model": {
            m: sum(1 for r in cls_rows if r["model"] == m
                   and r["mapped_position"] != 0) for m in MODELS},
        "note": ("test-037/038 在两轮换下三模型始终输出\"正面\""
                 "（各4/4请求）；不以其一答对推断语义理解")}

    # ================= 分支B =================
    jsn_rows = []
    for m in MODELS:
        for r in all_records[m]:
            if r["branch"] != "B":
                continue
            raw = r["raw_output"] or ""
            sc = score_item({"scoring": "json", "gold": r["gold"]}, raw)
            pj = parse_json_output(raw)
            jsn_rows.append({
                "model": m, "parent_task_id": r["parent_task_id"],
                "condition": r["condition"], "raw_output": r["raw_output"],
                "fence_present": int("```" in raw),
                "parse_method": pj["parse_method"],
                "strict_direct_object": int(pj["parse_method"] == "direct"
                                            and isinstance(pj["obj"], dict)),
                "format_correct": int(bool(sc["format_correct"])),
                "content_correct": int(bool(sc["content_correct"])),
                "joint_correct": int(bool(sc["correct"])),
                "completion_tokens": (r["usage_raw"] or {}).get("completion_tokens"),
                "truncated": int(r.get("truncated") or False)})
    by_mc = {}
    for r in jsn_rows:
        by_mc.setdefault((r["model"], r["condition"]), []).append(r)

    for m in MODELS:
        cond_stats = {}
        for cond in CONDITIONS:
            rows = by_mc[(m, cond)]
            ct = [r["completion_tokens"] for r in rows
                  if isinstance(r["completion_tokens"], int)]
            cond_stats[cond] = {
                "n": len(rows),
                "format_correct": sum(r["format_correct"] for r in rows),
                "content_correct": sum(r["content_correct"] for r in rows),
                "joint_correct": sum(r["joint_correct"] for r in rows),
                "fence_present_descriptive":
                    sum(r["fence_present"] for r in rows),
                "strict_direct_object_descriptive":
                    sum(r["strict_direct_object"] for r in rows),
                "truncated": sum(r["truncated"] for r in rows),
                "completion_tokens_mean":
                    round(sum(ct) / len(ct), 1) if ct else None}
        transitions, families = {}, {}
        for metric in METRICS:
            fam_p, fam_detail = {}, {}
            for ca, cb in PAIRS:
                a = {r["parent_task_id"]: r for r in by_mc[(m, ca)]}
                bb = {r["parent_task_id"]: r for r in by_mc[(m, cb)]}
                good_bad = sorted(t for t in a if a[t][metric] and not bb[t][metric])
                bad_good = sorted(t for t in a if not a[t][metric] and bb[t][metric])
                key = f"{ca}->{cb}"
                transitions[f"{key}:{metric}"] = {
                    "correct_to_wrong": good_bad, "wrong_to_correct": bad_good,
                    "n_correct_to_wrong": len(good_bad),
                    "n_wrong_to_correct": len(bad_good)}
                mc = mcnemar_exact(len(good_bad), len(bad_good))
                fam_detail[key] = mc
                fam_p[key] = mc["p_value"]
            fam_holm = holm(fam_p)
            families[metric] = {
                "members": [f"{ca}->{cb}" for ca, cb in PAIRS],
                "family_size": len(PAIRS),
                "detail": {k: {**v, "holm_p": fam_holm[k]}
                           for k, v in fam_detail.items()},
                "holm_p": fam_holm}
        summary["branch_B"][m] = {
            "by_condition": cond_stats, "transitions": transitions,
            "mcnemar_families": families}

    summary["branch_B_global_notes"] = {
        "inference_scope": ("推断检验（精确McNemar+族内Holm）仅作用于每题配对的"
                            "二元正确率：format_correct / content_correct / "
                            "joint_correct；每模型×每指标为独立3比较族"),
        "descriptive_only": ("fence_present、strict_direct_object、"
                             "completion_tokens 仅描述性；围栏减少不得与"
                             "格式正确率检验结果混为一谈"),
        "holm_grouping_correction": ("旧脚本把 format+joint 合并为 6 值族"
                                     "（Q4 格式 J1→J2 Holm=0.75；Q5/Q8=0.375）；"
                                     "预注册口径为每指标 3 比较族，本修订按其执行"),
        "dose_response_verdict": ("不写\"剂量响应成立\"：J1 未降低围栏率"
                                  "（15/15→15/15）；J2 减少围栏（15/15→0–3/15，"
                                  "描述性）；完整渐进改善预测未获支持"),
        "content_change_separation": ("内容变化分别以 J0→J1、J1→J2、J0→J2 三个"
                                      "基线报告，不混用")}

    # ================= 执行账目（7 个 run） =================
    runs_ledger = []
    dev_runs_seen = {}
    for rd in sorted(STAGE6.glob("run_*")):
        if not (rd / "result.json").is_file():
            continue
        res = json.loads((rd / "result.json").read_text(encoding="utf-8"))
        counts = res.get("counts", {})
        aux_warmup = 1 if res.get("warmup_request") else 0
        aux_cache = len((res.get("cache_reuse_check") or {}).get("requests") or [])
        task_req = res.get("requests_sent")
        entry = {
            "run_id": res["run_id"], "mode": res.get("mode"),
            "model_key": res.get("model_key"),
            "task_requests": task_req,
            "auxiliary_warmup_requests": aux_warmup,
            "auxiliary_cache_check_requests": aux_cache,
            "auxiliary_total": aux_warmup + aux_cache,
            "auxiliary_field_recording": {
                "warmup": "仅 ok 标记（result.json.warmup_request）；"
                          "messages/raw_output/usage 等未记录",
                "cache_check": "仅 cache_n/prompt_n（result.json."
                               "cache_reuse_check.requests）；其余字段未记录",
                "policy": "未保存字段标\"未记录\"，不补造"},
            "http_failed": counts.get("request_failed", 0),
            "timeouts": counts.get("timeout", 0),
            "truncated": counts.get("truncated", 0),
            "scoring_errors": res.get("scoring_errors", 0),
            "cache_check_passed": (res.get("cache_reuse_check") or {}).get("passed"),
            "execution_complete": res.get("execution_complete"),
            "note": None}
        if res.get("mode") == "dev":
            key = res.get("model_key")
            dev_runs_seen.setdefault(key, []).append(res["run_id"])
            if res.get("scoring_errors", 0) > 0:
                entry["note"] = ("开发验证缺陷尝试：分类变体缺 labels 评分别名，"
                                 f"{res['scoring_errors']} 条评分异常；"
                                 "原始记录保留，未覆盖")
            else:
                entry["note"] = "开发验证（缺陷修复后）"
        runs_ledger.append(entry)
    for e in runs_ledger:
        if e["mode"] == "dev" and e["scoring_errors"] == 0 and \
                len(dev_runs_seen.get(e["model_key"], [])) > 1:
            e["note"] += "；该模型此前存在缺陷尝试，本 run 为开发重跑"
    task_total = sum(e["task_requests"] for e in runs_ledger)
    aux_total = sum(e["auxiliary_total"] for e in runs_ledger)
    summary["ledger"] = {
        "n_runs": len(runs_ledger),
        "runs": runs_ledger,
        "task_requests_total": task_total,
        "auxiliary_requests_total": aux_total,
        "grand_total": task_total + aux_total,
        "classification_note": ("任务请求=携带变体任务的请求（正式195+开发52）；"
                                "辅助请求=每次服务启动的预热1+缓存检查2；"
                                "7次启动×3=21"),
        "totals_check": {"expected_task": 247, "expected_aux": 21,
                         "expected_total": 268,
                         "match": bool(task_total == 247 and aux_total == 21
                                       and task_total + aux_total == 268)},
        "dev_reruns": {k: len(v) for k, v in dev_runs_seen.items()},
        "scoring_errors_total": sum(e["scoring_errors"] for e in runs_ledger)}

    # ================= 基线一致性（实际输出逐字符） =================
    baseline = {}
    for m in MODELS:
        s3 = [json.loads(l) for l in
              (ROOT / STAGE3_RUNS[m] / "requests.jsonl")
              .read_text(encoding="utf-8").splitlines() if l.strip()]
        s3_out = {r["task_id"]: r for r in s3}
        compared = mism = 0
        detail = []
        for r in all_records[m]:
            comparable = (r["branch"] == "A" and r["rotation"] == 0) or \
                         (r["branch"] == "B" and r["condition"] == "J0")
            if not comparable:
                continue
            compared += 1
            s3r = s3_out.get(r["parent_task_id"])
            same = (s3r is not None and (r["raw_output"] or "") ==
                    (s3r.get("raw_output") or "")
                    and r["finish_reason"] == s3r.get("finish_reason")
                    and (r["usage_raw"] or {}).get("completion_tokens") ==
                    (s3r.get("usage_raw") or {}).get("completion_tokens"))
            if same:
                continue
            mism += 1
            detail.append({"variant_id": r["variant_id"],
                           "stage6": r["raw_output"],
                           "stage3": s3r.get("raw_output") if s3r else None})
        baseline[m] = {"stage3_run": STAGE3_RUNS[m], "compared": compared,
                       "identical_output_reason_tokens": compared - mism,
                       "mismatches": detail}
    summary["baseline_consistency"] = baseline

    # ---------- 追溯限制声明 ----------
    summary["traceability_limitation"] = {
        "run_snapshots": ("每个 run 内仅保存 config_snapshot（result.json），"
                          "无完整计划/变体/冻结清单快照；不补造当时快照"),
        "retrospective_copy": ("执行时冻结材料可由提交 f92012e 导出为"
                               "『事后追溯副本』（results/stage6/rev/"
                               "traceability_f92012e/，标明来源与导出时间；"
                               "不回填原 run，不据此宣称当时已存档）"),
        "timeline": ("冻结#1 → 提交 f92012e → 三个正式 run → 分析 → "
                     "图1标签缺陷修复 → 冻结#2（refreeze_log 记录）→ 修订分析；"
                     "f92012e 的清单即正式运行门禁实际核验的清单")}

    # ---------- 替代对照 ----------
    summary["replaced_conclusions"] = [
        {"old": "首位置选择比例附 Wilson 95% CI [0.70,0.97]/[0.64,0.95]"
                "（旧§3.1、图1误差条）",
         "new": "撤销区间：n=20 把同题两次轮换当独立样本，与预注册"
                "\"以题为统计单位\"冲突；保留描述计数 18/20、18/20、17/20"},
        {"old": "rot0 vs rot1 McNemar p=1.0（旧§3.2）",
         "new": "限定口径：该检验只比较正确率变化；p=1.0 不能证明位置偏好；"
                "位置偏好证据为描述性逐题模式（随位置 8/10、8/10、7/10）"},
        {"old": "\"三个量化版本在该行为上无实质差异\"（旧§3.4）",
         "new": "具体计数：19/20 题×轮换单元三模型输出同一标签；偏离请求 "
                "Q4 2、Q5 2、Q8 3（均为 test-037/038 rot1 输出\"正面\"，"
                "Q8 另有 test-035 rot1 输出\"紧急\"）"},
        {"old": "\"J2 有效…剂量响应\"\"J1→J2 进一步降低\"（旧§4.3）",
         "new": "不写\"剂量响应成立\"：J1 未降低围栏率（15/15→15/15）；"
                "J2 减少围栏（15/15→0–3/15，描述性）；完整渐进改善预测"
                "未获支持"},
        {"old": "\"Q8 最稳（内容 7→7）\"（旧§4.4）",
         "new": "具体计数：J1→J2 内容对→错 Q4 5 题、Q5 3 题、Q8 1 题；"
                "Q4 含 2 例示例字段名改写（test-024/025）；Q4 J2 直接可解析"
                "15/15 但格式仅 4/15（字段名改变产生多余/缺失字段）"},
        {"old": "\"Q8 是唯一一次放弃首位置且方向正确\"（旧§3.3 判读）",
         "new": "表述不准确：未选首位且与金标一致共 4 例（test-037 rot1 三模型 + test-035 rot1 Q8）；test-035 rot1 记为唯一跨模型标签分歧单元；"
                "均不作语义理解证据；test-037/038 描述为两轮换始终输出\"正面\"，"
                "不以其一答对推断语义理解"},
        {"old": "辅助请求 70 / 总数 265 / 6 次启动（旧§1）",
         "new": "按 7 个 run 核算：任务请求 247 + 辅助 21 = 268；"
                "辅助=7×(预热1+缓存检查2)；辅助请求未落盘字段标\"未记录\""},
        {"old": "Holm 校正 6 值合并族（格式 J1→J2：Q4 0.75、Q5/Q8 0.375）",
         "new": "按预注册\"每模型×每指标×3 比较\"分族：格式 J1→J2 与 J0→J2 "
                "Holm 后 Q4=0.375、Q5/Q8=0.1875；补齐内容与联合全部 27 个"
                "检验（均不显著，最小校正值 0.1875）"}]

    # ---------- 写出 ----------
    (REV / "stage6rev_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")

    def write_csv(name, rows):
        with open(REV / name, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    write_csv("stage6rev_cls_items.csv", cls_rows)
    write_csv("stage6rev_json_items.csv", jsn_rows)
    trans_rows = []
    for m in MODELS:
        for key, tv in summary["branch_B"][m]["transitions"].items():
            pair, metric = key.split(":")
            trans_rows.append({"model": m, "condition_pair": pair,
                               "metric": metric,
                               **{k: "|".join(v) if isinstance(v, list) else v
                                  for k, v in tv.items()}})
    write_csv("stage6rev_transitions.csv", trans_rows)
    fam_rows = []
    for m in MODELS:
        for metric, fam in summary["branch_B"][m]["mcnemar_families"].items():
            for cmp_, d in fam["detail"].items():
                fam_rows.append({"model": m, "metric": metric,
                                 "family_size": fam["family_size"],
                                 "comparison": cmp_, **d})
    write_csv("stage6rev_mcnemar_families.csv", fam_rows)

    # ---------- 图（无误差条） ----------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    const_map = {}
    for m in MODELS:
        for d in summary["branch_A"][m]["per_task_paired"]["semantic_constant"]["detail"]:
            const_map[(m, d["task"])] = d["constant_label"]

    fig, axes = plt.subplots(1, 3, figsize=(15.5, 4.8))
    ax = axes[0]
    for i, m in enumerate(MODELS):
        s = summary["branch_A"][m]
        cnt = s["chose_first_descriptive"]["count"]
        ax.bar(i, cnt / s["n_requests"], 0.55, color=f"C{i}", alpha=0.85)
        ax.text(i, cnt / s["n_requests"] + 0.03, f"{cnt}/{s['n_requests']}",
                ha="center", fontsize=11)
    ax.set_xticks(range(3),
                  [f"{m}\n(随位置 {summary['branch_A'][m]['per_task_paired']['follows_first_both']['count']}/10)"
                   for m in MODELS], fontsize=9)
    ax.set_ylim(0, 1.12)
    ax.axhline(0.5, ls="--", c="gray", lw=1)
    ax.set_ylabel("选择显示位置0标签的请求比例（仅描述）")
    ax.set_title("分支A：首位置选择计数（无区间：同题两次轮换不独立）")
    for pi, rot in ((1, 0), (2, 1)):
        ax = axes[pi]
        tasks = sorted({r["parent_task_id"] for r in cls_rows})
        grid = [[next(r["mapped_position"] for r in cls_rows
                      if r["model"] == m and r["parent_task_id"] == t
                      and r["rotation"] == rot)
                 for m in MODELS] for t in tasks]
        golds = [test_tasks[t]["labels"].index(test_tasks[t]["gold"])
                 for t in tasks]
        mat = [[2 if v is None else v for v in row] for row in grid]
        ax.imshow(mat, cmap="coolwarm", vmin=-0.4, vmax=2.4, aspect="auto")
        ax.set_xticks(range(3), MODELS, fontsize=9)
        ax.set_yticks(range(len(tasks)),
                      [f"{t} (金标位{g})" for t, g in zip(tasks, golds)],
                      fontsize=8)
        for yi, t in enumerate(tasks):
            for xi, m in enumerate(MODELS):
                v = grid[yi][xi]
                star = "*" if (m, t) in const_map else ""
                ax.text(xi, yi, ("—" if v is None else str(v)) + star,
                        ha="center", va="center", fontsize=10,
                        color="white" if mat[yi][xi] in (0, 2) else "black")
        ax.set_title(f"分支A：rot{rot} 选择位置（*=恒定输出，—=不可映射）")
    fig.tight_layout()
    fig.savefig(REV / "stage6rev_cls_position.png", dpi=150)
    plt.close(fig)

    fig, axes = plt.subplots(1, 4, figsize=(19, 4.4))
    for ai, metric in enumerate(("format_correct", "content_correct",
                                 "joint_correct")):
        ax = axes[ai]
        for ci, cond in enumerate(CONDITIONS):
            vals = [summary["branch_B"][m]["by_condition"][cond][metric]
                    for m in MODELS]
            ax.bar([i + (ci - 1) * 0.26 for i in range(3)], vals, 0.26,
                   label=cond)
            for i, v in enumerate(vals):
                if v:
                    ax.text(i + (ci - 1) * 0.26, v + 0.15, str(v),
                            ha="center", fontsize=8)
        ax.set_xticks(range(3), MODELS, fontsize=9)
        ax.set_ylim(0, 15.8)
        ax.set_ylabel("计数 /15")
        ax.set_title(f"{metric}（推断检验对象，配对）", fontsize=11)
        if ai == 0:
            ax.legend(fontsize=9)
    ax = axes[3]
    for ci, cond in enumerate(CONDITIONS):
        fr = [summary["branch_B"][m]["by_condition"][cond]
              ["fence_present_descriptive"] for m in MODELS]
        sd = [summary["branch_B"][m]["by_condition"][cond]
              ["strict_direct_object_descriptive"] for m in MODELS]
        ax.plot(range(3), fr, "o--", color=f"C{ci}", label=f"{cond} 围栏")
        ax.plot(range(3), sd, "s:", color=f"C{ci}", label=f"{cond} 直接可解析")
    ax.set_xticks(range(3), MODELS, fontsize=9)
    ax.set_ylim(-0.5, 15.5)
    ax.set_ylabel("计数 /15")
    ax.set_title("围栏/直接可解析（仅描述性，不做检验）", fontsize=11)
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(REV / "stage6rev_json_conditions.png", dpi=150)
    plt.close(fig)

    # ---------- 控制台摘要 ----------
    print(json.dumps({
        "branch_A_chose_first": {m: summary["branch_A"][m]
                                 ["chose_first_descriptive"]["count"]
                                 for m in MODELS},
        "branch_A_paired_follows": {m: summary["branch_A"][m]
                                    ["per_task_paired"]["follows_first_both"]["count"]
                                    for m in MODELS},
        "format_holm_J1J2": {m: summary["branch_B"][m]["mcnemar_families"]
                             ["format_correct"]["holm_p"]["J1->J2"]
                             for m in MODELS},
        "format_holm_J0J2": {m: summary["branch_B"][m]["mcnemar_families"]
                             ["format_correct"]["holm_p"]["J0->J2"]
                             for m in MODELS},
        "content_holm_J1J2": {m: summary["branch_B"][m]["mcnemar_families"]
                              ["content_correct"]["holm_p"]["J1->J2"]
                              for m in MODELS},
        "ledger": {k: summary["ledger"][k] for k in
                   ("n_runs", "task_requests_total", "auxiliary_requests_total",
                    "grand_total")},
        "baseline_identical": {m: baseline[m]["identical_output_reason_tokens"]
                               for m in MODELS},
        "outputs": sorted(p.name for p in REV.iterdir())},
        ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
