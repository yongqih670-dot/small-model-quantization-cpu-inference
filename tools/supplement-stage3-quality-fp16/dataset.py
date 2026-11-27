"""Build and audit independently authored scenarios before any formal inference."""
import json
import random
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_tasks import build_user_message
from score_answer import score_item

KINDS = ("single_field_extraction", "json_extraction", "classification", "normalization")
BANKS = ("extraction.txt", "json_extraction.txt", "classification.txt", "normalization.txt")
MODELS = ("Q4_K_M", "Q5_K_M", "Q8_0", "FP16")
TEMPLATE = ROOT / "datasets/prompt_template.json"
OLDER = [
    ROOT / "datasets/dev/dev_tasks.json", ROOT / "datasets/test/test_tasks.json",
    *(ROOT / "datasets/stage6").glob("*variants.json"),
    ROOT / "stage7_local/smoke_request.json", ROOT / "stage8_extensions/inputs.json",
    ROOT / "stage9_deep/new_items.json", ROOT / "stage10_validation/holdout.json",
    ROOT / "stage10_validation/texts.json",
]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def sections(path):
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].split("|")
        else:
            yield section, line.split("|")


def build():
    rng = random.Random(2026112703)
    tasks = []
    class_balance = {}
    for kind, source in zip(KINDS, BANKS):
        for fields, row in sections(HERE / source):
            family = fields[0]
            if kind == "single_field_extraction":
                field, gold, text = row
                task = dict(field=field, gold=gold, input=text, scoring="exact")
            elif kind == "json_extraction":
                values, text = row
                items = dict(item.split("=", 1) for item in values.split(";"))
                gold = {key: (None if value == "∅" else value) for key, value in items.items()}
                task = dict(fields=list(gold), gold=gold, input=text, scoring="json")
            elif kind == "normalization":
                rule, gold, text = row
                task = dict(rule=rule, gold=gold, input=text, scoring="exact")
            else:
                _, positive, negative, rule = fields
                side, text = row
                if side not in ("+", "-"):
                    raise ValueError(row)
                gold, other = (positive, negative) if side == "+" else (negative, positive)
                if (family, side) not in class_balance:
                    orders = [True] * 5 + [False] * 5
                    rng.shuffle(orders)
                    class_balance[family, side] = iter(orders)
                first = next(class_balance[family, side])
                task = dict(labels=[gold, other] if first else [other, gold], gold=gold,
                            input=f"分类规则：{rule}\n待分类文本：{text}", scoring="label")
            task.update(id=f"q{len(tasks)+1:04}", task_type=kind, source_family=family, source_text=text)
            tasks.append(task)
    return tasks


def pilot():
    examples = [
        dict(field="寄存袋上的墨色", gold="珊瑚橙", input="豆板展歇场时只挂一只寄存袋，签条使用珊瑚橙墨色。"),
        dict(fields=["旗子藏处", "领走人姓名"], gold={"旗子藏处": "小圆门背后", "领走人姓名": None},
             input="演出前把粉旗藏于小圆门背后，谁来领只临时听铃通知。"),
        dict(labels=["已完成", "尚未启动"], gold="尚未启动",
             input="若研磨已经完成则标已完成，若还没开始则标尚未启动。包裹里那块加厚浮石本周一次也没开始研磨。"),
        dict(rule="把给出的秒数写成 MM:SS，补齐两位", gold="03:31", input="工坊特制扣片的回弹调试经过211秒。"),
    ]
    for i, (example, kind, scoring) in enumerate(zip(examples, KINDS, ("exact", "json", "label", "exact")), 1):
        example.update(id=f"pilot{i}", task_type=kind, scoring=scoring, source_text=example["input"],
                       source_family="独立开发试跑")
    return examples


