# -*- coding: utf-8 -*-
"""Stage 6 变体数据构建器（确定性、无随机、无模型调用）。

从已冻结的 datasets/test/test_tasks.json 与 datasets/dev/dev_tasks.json 生成：
- datasets/stage6/stage6_cls_variants.json   分支A：分类标签循环轮换（20 变体）
- datasets/stage6/stage6_json_variants.json  分支B：JSON J0/J1/J2（45 变体）
- datasets/stage6/stage6_dev_variants.json   开发验证变体（2 分类 + 3 JSON 条件化）
- datasets/stage6/stage6_source_map.json     来源映射（变体 -> 父题 + 精确改动）
- datasets/stage6/stage6_order.json          固定种子执行顺序（canonical + shuffled）

设计要点（预注册，见 docs/STAGE6_PLAN.md 与 configs/stage6_plan.json）：
- 分支A：每题 K 个候选标签做 K 个循环轮换（rot_k = labels[k:]+labels[:k]），
  原顺序即 rot0；每个标签在轮换集合中恰好出现在每个显示位置一次；
  除标签显示顺序外一切不变（题干/金标/标签文字/system prompt/标点）；
  本集 10 题全部 K=2，故每题 2 个变体（rot0 原序 + rot1 交换序）。
  提示模板不含编号，无编号-顺序一致性问题。
- 分支B：J0=原始提示；J1=J0 在「要求」段落后插入固定格式要求行；
  J2=J1 再插入固定格式示例块。插入位置与文字在本脚本中硬编码冻结；
  J1/J2 除插入行外与前一条件逐字节相同（纯插入性质，脚本内校验）。
  示例为独立虚构内容，与正式集字段/金标无交集（脚本内程序化校验）。
- 变体内嵌完整 messages 数组与提示哈希，执行脚本只发送、不再渲染。

构建自带失败即停的校验；任何校验失败则不写出文件。

用法: C:/Anaconda/python.exe scripts/stage6_build_variants.py
"""

import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")

OUT_DIR = ROOT / "datasets" / "stage6"
ORDER_SEED = 20261004

# ---- 分支B 固定插入内容（预注册原文，勿改） ----
J1_INSERT = "补充格式要求：只返回一个JSON对象，不使用Markdown代码围栏，不添加解释。"
J2_EXAMPLE = ("\n示例（仅演示输出格式，内容与本题无关）：\n"
              "输入：社团办公室借出投影仪一台，借用人为研究生会宣传部，"
              "要求本周五17:00前归还。\n"
              "输出：\n"
              '{"物品名称": "投影仪", "借用人": "研究生会宣传部", "归还期限": "本周五17:00前"}')
J_ANCHOR = "\n\n需要提取的字段："


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def messages_of(template: dict, user_msg: str) -> list:
    return [{"role": "system", "content": template["system_prompt"]},
            {"role": "user", "content": user_msg}]


def render(template: dict, task: dict, labels=None) -> str:
    """与 run_tasks.build_user_message 同口径渲染（labels 可替换显示顺序）。"""
    tpl = template["per_type"][task["task_type"]]
    return tpl.format(
        input=task["input"], field=task.get("field", "{field}"),
        fields=", ".join(task.get("fields", [])),
        labels="、".join(labels if labels is not None else task.get("labels", [])),
        rule=task.get("rule", "{rule}"))


