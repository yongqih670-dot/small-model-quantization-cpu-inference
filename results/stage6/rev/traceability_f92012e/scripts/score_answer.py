# -*- coding: utf-8 -*-
"""评分器（Stage 2 预注册口径，冻结于 datasets/frozen_hashes.json）。

三个独立布尔量（见 docs/EXPERIMENT_PROTOCOL.md §评分口径）：
- format_correct  格式正确
- content_correct 内容正确
- correct         两者同时正确（联合判定）

分母约定：全部计入（失败、超时、截断、格式错误都占分母），不剔除。

各类任务口径：
- exact（单字段抽取 / 规范化转换）
    content_correct := normalize(output) == normalize(gold)
    format_correct  := output.strip() == gold.strip()
        （normalize 额外去掉结尾句读，见 normalize_text；因此 “5。”
         内容对但格式错。数值/日期不做任何智能改写。）
- label（限定标签分类）
    content_correct := normalize(output) == normalize(gold)
    format_correct  := output.strip() in labels
- json（多字段抽取）
    解析规则（仅为"提取 JSON 载荷"的固定规则，不是修复）：
        1) 直接 json.loads(output)；
        2) 失败则取首个 ```json ...``` / ``` ... ``` 围栏内容解析；
        3) 再失败则取第一个 '{' 到最后一个 '}' 的子串解析；
        4) 仍失败 -> 不可解析。
    format_correct := 解析为 dict 且 parse_method=="direct"
                      （无围栏、无额外说明、无多余字段）
    content_correct := 所有 gold 字段存在、类型一致、值相等
                      （多余字段不影响内容判定，但拉低 format）
                      字符串值比较前对称剥离成对外层引号/书名号
                      （见 _values_equal；对应模板"字段值使用输入原文表述"）
    correct := format_correct and content_correct

本评分器绝不调用模型，不做答案改写，不把不可解析输出判对。
"""

from __future__ import annotations

import json
import re

_TRAILING_PUNCT = "。．.！!？?，,；;、\u3000"

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.S)


def normalize_text(text: str) -> str:
    """展示等价归一：去首尾空白 + 去结尾句读（仅结尾，重复应用至尽）。"""
    t = (text or "").strip()
    while t and t[-1] in _TRAILING_PUNCT:
        t = t[:-1].rstrip()
    return t


def parse_json_output(output: str) -> dict:
    """按预注册规则从输出中提取 JSON 载荷。"""
    out = (output or "").strip()
    if not out:
        return {"obj": None, "parse_method": "empty", "surrounding_prose": False}
    # 1) 直接解析
    try:
        return {"obj": json.loads(out), "parse_method": "direct",
                "surrounding_prose": False}
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) 代码围栏
    m = _FENCE_RE.search(out)
    if m:
        try:
            return {"obj": json.loads(m.group(1)), "parse_method": "fenced",
                    "surrounding_prose": True}
        except (json.JSONDecodeError, ValueError):
            pass
    # 3) 首个 '{' 到最后一个 '}'
    if "{" in out and "}" in out:
        sub = out[out.index("{"): out.rindex("}") + 1]
        try:
            return {"obj": json.loads(sub), "parse_method": "brace_slice",
                    "surrounding_prose": True}
        except (json.JSONDecodeError, ValueError):
            return {"obj": None, "parse_method": "unparseable",
                    "surrounding_prose": True}
    return {"obj": None, "parse_method": "unparseable", "surrounding_prose": True}