def values(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from values(value)
    elif isinstance(obj, str) and len(obj) >= 12:
        yield obj


def normalized(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\d+(?:[:./-]\d+)*", "数", text)
    return "".join(ch for ch in text if ch.isalnum())


def grams(text):
    return {text[i:i+3] for i in range(len(text)-2)}


def audit(tasks, examples):
    sizes = Counter(task["task_type"] for task in tasks)
    if sizes != {key: 160 for key in KINDS}:
        raise ValueError(f"Unexpected strata {sizes}")
    template = read(TEMPLATE)
    known = {}
    for item in tasks + examples:
        if item["scoring"] == "json":
            gold = json.dumps(item["gold"], ensure_ascii=False)
            if not item["fields"] or len(item["fields"]) != len(set(item["fields"])):
                raise ValueError(item["id"])
            if any(v is not None and v not in item["source_text"] for v in item["gold"].values()):
                raise ValueError(f"Nonverbatim JSON gold: {item}")
        else:
            gold = item["gold"]
        if (item["task_type"] == KINDS[0] and gold != "未提供" and gold not in item["source_text"]):
            raise ValueError(f"Single-field gold not in source: {item}")
        scored = score_item(item, gold)
        if not (scored["correct"] and build_user_message(item, template)):
            raise ValueError(f"Gold or rendering invalid: {item}")
        key = normalized(item["source_text"])
        if not key or key in known:
            raise ValueError(f"Duplicate: {item}")
        known[key] = item["id"]
    by_domain = defaultdict(Counter)
    for item in tasks:
        if item["task_type"] == KINDS[2]:
            by_domain[item["source_family"]][(item["gold"], item["labels"].index(item["gold"]))] += 1
    if len(by_domain) != 8 or any(sorted(counts.values()) != [5] * 4 for counts in by_domain.values()):
        raise ValueError(f"Unbalanced label domains: {by_domain}")

    old = {}
    for path in OLDER:
        for text in values(read(path)):
            key = normalized(text)
            if key:
                old.setdefault(key, path.relative_to(ROOT).as_posix())
    overlap = set(known) & set(old)
    if overlap:
        raise ValueError(f"External exact duplicates: {overlap}")
    history = [(grams(key), origin) for key, origin in old.items() if len(key) >= 12]
    novel = [(grams(key), identifier) for key, identifier in known.items()]
    max_known, max_novel = (0, "", ""), (0, "", "")
    for i, (a, identifier) in enumerate(novel):
        for b, counterpart in novel[:i]:
            score = len(a & b) / len(a | b)
            max_novel = max(max_novel, (score, identifier, counterpart))
        for b, origin in history:
            score = len(a & b) / len(a | b)
            max_known = max(max_known, (score, identifier, origin))
    if max_known[0] >= .50 or max_novel[0] >= .50:
        raise ValueError(f"Near duplicates must be reviewed before freezing: {max_known}, {max_novel}")
    return dict(formal=len(tasks), pilot=len(examples), strata=dict(sizes),
                classification={k: {f"{lab}/{pos+1}": n for (lab, pos), n in v.items()}
                                for k, v in by_domain.items()},
                source_families={key: dict(Counter(x["source_family"] for x in tasks if x["task_type"]==key))
                                 for key in KINDS},
                previous_banks=sorted(set(old.values())), compared_previous_strings=len(history),
                text_screen="NFKC/casefold; replace numbers; remove punctuation; character trigram Jaccard >=0.50 rejected",
                maximum_previous_similarity=max_known, maximum_internal_similarity=max_novel,
                check="Direct gold/format validation against the original frozen scorer and fixed source strings")


def schedule(tasks):
    rng = random.Random(2026112803)
    groups = []
    for kind in KINDS:
        items = [item["id"] for item in tasks if item["task_type"] == kind]
        rng.shuffle(items)
        groups.append(items)
    williams = [(0, 1, 3, 2), (1, 2, 0, 3), (2, 3, 1, 0), (3, 0, 2, 1)]
    orders = []
    for _ in range(4):
        shuffled = williams.copy()
        rng.shuffle(shuffled)
        orders.extend(shuffled)
    out = []
    for i, order in enumerate(orders):
        ids = [item for group in groups for item in group[i*10:(i+1)*10]]
        rng.shuffle(ids)
        out.append(dict(batch=i+1, models=[MODELS[x] for x in order], tasks=ids))
    return out


def main():
    files = ("tasks.json", "pilot.json", "data_audit.json", "schedule.json")
    if any((HERE / name).exists() for name in files):
        raise ValueError("Dataset outputs exist")
    tasks, examples = build(), pilot()
    audited = audit(tasks, examples)
    for name, value in zip(files, (tasks, examples, audited, schedule(tasks))):
        write(HERE / name, value)
    print(f"Audited {len(tasks)} independent item texts and {len(examples)} separate pilot items")


if __name__ == "__main__":
    main()