def build_cls_variant(template, task, rotation: int) -> dict:
    labels = task["labels"]
    k = len(labels)
    displayed = labels[rotation % k:] + labels[:rotation % k]
    user_msg = render(template, task, labels=displayed)
    msgs = messages_of(template, user_msg)
    return {
        "variant_id": f"{task['id']}__rot{rotation}",
        "branch": "A", "phase": task.get("_phase", "formal"),
        "parent_task_id": task["id"], "task_type": "classification",
        "scoring": task["scoring"], "gold": task["gold"],
        "labels_source_order": labels, "labels_displayed": displayed,
        # 评分兼容别名：score_label 需要 labels 键；集合与源顺序相同，
        # 格式判定为集合成员关系，顺序不影响结果（stage6_verify 另行核验）
        "labels": displayed,
        "rotation": rotation, "n_labels": k,
        "gold_position_in_display": displayed.index(task["gold"]),
        "input": task["input"], "user_message": user_msg, "messages": msgs,
        "prompt_sha256": sha256_text(user_msg),
        "messages_sha256": sha256_text(json.dumps(
            msgs, ensure_ascii=False, separators=(",", ":"))),
        "transformation": {
            "kind": "cyclic_label_rotation",
            "definition": "labels_displayed = labels[k:] + labels[:k], k=rotation",
            "changed_fields": ["labels_displayed(渲染进提示的标签顺序)"],
            "unchanged_fields": ["input", "gold", "标签文字集合", "system prompt",
                                  "模板其余文字与标点", "评分方式"]},
    }


def j1_of(j0: str) -> str:
    assert j0.count(J_ANCHOR) == 1, "J1 锚点不唯一"
    return j0.replace(J_ANCHOR, "\n" + J1_INSERT + J_ANCHOR, 1)


def j2_of(j1: str) -> str:
    assert j1.count(J_ANCHOR) == 1, "J2 锚点不唯一"
    return j1.replace(J_ANCHOR, J2_EXAMPLE + J_ANCHOR, 1)


def build_json_variant(template, task, condition: str) -> dict:
    j0 = render(template, task)
    if condition == "J0":
        user_msg = j0
        transf = {"kind": "none", "changed_fields": [],
                  "unchanged_fields": ["全部（与父题渲染逐字节相同）"]}
    elif condition == "J1":
        user_msg = j1_of(j0)
        transf = {"kind": "fixed_insert",
                  "inserted_text": J1_INSERT,
                  "insert_position": "「要求」段末行之后、空行+需要提取的字段 之前",
                  "changed_fields": ["新增一行格式要求"],
                  "unchanged_fields": ["J0 全部原文", "金标", "字段集", "输入"]}
    elif condition == "J2":
        user_msg = j2_of(j1_of(j0))
        transf = {"kind": "fixed_insert",
                  "inserted_text": J2_EXAMPLE,
                  "insert_position": "J1 插入行之后、空行+需要提取的字段 之前",
                  "changed_fields": ["新增固定格式示例块"],
                  "unchanged_fields": ["J1 全部原文（含其新增行）", "金标", "字段集", "输入"]}
    else:
        raise ValueError(condition)
    msgs = messages_of(template, user_msg)
    return {
        "variant_id": f"{task['id']}__{condition}",
        "branch": "B", "phase": task.get("_phase", "formal"),
        "parent_task_id": task["id"], "task_type": "json_extraction",
        "scoring": task["scoring"], "condition": condition,
        "gold": task["gold"], "fields": task["fields"],
        "input": task["input"], "user_message": user_msg, "messages": msgs,
        "prompt_sha256": sha256_text(user_msg),
        "messages_sha256": sha256_text(json.dumps(
            msgs, ensure_ascii=False, separators=(",", ":"))),
        "transformation": transf,
    }


def verify_variant(v: dict, template, parent: dict) -> None:
    """构建后逐变体校验（失败即停）。"""
    if v["branch"] == "A":
        assert sorted(v["labels_displayed"]) == sorted(parent["labels"]), v["variant_id"]
        assert v["gold"] == parent["gold"] and v["input"] == parent["input"]
        assert v["gold_position_in_display"] == v["labels_displayed"].index(v["gold"])
        # 循环轮换性质：每个标签在每个位置恰好一次（在全部 K 个轮换上成立，
        # 此处对单变体校验渲染与显示顺序一致）
        assert v["user_message"] == render(template, parent, labels=v["labels_displayed"])
    else:
        assert v["gold"] == parent["gold"] and v["input"] == parent["input"]
        assert v["fields"] == parent["fields"]
        j0 = render(template, parent)
        if v["condition"] == "J0":
            assert v["user_message"] == j0
        elif v["condition"] == "J1":
            assert v["user_message"] == j0.replace(
                J_ANCHOR, "\n" + J1_INSERT + J_ANCHOR, 1)
            assert J1_INSERT in v["user_message"]
        else:
            assert v["user_message"] == j0.replace(
                J_ANCHOR, "\n" + J1_INSERT + J_ANCHOR, 1).replace(
                J_ANCHOR, J2_EXAMPLE + J_ANCHOR, 1)
    assert v["messages"][0] == {"role": "system",
                                "content": template["system_prompt"]}
    assert v["messages"][1] == {"role": "user", "content": v["user_message"]}