def _type_class(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _strip_enclosing(s: str) -> str:
    """成对剥离外层引号类标点（书名号/弯引号/直引号/角引号），可重复应用。

    只剥离包围整个值的匹配对（对金标与输出对称作用），不剥离（）——
    圆括号内容可能具有语义（如班级"高二（3）班"）。
    """
    pairs = {"《": "》", "“": "”", '"': '"', "‘": "’", "'": "'",
             "「": "」", "『": "』"}
    changed = True
    while changed and len(s) >= 2:
        changed = False
        for left, right in pairs.items():
            if s.startswith(left) and s.endswith(right):
                s = s[1:-1].strip()
                changed = True
                break
    return s


def _values_equal(gold, got) -> bool:
    """数值按值比较(5 == 5.0)；bool 不与数字混同；字符串先剥离外层引号类
    标点再精确比较（对应模板"字段值使用输入原文中的表述"，源文本可能用
    书名号/引号标注名称，剥离对称作用于两侧）。"""
    tc = _type_class(gold)
    if tc == "number":
        return _type_class(got) == "number" and gold == got
    if tc == "string":
        if not isinstance(got, str):
            return False
        return _strip_enclosing(gold) == _strip_enclosing(got)
    if tc == "null":
        return got is None
    if tc == "bool":
        return isinstance(got, bool) and got == gold
    if tc == "list":
        return isinstance(got, list) and len(got) == len(gold) and all(
            _values_equal(g, x) for g, x in zip(gold, got))
    return got == gold


def score_exact(output: str, gold: str) -> dict:
    norm_out = normalize_text(output)
    norm_gold = normalize_text(gold)
    content = norm_out == norm_gold
    fmt = (output or "").strip() == (gold or "").strip()
    return {"format_correct": fmt, "content_correct": content,
            "correct": fmt and content,
            "detail": {"normalized_output": norm_out, "normalized_gold": norm_gold}}


def score_label(output: str, gold: str, labels: list) -> dict:
    stripped = (output or "").strip()
    norm_out = normalize_text(output)
    content = norm_out == normalize_text(gold)
    fmt = stripped in labels
    return {"format_correct": fmt, "content_correct": content,
            "correct": fmt and content,
            "detail": {"stripped_output": stripped,
                       "in_label_set": fmt,
                       "normalized_output": norm_out,
                       "normalized_gold": normalize_text(gold)}}


def score_json(output: str, gold: dict) -> dict:
    parsed = parse_json_output(output)
    obj = parsed["obj"]
    fields_report = {}
    content = False
    extra_fields = []
    if isinstance(obj, dict):
        extra_fields = sorted(set(obj.keys()) - set(gold.keys()))
        all_ok = True
        for key, gold_val in gold.items():
            present = key in obj
            got = obj.get(key)
            type_ok = _type_class(gold_val) == _type_class(got) or (
                _type_class(gold_val) == "number" and _type_class(got) == "number")
            value_ok = _values_equal(gold_val, got)
            fields_report[key] = {"present": present, "type_ok": bool(type_ok),
                                  "value_ok": bool(value_ok),
                                  "expected_type": _type_class(gold_val),
                                  "got_type": _type_class(got) if present else None,
                                  "expected": gold_val, "got": got}
            if not (present and value_ok):
                all_ok = False
        content = all_ok
    # 格式口径：直接解析为对象 + 无多余字段（围栏/额外说明/多余字段均为格式错误）
    fmt = (parsed["parse_method"] == "direct" and isinstance(obj, dict)
           and not extra_fields)
    return {"format_correct": fmt, "content_correct": content,
            "correct": fmt and content,
            "detail": {"parse_method": parsed["parse_method"],
                       "surrounding_prose": parsed["surrounding_prose"],
                       "extra_fields": extra_fields,
                       "fields": fields_report}}


def score_item(item: dict, output: str) -> dict:
    """按条目的 scoring 方式评分；未知方式抛错（冻结口径之外不做静默兜底）。"""
    scoring = item["scoring"]
    if scoring == "exact":
        res = score_exact(output, item["gold"])
    elif scoring == "label":
        res = score_label(output, item["gold"], item["labels"])
    elif scoring == "json":
        res = score_json(output, item["gold"])
    else:
        raise ValueError(f"未知评分方式: {scoring}")
    res["scoring"] = scoring
    res["truncated_output"] = False  # 由 runner 依据 finish_reason 覆盖
    return res


# ---------------- CLI：人工抽检单条输出 ----------------
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 4:
        print("用法: python score_answer.py <scoring:exact|label|json> "
              "<gold-json> <output>")
        print("  gold-json 例: '\"5\"' 或 '{\"name\":\"张三\"}' 或 "
              "'{\"gold\":\"有效\",\"labels\":[\"有效\",\"无效\"]}'")
        sys.exit(2)
    scoring, gold_raw, output = sys.argv[1], sys.argv[2], sys.argv[3]
    gold_data = json.loads(gold_raw)
    if scoring == "label":
        item = {"scoring": "label", "gold": gold_data["gold"],
                "labels": gold_data["labels"]}
    elif scoring == "exact":
        item = {"scoring": "exact", "gold": gold_data if isinstance(
            gold_data, str) else gold_data["gold"]}
    else:
        item = {"scoring": "json", "gold": gold_data}
    print(json.dumps(score_item(item, output), ensure_ascii=False, indent=2))
