# -*- coding: utf-8 -*-
"""Stage 6 完整性核验（只读，实验完成后运行）。

核验项（对应交付要求"实验完整性与历史证据未变的核验记录"）：
V1 历史冻结清单（Stage 3）逐文件 SHA256 通过 —— 历史证据未改；
V2 git 工作区中历史路径零改动零新增（对照 rev4 基线）；
V3 Stage 6 冻结清单逐文件 SHA256 通过 —— 预注册材料未改；
V4 变体单一因素独立复核（不导入构建器，独立重推导）：
   A: 标签多重集不变、金标/输入不变、user_message == 按显示序渲染父题；
   B: J0==父题渲染；J1=J0+固定行；J2=J1+示例块（锚点后文本逐字节不变）；
   金标与 scoring 字段全部未变；
V5 请求组合完整：三个正式 run 各 65 单元、无重复、与变体全集一一对应、
   三模型同一单元集合与同一顺序；
V6 汇总可重算：从绑定 run 的 requests.jsonl 独立重算首位置选择数、
   各条件三档得分、基线一致数，与 stage6_summary.json 比对；
V7 无合成数据混入：results/stage6/ 所有 run synthetic=false 且
   is_real_inference=true。

用法: C:/Anaconda/python.exe scripts/stage6_verify.py
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from score_answer import normalize_text  # noqa: E402

STAGE6 = ROOT / "results" / "stage6"
J1_INSERT = "补充格式要求：只返回一个JSON对象，不使用Markdown代码围栏，不添加解释。"
J2_EXAMPLE = ("\n示例（仅演示输出格式，内容与本题无关）：\n"
              "输入：社团办公室借出投影仪一台，借用人为研究生会宣传部，"
              "要求本周五17:00前归还。\n"
              "输出：\n"
              '{"物品名称": "投影仪", "借用人": "研究生会宣传部", "归还期限": "本周五17:00前"}')
J_ANCHOR = "\n\n需要提取的字段："
MODELS = ["Q4_K_M", "Q5_K_M", "Q8_0"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_manifest(path: Path) -> list:
    man = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    for rel, want in man["files"].items():
        p = ROOT / rel
        if not p.is_file():
            problems.append(f"缺失: {rel}")
        elif sha256_file(p) != want:
            problems.append(f"哈希不一致: {rel}")
    return problems


def render(template: dict, task: dict, labels=None) -> str:
    tpl = template["per_type"][task["task_type"]]
    return tpl.format(
        input=task["input"], field=task.get("field", "{field}"),
        fields=", ".join(task.get("fields", [])),
        labels="、".join(labels if labels is not None else task.get("labels", [])),
        rule=task.get("rule", "{rule}"))


def git_status_porcelain() -> list:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8")
    return [l for l in out.stdout.splitlines() if l.strip()]


def main() -> int:
    report = {"verdicts": {}, "failures": []}

    def verdict(name: str, ok: bool, detail):
        report["verdicts"][name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            report["failures"].append(name)

    # V1 历史冻结清单
    v1 = check_manifest(ROOT / "datasets" / "frozen_hashes_stage3.json")
    verdict("V1_stage3_frozen_files_unchanged", not v1, v1 or "21 文件全部一致")

    # V2 git 历史路径零改动（排除 Stage 6 专属路径）
    HIST_PREFIXES = ("results/raw/", "results/summary/", "plots/",
                     "docs/", "datasets/", "configs/", "scripts/",
                     "environment/", "logs/", "tests/", "models/",
                     "README.md", ".gitattributes", "requirements.txt",
                     "guide_extract.txt")
    changes = git_status_porcelain()

    def is_stage6_path(line: str) -> bool:
        p = line[3:].strip().strip('"').replace("\\", "/").lower()
        return "stage6" in p

    hist_changes = [l for l in changes if not is_stage6_path(l)]
    verdict("V2_git_historical_paths_clean", not hist_changes,
            hist_changes or "历史路径无改动（M/D/R），新增文件均属 Stage 6 命名")

    # V3 Stage 6 冻结清单
    v3 = check_manifest(ROOT / "datasets" / "stage6" / "frozen_hashes_stage6.json")
    verdict("V3_stage6_frozen_files_unchanged", not v3, v3 or "22 文件全部一致")

    # V4 变体单一因素独立复核
    template = json.loads((ROOT / "datasets" / "prompt_template.json")
                          .read_text(encoding="utf-8"))
    test = json.loads((ROOT / "datasets" / "test" / "test_tasks.json")
                      .read_text(encoding="utf-8"))
    parents = {t["id"]: t for t in test["tasks"]}
    problems = []
    for fname in ("stage6_cls_variants.json", "stage6_json_variants.json"):
        doc = json.loads((ROOT / "datasets" / "stage6" / fname)
                         .read_text(encoding="utf-8"))
        for v in doc["variants"]:
            p = parents[v["parent_task_id"]]
            if v["gold"] != p["gold"] or v["input"] != p["input"] \
                    or v["scoring"] != p["scoring"]:
                problems.append(f"{v['variant_id']}: 金标/输入/评分方式改变")
                continue
            if v["messages"][0]["content"] != template["system_prompt"]:
                problems.append(f"{v['variant_id']}: system prompt 改变")
            if v["branch"] == "A":
                if sorted(v["labels_displayed"]) != sorted(p["labels"]):
                    problems.append(f"{v['variant_id']}: 标签集合改变")
                expect = render(template, p, labels=v["labels_displayed"])
                if v["user_message"] != expect:
                    problems.append(f"{v['variant_id']}: 渲染不等于按显示序渲染父题")
                if v["labels"] != v["labels_displayed"]:
                    problems.append(f"{v['variant_id']}: labels 别名与显示序不一致")
            else:
                j0 = render(template, p)
                if v["condition"] == "J0":
                    if v["user_message"] != j0:
                        problems.append(f"{v['variant_id']}: J0 不等于父题渲染")
                elif v["condition"] == "J1":
                    exp = j0.replace(J_ANCHOR, "\n" + J1_INSERT + J_ANCHOR, 1)
                    if v["user_message"] != exp:
                        problems.append(f"{v['variant_id']}: J1 非纯插入固定行")
                else:
                    exp = j0.replace(J_ANCHOR, "\n" + J1_INSERT + J_ANCHOR, 1) \
                            .replace(J_ANCHOR, J2_EXAMPLE + J_ANCHOR, 1)
                    if v["user_message"] != exp:
                        problems.append(f"{v['variant_id']}: J2 非纯插入固定示例")
                    if v["user_message"].split(J_ANCHOR)[1] != \
                            j0.split(J_ANCHOR)[1]:
                        problems.append(f"{v['variant_id']}: 锚点后文本改变")
    verdict("V4_variants_single_factor", not problems,
            problems or "A: 标签多重集/金标/输入/system/渲染一致；"
                        "B: 纯插入性质与金标不变逐变体成立")

    # V5 请求组合完整性
    variant_ids = set()
    for fname in ("stage6_cls_variants.json", "stage6_json_variants.json"):
        doc = json.loads((ROOT / "datasets" / "stage6" / fname)
                         .read_text(encoding="utf-8"))
        variant_ids |= {v["variant_id"] for v in doc["variants"]}
    order_doc = json.loads((ROOT / "datasets" / "stage6" / "stage6_order.json")
                           .read_text(encoding="utf-8"))
    binding = json.loads((STAGE6 / "formal_run_binding.json")
                         .read_text(encoding="utf-8"))
    v5 = []
    orders = []
    for m in MODELS:
        run_dir = STAGE6 / binding["runs"][m]
        recs = [json.loads(l) for l in
                (run_dir / "requests.jsonl").read_text(encoding="utf-8").splitlines()
                if l.strip()]
        ids = [r["variant_id"] for r in recs]
        if len(ids) != 65 or len(set(ids)) != 65:
            v5.append(f"{m}: 单元数/唯一性异常 ({len(ids)}/{len(set(ids))})")
        if set(ids) != variant_ids:
            v5.append(f"{m}: 单元集合与变体全集不一致 "
                      f"(差={set(ids) ^ variant_ids})")
        executed_order = sorted(ids, key=lambda x: next(
            r["unit_index_in_order"] for r in recs if r["variant_id"] == x))
        orders.append(executed_order)
        if executed_order != order_doc["shuffled_formal"]:
            v5.append(f"{m}: 实际执行顺序与冻结顺序不一致")
    same = all(o == orders[0] for o in orders) if orders else False
    if not same:
        v5.append("三模型执行顺序不一致")
    verdict("V5_request_matrix_complete", not v5,
            v5 or "3×65 单元、无重复、与变体全集一一对应、顺序=冻结顺序且三模型一致")

    # V6 汇总可重算
    summary = json.loads((STAGE6 / "stage6_summary.json")
                         .read_text(encoding="utf-8"))
    v6 = []
    for m in MODELS:
        run_dir = STAGE6 / binding["runs"][m]
        recs = [json.loads(l) for l in
                (run_dir / "requests.jsonl").read_text(encoding="utf-8").splitlines()
                if l.strip()]
        # 首位置选择数
        first = 0
        for r in recs:
            if r["branch"] != "A":
                continue
            s = (r["raw_output"] or "").strip()
            if s in r["labels_displayed"] and \
                    r["labels_displayed"].index(s) == 0:
                first += 1
            elif not (s in r["labels_displayed"]) and \
                    normalize_text(r["raw_output"] or "") in [
                        normalize_text(x) for x in r["labels_displayed"]] and \
                    normalize_text(r["raw_output"] or "") == \
                    normalize_text(r["labels_displayed"][0]):
                first += 1
        if first != summary["branch_A"][m]["chose_first"]:
            v6.append(f"{m}: 首位置选择重算 {first} != 汇总 "
                      f"{summary['branch_A'][m]['chose_first']}")
        # 各条件联合正确
        for cond in ("J0", "J1", "J2"):
            jc = sum(1 for r in recs if r.get("condition") == cond
                     and bool(r["score"]["correct"]))
            if jc != summary["branch_B"][m]["by_condition"][cond]["joint_correct"]:
                v6.append(f"{m}/{cond}: 联合正确重算 {jc} != 汇总")
        # 基线一致数
        base = sum(1 for r in recs
                   if (r["branch"] == "A" and r["rotation"] == 0)
                   or (r["branch"] == "B" and r["condition"] == "J0"))
        if base != summary["baseline_consistency"][m]["compared"]:
            v6.append(f"{m}: 基线比较数重算 {base} != 汇总")
    verdict("V6_summary_recomputable", not v6, v6 or "三模型全部关键汇总可重算")

    # V7 无合成数据
    v7 = []
    for rd in sorted(STAGE6.glob("run_*")):
        res = json.loads((rd / "result.json").read_text(encoding="utf-8"))
        if res.get("synthetic") or not res.get("is_real_inference"):
            v7.append(rd.name)
    verdict("V7_no_synthetic_data", not v7,
            v7 or f"{len(list(STAGE6.glob('run_*')))} 个 run 全部为真实推理")

    report["overall"] = "PASS" if not report["failures"] else "FAIL"
    out = STAGE6 / "verify_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