def main() -> int:
    template = json.loads(
        (ROOT / "datasets" / "prompt_template.json").read_text(encoding="utf-8"))
    test = json.loads(
        (ROOT / "datasets" / "test" / "test_tasks.json").read_text(encoding="utf-8"))
    dev = json.loads(
        (ROOT / "datasets" / "dev" / "dev_tasks.json").read_text(encoding="utf-8"))
    test_tasks = {t["id"]: t for t in test["tasks"]}
    dev_tasks = {t["id"]: t for t in dev["tasks"]}

    cls_ids = [t["id"] for t in test["tasks"] if t["task_type"] == "classification"]
    jsn_ids = [t["id"] for t in test["tasks"] if t["task_type"] == "json_extraction"]
    dev_cls_ids = [t["id"] for t in dev["tasks"] if t["task_type"] == "classification"]
    dev_jsn_ids = [t["id"] for t in dev["tasks"] if t["task_type"] == "json_extraction"]
    assert len(cls_ids) == 10 and len(jsn_ids) == 15
    # K 值核查：分支A请求预算按真实标签数计算
    k_values = {tid: len(test_tasks[tid]["labels"]) for tid in cls_ids}
    assert set(k_values.values()) == {2}, f"非预期标签数: {k_values}"

    # ---- 分支A 变体 ----
    cls_variants = []
    for tid in cls_ids:
        for rot in range(k_values[tid]):
            v = build_cls_variant(template, test_tasks[tid], rot)
            verify_variant(v, template, test_tasks[tid])
            cls_variants.append(v)

    # ---- 分支B 变体 ----
    json_variants = []
    for tid in jsn_ids:
        for cond in ("J0", "J1", "J2"):
            v = build_json_variant(template, test_tasks[tid], cond)
            verify_variant(v, template, test_tasks[tid])
            json_variants.append(v)

    # ---- dev 变体 ----
    dev_variants = []
    for tid in dev_cls_ids:
        t = dict(dev_tasks[tid]); t["_phase"] = "dev"
        for rot in range(len(t["labels"])):
            v = build_cls_variant(template, t, rot)
            verify_variant(v, template, dev_tasks[tid])
            dev_variants.append(v)
    for tid in dev_jsn_ids:
        t = dict(dev_tasks[tid]); t["_phase"] = "dev"
        for cond in ("J0", "J1", "J2"):
            v = build_json_variant(template, t, cond)
            verify_variant(v, template, dev_tasks[tid])
            dev_variants.append(v)

    # ---- 示例内容与正式集无交集（程序化校验） ----
    example_texts = J1_INSERT + J2_EXAMPLE
    formal_strings = set()
    for t in test["tasks"]:
        formal_strings.update(t.get("labels", []))
        formal_strings.update(t.get("fields", []))
        if isinstance(t.get("gold"), dict):
            formal_strings.update(t["gold"].keys())
            formal_strings.update(str(x) for x in t["gold"].values())
        else:
            formal_strings.add(str(t.get("gold")))
    # 长度<2 的字符串（如 test-006 金标"7"）无信息量，不参与子串检查（规则预注册）
    short_ignored = sorted(s for s in formal_strings if len(s) < 2)
    overlap = sorted(s for s in formal_strings
                     if len(s) >= 2 and s in example_texts)
    assert not overlap, f"示例与正式集内容重叠: {overlap}"

    # ---- 纯插入性质校验：J1 = J0 + 1行；J2 = J1 + 示例块（除锚点位移外逐字节） ----
    for tid in jsn_ids:
        j0 = render(template, test_tasks[tid])
        j1 = j1_of(j0)
        j2 = j2_of(j1)
        pre0, pre1 = j0.split(J_ANCHOR)[0], j1.split(J_ANCHOR)[0]
        assert pre1 == pre0 + "\n" + J1_INSERT
        assert j2.split(J_ANCHOR)[0] == pre1 + J2_EXAMPLE
        assert j0.replace(J_ANCHOR, "@@") .split("@@")[1] == \
               j1.replace(J_ANCHOR, "@@").split("@@")[1] == \
               j2.replace(J_ANCHOR, "@@").split("@@")[1], "锚点之后文本必须逐字节相同"

    # ---- 来源映射 ----
    source_map = {
        "purpose": "Stage 6 变体来源映射（变体 -> 父题 + 精确改动；父题均出自冻结数据集）",
        "parent_sources": {
            "formal": "datasets/test/test_tasks.json (test_tasks_v2 rev2, 已冻结)",
            "dev": "datasets/dev/dev_tasks.json (已冻结)"},
        "fixed_insert_texts": {"J1": J1_INSERT, "J2": J2_EXAMPLE,
                               "anchor": J_ANCHOR},
        "variants": {}}
    for v in cls_variants + json_variants + dev_variants:
        source_map["variants"][v["variant_id"]] = {
            "parent_task_id": v["parent_task_id"],
            "branch": v["branch"], "phase": v["phase"],
            "condition_or_rotation": v.get("condition", f"rot{v.get('rotation')}"),
            "prompt_sha256": v["prompt_sha256"],
            "transformation": v["transformation"]}

    # ---- 执行顺序（固定种子，先 canonical 后 shuffle；dev 与 formal 同种子） ----
    canonical_formal = [v["variant_id"] for v in cls_variants] + \
                       [v["variant_id"] for v in json_variants]
    canonical_dev = [v["variant_id"] for v in dev_variants]
    shuffled_formal = list(canonical_formal)
    random.Random(ORDER_SEED).shuffle(shuffled_formal)
    shuffled_dev = list(canonical_dev)
    random.Random(ORDER_SEED).shuffle(shuffled_dev)
    order_doc = {
        "seed": ORDER_SEED,
        "rule": "random.Random(seed).shuffle(canonical)；同一 shuffled 顺序用于全部三个模型，便于配对",
        "canonical_formal": canonical_formal,
        "shuffled_formal": shuffled_formal,
        "canonical_dev": canonical_dev,
        "shuffled_dev": shuffled_dev,
        "counts": {"formal_units": len(canonical_formal),
                   "dev_units": len(canonical_dev),
                   "formal_requests": len(canonical_formal) * 3,
                   "dev_requests": len(canonical_dev) * 3}}

    OUT_DIR.mkdir(parents=True, exist_ok=False)
    files = {
        "stage6_cls_variants.json": {"dataset_id": "stage6_cls_variants_v1",
                                     "purpose": "分支A：分类标签循环轮换变体（10题×2序，全部K=2）",
                                     "variants": cls_variants},
        "stage6_json_variants.json": {"dataset_id": "stage6_json_variants_v1",
                                      "purpose": "分支B：JSON格式遵循变体（15题×J0/J1/J2）",
                                      "variants": json_variants},
        "stage6_dev_variants.json": {"dataset_id": "stage6_dev_variants_v1",
                                     "purpose": "开发题链路验证变体（2分类×2序 + 3JSON×3条件）",
                                     "variants": dev_variants},
        "stage6_source_map.json": source_map,
        "stage6_order.json": order_doc,
    }
    for name, doc in files.items():
        (OUT_DIR / name).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8",
            newline="\n")

    print(json.dumps({
        "cls_variants": len(cls_variants), "json_variants": len(json_variants),
        "dev_variants": len(dev_variants),
        "formal_units_per_model": len(canonical_formal),
        "formal_requests_total": len(canonical_formal) * 3,
        "dev_requests_total": len(canonical_dev) * 3,
        "k_values": k_values, "example_overlap": overlap,
        "example_overlap_check_ignored_short_strings": short_ignored,
        "out_dir": str(OUT_DIR)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
