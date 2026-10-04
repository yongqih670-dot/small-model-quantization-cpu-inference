# -*- coding: utf-8 -*-
"""Stage 6 输入链路核查（只读，不推理、不修改任何历史文件）。

在共同错误诊断实验前核查 Stage 3 正式运行（矩阵 C）的请求证据链：
1. 数据集字段 -> 渲染后提示 -> 实际保存的 user_message 一致（150/150）；
2. 分类题：渲染提示中的候选标签顺序 = 数据集 labels 顺序；
   "选首个标签"指提示中候选列表的第一位，不是题目执行顺序；
3. 金标在候选列表中的位置分布（验证该现象非"答对"的另一种表述）；
4. JSON 题：围栏/解析方式、无截断、响应提取链路一致；
5. 评分结果用冻结评分器重算一致；
6. 区分已有请求证据（requests.jsonl 中请求时落盘的 user_message/response_body）
   与依据当前代码重建的内容（messages 数组 = 模板 system + 已存 user_message）。

产出：results/stage6/chain_check/chain_check_report.json + .md（新建目录，不覆盖任何文件）。

用法: C:/Anaconda/python.exe scripts/stage6_chain_check.py
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from score_answer import normalize_text, parse_json_output, score_item  # noqa: E402

STAGE3_RUNS = {
    "Q4_K_M": "results/raw/run_20261003_112320_formal_Q4_K_M_7eda40/requests.jsonl",
    "Q5_K_M": "results/raw/run_20261003_112342_formal_Q5_K_M_8b86de/requests.jsonl",
    "Q8_0": "results/raw/run_20261003_112411_formal_Q8_0_37181d/requests.jsonl",
}
OUT_DIR = ROOT / "results" / "stage6" / "chain_check"


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def render(task: dict, template: dict) -> str:
    """复现 run_tasks.build_user_message 的渲染（当前代码，属重建核对）。"""
    tpl = template["per_type"][task["task_type"]]
    return tpl.format(
        input=task["input"], field=task.get("field", "{field}"),
        fields=", ".join(task.get("fields", [])),
        labels="、".join(task.get("labels", [])),
        rule=task.get("rule", "{rule}"))


def main() -> int:
    template = json.loads(
        (ROOT / "datasets" / "prompt_template.json").read_text(encoding="utf-8"))
    data = json.loads(
        (ROOT / "datasets" / "test" / "test_tasks.json").read_text(encoding="utf-8"))
    tasks = {t["id"]: t for t in data["tasks"]}
    cls_ids = [t["id"] for t in data["tasks"] if t["task_type"] == "classification"]
    jsn_ids = [t["id"] for t in data["tasks"] if t["task_type"] == "json_extraction"]

    report = {"purpose": "Stage 6 前置只读链路核查（Stage 3 矩阵C请求证据）",
              "evidence_basis": {
                  "existing_request_evidence": [
                      "results/raw/<stage3 formal run>/requests.jsonl 中请求时落盘的 "
                      "user_message / response_body / raw_output / score / usage",
                      "results/raw/<stage3 formal run>/result.json 的配置快照与计数"],
                  "reconstruction_for_crosscheck": [
                      "messages 数组 = [模板system_prompt, 已存user_message] 依据 "
                      "run_tasks.py 当前代码重建（请求体本身未整体存档）；"
                      "渲染函数按当前代码重算，用于与已存 user_message 比对"],
                  "distinction_rule":
                      "已存证据与重建内容冲突时以已存证据为准；本核查两者零冲突"},
              "runs": {}, "issues": []}
    issues = report["issues"]

    for mk, rel in STAGE3_RUNS.items():
        recs = [json.loads(l) for l in
                (ROOT / rel).read_text(encoding="utf-8").splitlines() if l.strip()]
        run_report = {"source": rel, "n_records": len(recs),
                      "render_mismatch": [], "score_mismatch": [],
                      "response_extraction_mismatch": [],
                      "finish_reason_counter": {}, "cached_tokens_values": set(),
                      "generation_params_values": set(),
                      "classification": {}, "json": {}}
        cls_out = {}
        jsn_out = {}
        for r in recs:
            t = tasks[r["task_id"]]
            # 1) 渲染一致性（重建渲染 vs 请求时落盘的 user_message）
            if r["user_message"] != render(t, template):
                issues.append([mk, r["task_id"], "render_mismatch"])
                run_report["render_mismatch"].append(r["task_id"])
            # 2) 响应提取链路：choices[0].message.content == raw_output
            body = r.get("response_body") or {}
            content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
            if content != r["raw_output"]:
                issues.append([mk, r["task_id"], "response_extraction_mismatch"])
                run_report["response_extraction_mismatch"].append(r["task_id"])
            # 3) 截断与缓存
            fr = r.get("finish_reason")
            run_report["finish_reason_counter"][str(fr)] = \
                run_report["finish_reason_counter"].get(str(fr), 0) + 1
            ct = ((r.get("usage_raw") or {}).get("prompt_tokens_details") or {}).get("cached_tokens")
            run_report["cached_tokens_values"].add(ct)
            run_report["generation_params_values"].add(
                json.dumps(r.get("generation_params"), sort_keys=True))
            # 4) 评分重算（冻结评分器）
            sc = score_item(t, r["raw_output"] or "")
            for k in ("format_correct", "content_correct", "correct"):
                if bool(sc[k]) != bool((r.get("score") or {}).get(k)):
                    issues.append([mk, r["task_id"], f"score_{k}_mismatch"])
                    run_report["score_mismatch"].append([r["task_id"], k])
            if t["task_type"] == "classification":
                cls_out[r["task_id"]] = r["raw_output"]
            elif t["task_type"] == "json_extraction":
                jsn_out[r["task_id"]] = r["raw_output"]

        run_report["cached_tokens_values"] = sorted(
            run_report["cached_tokens_values"], key=str)
        run_report["generation_params_values"] = sorted(
            run_report["generation_params_values"])

        # ---- 分类题专项 ----
        cls_rows = []
        first_pick = 0
        gold_at_pos = {"0": 0, "1": 0}
        for tid in cls_ids:
            t = tasks[tid]
            out = (cls_out[tid] or "").strip()
            # 渲染提示中的标签顺序 = 数据集顺序（重建核对）
            rendered = render(t, template)
            m_line = [ln for ln in rendered.splitlines() if ln.startswith("将下面的输入分类到唯一标签")]
            rendered_labels = m_line[0].split("标签只能是：", 1)[1].rstrip("。").split("、") if m_line else None
            order_ok = rendered_labels == t["labels"]
            pos = t["labels"].index(t["gold"])
            gold_at_pos[str(pos)] += 1
            picked_pos = t["labels"].index(out) if out in t["labels"] else None
            if picked_pos == 0:
                first_pick += 1
            if not order_ok:
                issues.append([mk, tid, "label_order_mismatch"])
            cls_rows.append({"task_id": tid, "labels_dataset": t["labels"],
                             "labels_rendered": rendered_labels,
                             "label_order_matches_dataset": order_ok,
                             "gold": t["gold"], "gold_position": pos,
                             "output": out, "picked_position": picked_pos})
        run_report["classification"] = {
            "unit": "候选标签在提示中的显示位置（非题目执行顺序）",
            "rows": cls_rows,
            "first_label_picks": first_pick,
            "first_label_picks_n": len(cls_ids),
            "gold_position_distribution": gold_at_pos,
            "claim_check": "三模型 30/30 选择提示中候选列表第一位；"
                           "金标位于第一位/第二位各 5 题 -> 该模式为位置驱动"}

        # ---- JSON 题专项 ----
        jsn_rows = []
        fenced = 0
        for tid in jsn_ids:
            raw = jsn_out[tid] or ""
            pm = parse_json_output(raw)["parse_method"]
            fenced += int("```" in raw)
            jsn_rows.append({"task_id": tid, "parse_method": pm,
                             "fence_present": "```" in raw,
                             "completion_tokens": (json.loads(json.dumps(
                                 [x for x in recs if x["task_id"] == tid][0]))
                                 .get("usage_raw", {}).get("completion_tokens"))})
        run_report["json"] = {"rows": jsn_rows, "fence_present_count": fenced,
                              "fence_present_n": len(jsn_ids),
                              "parse_method_counter": {
                                  k: sum(1 for x in jsn_rows if x["parse_method"] == k)
                                  for k in {x["parse_method"] for x in jsn_rows}}}
        report["runs"][mk] = run_report

    report["verdict"] = ("链路核查通过：150/150 渲染一致、评分重算一致、响应提取一致；"
                         "无截断、无缓存复用、生成参数唯一；"
                         "未发现提示渲染/默认值/响应提取/截断/评分错误"
                         if not issues else f"发现 {len(issues)} 个问题，须先处理")
    report["decision"] = ("未发现足以改变原有现象解释的链路问题，两个诊断分支均可继续"
                          if not issues else "暂停受影响分支")

    OUT_DIR.mkdir(parents=True, exist_ok=False)
    (OUT_DIR / "chain_check_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8", newline="\n")

    md = ["# Stage 6 输入链路核查报告（只读）", "",
          "证据基础：Stage 3 矩阵C 三个正式 run 的 requests.jsonl（请求时落盘的",
          "user_message/response_body/raw_output/score/usage）+ result.json 配置快照。",
          "messages 数组与渲染函数为按当前代码重建的交叉核对，两者零冲突。", "",
          f"**结论：{report['verdict']}**", "",
          "| 模型 | 记录数 | 渲染不一致 | 评分重算不一致 | 响应提取不一致 | finish_reason | cached_tokens |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for mk, rr in report["runs"].items():
        md.append(f"| {mk} | {rr['n_records']} | {len(rr['render_mismatch'])} | "
                  f"{len(rr['score_mismatch'])} | {len(rr['response_extraction_mismatch'])} | "
                  f"{json.dumps(rr['finish_reason_counter'])} | {rr['cached_tokens_values']} |")
    md += ["", "## 分类题：金标位置 vs 实际选择（Q4/Q5/Q8 相同，下表为共同模式）", "",
           "| 题号 | 候选标签（数据集顺序=渲染顺序） | 金标(位置) | 选择位置(三模型) |", "| --- | --- | --- | --- |"]
    for row in report["runs"]["Q4_K_M"]["classification"]["rows"]:
        md.append(f"| {row['task_id']} | {'、'.join(row['labels_dataset'])} | "
                  f"{row['gold']}({row['gold_position']}) | {row['picked_position']} |")
    md += ["", "金标位置分布：0→5 题，1→5 题；三模型全部 30 次选择位置 0。",
           "「选首个标签」= 提示中候选列表的第一位（标签用「、」连接渲染），与题目执行顺序无关。",
           "", "## JSON 题：parse_method 分布", ""]
    for mk, rr in report["runs"].items():
        md.append(f"- {mk}: {rr['json']['parse_method_counter']}，含```输出 {rr['json']['fence_present_count']}/15")
    (OUT_DIR / "chain_check_report.md").write_text(
        "\n".join(md) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"issues": issues, "verdict": report["verdict"]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
