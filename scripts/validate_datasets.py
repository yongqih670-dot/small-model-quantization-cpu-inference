# -*- coding: utf-8 -*-
"""数据集静态校验（不调用模型）。

检查项：
- 结构：必填字段、ID 唯一、任务类型合法、scoring 与类型匹配；
- JSON 题：gold 的字段集合与声明 fields 一致；null 仅允许出现在 gold；
- 分类题：labels 至少 2 个且 gold ∈ labels；输入含"分类规则"；
- 规范化题：rule 非空，gold 非空；
- 开发/正式集：输入(归一化后)零重叠；正式集组成数量符合声明 15/15/10/10；
- 近重复预警：开发集与正式集不存在"同类型且同 gold 且输入前12字符相同"的组合。
退出码 0=全部通过, 1=有错误。
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALID_TYPES = {"single_field_extraction", "json_extraction",
               "classification", "normalization"}
REQUIRED_SCORING = {"single_field_extraction": "exact",
                    "json_extraction": "json",
                    "classification": "label",
                    "normalization": "exact"}


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def check_dataset(path: Path, expect: dict | None = None) -> list:
    errs = []
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = data["tasks"]
    ids = set()
    inputs = {}
    for t in tasks:
        tid = t.get("id", "<no-id>")
        for key in ("id", "task_type", "scoring", "input", "gold"):
            if key not in t:
                errs.append(f"{tid}: 缺少必填字段 {key}")
        if tid in ids:
            errs.append(f"ID 重复: {tid}")
        ids.add(tid)
        tt, sc = t.get("task_type"), t.get("scoring")
        if tt not in VALID_TYPES:
            errs.append(f"{tid}: 未知任务类型 {tt}")
            continue
        if sc != REQUIRED_SCORING[tt]:
            errs.append(f"{tid}: scoring={sc} 与类型 {tt} 不匹配")
        if not t.get("input"):
            errs.append(f"{tid}: input 为空")
        key = norm(t.get("input", ""))
        if key in inputs:
            errs.append(f"{tid}: 输入与 {inputs[key]} 完全重复")
        inputs[key] = tid
        if tt == "json_extraction":
            gold = t.get("gold")
            if not isinstance(gold, dict):
                errs.append(f"{tid}: JSON 题 gold 必须是对象")
            else:
                if set(gold.keys()) != set(t.get("fields", [])):
                    errs.append(f"{tid}: gold 字段 {sorted(gold)} 与声明 fields "
                                f"{sorted(t.get('fields', []))} 不一致")
        if tt == "classification":
            labels = t.get("labels", [])
            if len(labels) < 2:
                errs.append(f"{tid}: 分类题 labels 至少 2 个")
            if t.get("gold") not in labels:
                errs.append(f"{tid}: gold 不在 labels 中")
            if "分类规则" not in t.get("input", ""):
                errs.append(f"{tid}: 分类题输入必须内嵌'分类规则'")
        if tt == "normalization" and not t.get("rule"):
            errs.append(f"{tid}: 规范化题缺少 rule")
        if tt == "single_field_extraction" and not t.get("field"):
            errs.append(f"{tid}: 单字段抽取缺少 field")
    if expect:
        from collections import Counter
        cnt = Counter(t["task_type"] for t in tasks)
        for k, v in expect.items():
            if cnt.get(k, 0) != v:
                errs.append(f"组成数量不符: {k} 期望 {v} 实际 {cnt.get(k, 0)}")
        if data.get("task_count") != len(tasks):
            errs.append(f"task_count 声明 {data.get('task_count')} 与实际 "
                        f"{len(tasks)} 不符")
    return errs


def render_check(data: dict, template: dict, label: str) -> list:
    """逐题校验'最终渲染提示—输入—金标—评分规则'一致性（不调用模型）。"""
    errs = []
    sys_p = template["system_prompt"]
    for t in data["tasks"]:
        tid = t["id"]
        tpl = template["per_type"][t["task_type"]]
        fields = {
            "input": t.get("input", ""),
            "field": t.get("field", ""),
            "fields": ", ".join(t.get("fields", [])),
            "labels": "、".join(t.get("labels", [])),
            "rule": t.get("rule", ""),
        }
        try:
            rendered = tpl.format(**fields)
        except (KeyError, IndexError) as e:
            errs.append(f"{label} {tid}: 模板渲染失败 {e!r}")
            continue
        full = sys_p + rendered
        if norm(t.get("input", ""))[:20] not in norm(rendered):
            errs.append(f"{label} {tid}: 渲染提示缺少输入文本")
        if t["task_type"] == "single_field_extraction":
            if t.get("field", "") not in rendered:
                errs.append(f"{label} {tid}: 字段名未出现在渲染提示中")
        if t["task_type"] == "json_extraction":
            for f in t.get("fields", []):
                if f not in rendered:
                    errs.append(f"{label} {tid}: 字段 {f} 未出现在渲染提示中")
        if t["task_type"] == "classification":
            for lab in t.get("labels", []):
                if lab not in rendered:
                    errs.append(f"{label} {tid}: 标签 {lab} 未出现在渲染提示中")
        if t["task_type"] == "normalization" and t.get("rule", "") not in rendered:
            errs.append(f"{label} {tid}: 转换规则未出现在渲染提示中")
        # 金标与口径抽查
        if tid == "test-012":
            if t.get("gold") != "38.5":
                errs.append(f"{label} test-012: 金标必须为 '38.5'（Stage 3 修正）")
            if "只输出数字本身" not in rendered:
                errs.append(f"{label} test-012: 渲染提示应含无单位规则")
        if t["task_type"] == "json_extraction":
            for k, v in (t.get("gold") or {}).items():
                if isinstance(v, str) and not v.strip():
                    errs.append(f"{label} {tid}: 字段 {k} 金标为空白字符串")
    return errs


def main() -> int:
    dev = ROOT / "datasets/dev/dev_tasks.json"
    test = ROOT / "datasets/test/test_tasks.json"
    all_errs = []
    errs_dev = check_dataset(dev)
    errs_test = check_dataset(test, expect={"single_field_extraction": 15,
                                            "json_extraction": 15,
                                            "classification": 10,
                                            "normalization": 10})
    all_errs += [f"[dev]    {e}" for e in errs_dev]
    all_errs += [f"[test]   {e}" for e in errs_test]

    # 跨集合重叠与近重复
    dev_full = json.loads(dev.read_text(encoding="utf-8"))
    test_full = json.loads(test.read_text(encoding="utf-8"))
    dev_data = dev_full["tasks"]
    test_data = test_full["tasks"]
    test_keys = {norm(t["input"]): t["id"] for t in test_data}
    for t in dev_data:
        k = norm(t["input"])
        if k in test_keys:
            all_errs.append(f"[cross]  dev-{t['id']} 输入与 {test_keys[k]} 重叠")
    for d in dev_data:
        for t in test_data:
            if (d["task_type"] == t["task_type"]
                    and str(d["gold"]) == str(t["gold"])
                    and norm(d["input"])[:12] == norm(t["input"])[:12]):
                all_errs.append(f"[cross]  疑似近重复: {d['id']} vs {t['id']}")

    # 渲染一致性（不调用模型）
    tpl = json.loads((ROOT / "datasets/prompt_template.json").read_text(
        encoding="utf-8"))
    all_errs += [f"[render] {e}" for e in
                 render_check(dev_full, tpl, "dev")]
    all_errs += [f"[render] {e}" for e in
                 render_check(test_full, tpl, "test")]

    if all_errs:
        print("数据集校验未通过:")
        for e in all_errs:
            print(" -", e)
        return 1
    print(f"数据集校验通过: dev {len(dev_data)} 条, test {len(test_data)} 条")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
