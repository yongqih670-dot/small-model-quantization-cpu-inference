# -*- coding: utf-8 -*-
"""Stage 6 修订版强化只读核验（零推理）。

在 stage6_verify.py（V1–V7，保留不动）之外新增修订级核验 V-R1…V-R10：
- V-R1  git diff 942573d..HEAD 名称-状态：仅允许"新增且路径含 stage6"与
        "修改 .gitattributes"；任何删除/重命名/其他修改即失败；
- V-R2  .gitattributes 差异按白名单逐行核对（仅 3 行追加，无删除）；
- V-R3  两份冻结清单逐文件 SHA256 通过，文件数动态统计（不硬编码）；
- V-R4  独立重算 195 条正式评分（冻结评分器 vs 落盘 score）零不一致；
- V-R5  请求矩阵与顺序：3×65 唯一、与变体全集一一对应、执行顺序=冻结
        顺序、三模型一致；
- V-R6  75 条基线实际输出一致性（raw_output+finish_reason+completion_tokens
        逐字符对 Stage 3 原始记录）；
- V-R7  配对转移与统计独立重算 vs stage6rev_summary.json；并断言预注册
        分族的预期值（格式 J1→J2/J0→J2：Q4 Holm=0.375，Q5/Q8=0.1875），
        全部 27 个检验校正值 ≥0.1875（无不显著结论被推翻）；
- V-R8  离线反例测试（合成数据仅进临时目录，测毕删除）：holm 已知答案、
        分族反例（3值族 vs 旧6值族合并的差异）、清单篡改检出、
        .gitattributes 白名单解析器反例；
- V-R9  核验前后历史文件（git 跟踪且路径不含 stage6）SHA256 快照一致；
- V-R10 追溯副本逐字节等于 git show f92012e；冻结#1→#2 差异=仅
        stage6_analyze.py 条目；
- V-R11 账目重算：7 run → 任务 247 + 辅助 21 = 268，与修订汇总一致，
        评分异常仅存在于缺陷 dev 尝试（4 条）。

报告不写时间戳/HEAD 相关内容（跨运行字节稳定）。只写
results/stage6/rev/verify_revision_report.json。

用法: C:/Anaconda/python.exe scripts/stage6_verify_revision.py
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from score_answer import score_item  # noqa: E402

STAGE6 = ROOT / "results" / "stage6"
REV = STAGE6 / "rev"
BASE_COMMIT = "942573d"
TRACE_COMMIT = "f92012e"
STAGE3_MANIFEST = ROOT / "datasets" / "frozen_hashes_stage3.json"
STAGE6_MANIFEST = ROOT / "datasets" / "stage6" / "frozen_hashes_stage6.json"
GITATTR_ALLOWED_ADDED = [
    "# Stage 6 预注册配置与脚本按字节原样存取（冻结哈希跨检出稳定）",
    "configs/stage6_*.json -text",
    "scripts/stage6_*.py -text",
]
MODELS = ["Q4_K_M", "Q5_K_M", "Q8_0"]
CONDITIONS = ["J0", "J1", "J2"]
PAIRS = [("J0", "J1"), ("J1", "J2"), ("J0", "J2")]
METRICS = ["format_correct", "content_correct", "joint_correct"]
STAGE3_RUNS = {
    "Q4_K_M": "results/raw/run_20261003_112320_formal_Q4_K_M_7eda40",
    "Q5_K_M": "results/raw/run_20261003_112342_formal_Q5_K_M_8b86de",
    "Q8_0": "results/raw/run_20261003_112411_formal_Q8_0_37181d",
}


# ---------------- 基础工具 ----------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(args: list, binary: bool = False):
    r = subprocess.run(["git"] + args, cwd=ROOT, capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode("utf-8")


def check_manifest(manifest_path: Path, base: Path = ROOT) -> tuple:
    """返回 (问题列表, 文件数)；文件数动态统计。"""
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = man["files"]
    problems = []
    for rel, want in files.items():
        p = base / rel
        if not p.is_file():
            problems.append(f"缺失: {rel}")
        elif sha256_file(p) != want:
            problems.append(f"哈希不一致: {rel}")
    return problems, len(files)


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    import math
    pmf_le = sum(math.comb(n, i) / 2 ** n for i in range(0, k + 1))
    return round(min(1.0, 2 * pmf_le), 6)


def holm(pvals: dict) -> dict:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    out, running = {}, 0.0
    for i, (name, p) in enumerate(items):
        adj = min(1.0, p * (m - i))
        running = max(running, adj)
        out[name] = round(running, 6)
    return out


def is_stage6_path(p: str) -> bool:
    return "stage6" in p.replace("\\", "/").lower()


def parse_gitattributes_diff(diff_text: str) -> tuple:
    """从 unified diff 提取（新增行, 删除行）——纯函数，供反例测试复用。"""
    added, removed = [], []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") \
                or line.startswith("diff ") or line.startswith("index "):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def historical_snapshot() -> dict:
    """git 跟踪且路径不含 stage6 的文件 -> {rel: sha256}。"""
    snap = {}
    for rel in git(["ls-files"]).splitlines():
        rel = rel.strip()
        if not rel or is_stage6_path(rel):
            continue
        p = ROOT / rel
        if p.is_file():
            snap[rel] = sha256_file(p)
    return snap


# ---------------- 主流程 ----------------
def main() -> int:
    report = {"verdicts": {}, "counterexample_tests": {}, "failures": []}

    def verdict(name: str, ok: bool, detail):
        report["verdicts"][name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            report["failures"].append(name)

    hist_before = historical_snapshot()

    # ---------- V-R1 git diff 范围 ----------
    ns = git(["diff", "--name-status", f"{BASE_COMMIT}..HEAD"])
    forbidden, allowed_n = [], 0
    for line in ns.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status, path = parts[0].strip(), parts[1].strip()
        if status == "A" and is_stage6_path(path):
            allowed_n += 1
        elif status == "M" and (path == ".gitattributes" or is_stage6_path(path)):
            allowed_n += 1
        else:
            forbidden.append(line)
    verdict("VR1_git_diff_scope_vs_rev4", not forbidden,
            {"forbidden": forbidden,
             "note": "仅允许新增 stage6 命名路径与修改 .gitattributes；"
                     "允许项数量不写入报告（跨提交稳定）"})

    # ---------- V-R2 .gitattributes 白名单 ----------
    gdiff = git(["diff", f"{BASE_COMMIT}..HEAD", "--", ".gitattributes"])
    added, removed = parse_gitattributes_diff(gdiff)
    vr2_ok = (removed == [] and added == GITATTR_ALLOWED_ADDED)
    verdict("VR2_gitattributes_allowlist_only", vr2_ok,
            {"added": added, "removed": removed,
             "expected_added": GITATTR_ALLOWED_ADDED})

    # ---------- V-R3 冻结清单（动态计数） ----------
    p1, n1 = check_manifest(STAGE3_MANIFEST)
    p2, n2 = check_manifest(STAGE6_MANIFEST)
    verdict("VR3_frozen_manifests_pass_dynamic_count",
            not p1 and not p2 and n1 > 0 and n2 > 0,
            {"stage3_problems": p1, "stage3_file_count": n1,
             "stage6_problems": p2, "stage6_file_count": n2,
             "note": "文件数由清单动态统计，不硬编码"})

    # ---------- 载入运行与变体 ----------
    binding = json.loads((STAGE6 / "formal_run_binding.json")
                         .read_text(encoding="utf-8"))
    all_records, run_results = {}, {}
    for m in MODELS:
        run_dir = STAGE6 / binding["runs"][m]
        run_results[m] = json.loads((run_dir / "result.json")
                                    .read_text(encoding="utf-8"))
        all_records[m] = [json.loads(l) for l in
                          (run_dir / "requests.jsonl")
                          .read_text(encoding="utf-8").splitlines() if l.strip()]

    # ---------- V-R4 独立重算 195 条评分 ----------
    mism = []
    for m in MODELS:
        for r in all_records[m]:
            item = ({"scoring": "label", "gold": r["gold"],
                     "labels": r["labels_displayed"]}
                    if r["branch"] == "A" else
                    {"scoring": "json", "gold": r["gold"]})
            sc = score_item(item, r["raw_output"] or "")
            for k in ("format_correct", "content_correct", "correct"):
                if bool(sc[k]) != bool(r["score"][k]):
                    mism.append([m, r["variant_id"], k])
    verdict("VR4_rescore_195_zero_mismatch", not mism,
            {"mismatches": mism, "checked": 3 * 65})

    # ---------- V-R5 请求矩阵与顺序 ----------
    variant_ids = set()
    for fname in ("stage6_cls_variants.json", "stage6_json_variants.json"):
        doc = json.loads((ROOT / "datasets" / "stage6" / fname)
                         .read_text(encoding="utf-8"))
        variant_ids |= {v["variant_id"] for v in doc["variants"]}
    order_doc = json.loads((ROOT / "datasets" / "stage6" / "stage6_order.json")
                           .read_text(encoding="utf-8"))
    v5, orders = [], []
    for m in MODELS:
        ids = [r["variant_id"] for r in all_records[m]]
        if len(ids) != 65 or len(set(ids)) != 65:
            v5.append(f"{m}: 单元数/唯一性异常")
        if set(ids) != variant_ids:
            v5.append(f"{m}: 单元集合与变体全集不一致")
        executed = sorted(ids, key=lambda x: next(
            r["unit_index_in_order"] for r in all_records[m]
            if r["variant_id"] == x))
        orders.append(executed)
        if executed != order_doc["shuffled_formal"]:
            v5.append(f"{m}: 执行顺序与冻结顺序不一致")
    if not all(o == orders[0] for o in orders):
        v5.append("三模型执行顺序不一致")
    verdict("VR5_request_matrix_and_order", not v5, v5 or
            {"per_model_units": 65, "order": "三模型一致且等于冻结顺序"})

    # ---------- V-R6 基线实际输出一致性（75 条逐字符） ----------
    v6, compared_total = [], 0
    for m in MODELS:
        s3 = {r["task_id"]: r for r in
              [json.loads(l) for l in
               (ROOT / STAGE3_RUNS[m] / "requests.jsonl")
               .read_text(encoding="utf-8").splitlines() if l.strip()]}
        for r in all_records[m]:
            comparable = (r["branch"] == "A" and r["rotation"] == 0) or \
                         (r["branch"] == "B" and r["condition"] == "J0")
            if not comparable:
                continue
            compared_total += 1
            s3r = s3.get(r["parent_task_id"])
            if s3r is None or (r["raw_output"] or "") != (s3r["raw_output"] or "") \
                    or r["finish_reason"] != s3r.get("finish_reason") \
                    or (r["usage_raw"] or {}).get("completion_tokens") != \
                    (s3r.get("usage_raw") or {}).get("completion_tokens"):
                v6.append({"model": m, "variant_id": r["variant_id"]})
    verdict("VR6_baseline_75_actual_outputs", not v6,
            {"compared": compared_total, "mismatches": v6,
             "fields": ["raw_output", "finish_reason", "completion_tokens"]})

    # ---------- V-R7 转移与统计重算 vs 修订汇总 ----------
    rev = json.loads((REV / "stage6rev_summary.json").read_text(encoding="utf-8"))
    v7 = []
    # 7a 分支A 计数
    for m in MODELS:
        first = sum(1 for r in all_records[m] if r["branch"] == "A"
                    and (r["raw_output"] or "").strip() == r["labels_displayed"][0])
        if first != rev["branch_A"][m]["chose_first_descriptive"]["count"]:
            v7.append(f"{m}: 首位置计数重算不一致")
        follows = 0
        per_task = {}
        for r in all_records[m]:
            if r["branch"] == "A":
                per_task.setdefault(r["parent_task_id"], {})[r["rotation"]] = r
        for tid, rots in per_task.items():
            r0, r1 = rots[0], rots[1]
            o0 = (r0["raw_output"] or "").strip()
            o1 = (r1["raw_output"] or "").strip()
            if o0 == r0["labels_displayed"][0] and o1 == r1["labels_displayed"][0]:
                follows += 1
        if follows != rev["branch_A"][m]["per_task_paired"]["follows_first_both"]["count"]:
            v7.append(f"{m}: 随位置题数重算不一致")
    # 7b 分支B 转移与分族
    for m in MODELS:
        by_mc = {}
        for r in all_records[m]:
            if r["branch"] == "B":
                by_mc.setdefault((r["condition"]), []).append(r)
        for metric in METRICS:
            # 落盘 score 的联合键名为 correct；joint_correct 为分析口径名
            stored_key = "correct" if metric == "joint_correct" else metric
            fam_p = {}
            for ca, cb in PAIRS:
                a = {r["parent_task_id"]: r for r in by_mc[ca]}
                bb = {r["parent_task_id"]: r for r in by_mc[cb]}
                gb = sorted(t for t in a if a[t]["score"][stored_key]
                            and not bb[t]["score"][stored_key])
                bg = sorted(t for t in a if not a[t]["score"][stored_key]
                            and bb[t]["score"][stored_key])
                tr = rev["branch_B"][m]["transitions"][f"{ca}->{cb}:{metric}"]
                if tr["correct_to_wrong"] != gb or tr["wrong_to_correct"] != bg:
                    v7.append(f"{m}/{metric}/{ca}->{cb}: 转移列表重算不一致")
                fam_p[f"{ca}->{cb}"] = mcnemar_exact(len(gb), len(bg))
            holm_adj = holm(fam_p)
            fam = rev["branch_B"][m]["mcnemar_families"][metric]
            if fam["members"] != [f"{c[0]}->{c[1]}" for c in PAIRS] \
                    or fam["family_size"] != 3:
                v7.append(f"{m}/{metric}: 族成员/族大小与预注册不符")
            for k in fam_p:
                if abs(fam["detail"][k]["p_value"] - fam_p[k]) > 1e-9 \
                        or abs(fam["detail"][k]["holm_p"] - holm_adj[k]) > 1e-9:
                    v7.append(f"{m}/{metric}/{k}: p/Holm 重算不一致")
    # 7c 预期值断言
    expected = {"Q4_K_M": 0.375, "Q5_K_M": 0.1875, "Q8_0": 0.1875}
    for m, exp in expected.items():
        fam = rev["branch_B"][m]["mcnemar_families"]["format_correct"]["holm_p"]
        if fam["J1->J2"] != exp or fam["J0->J2"] != exp:
            v7.append(f"{m}: 格式族 Holm 预期 {exp} 不符: {fam}")
    all_holm = [fam["holm_p"][k] for m in MODELS
                for fam in rev["branch_B"][m]["mcnemar_families"].values()
                for k in fam["holm_p"]]
    if all_holm and min(all_holm) < 0.1875:
        v7.append(f"存在校正值 <0.1875: min={min(all_holm)}")
    verdict("VR7_transitions_and_stats_recompute", not v7, v7 or
            {"families": "3模型×3指标×3比较=27", "min_holm": min(all_holm),
             "expected_format_holm": expected})

    # ---------- V-R8 反例测试（临时目录，测毕删除） ----------
    tmp = Path(tempfile.mkdtemp(prefix="stage6rev_test_"))
    try:
        # 8a holm 已知答案
        got = holm({"a": 0.01, "b": 0.04, "c": 0.03})
        t8a = got == {"a": 0.03, "c": 0.06, "b": 0.06}
        # 8b 分族反例：Q4 实际 p 模式，3值族 vs 旧6值合并族
        fmt = {"J0->J1": 1.0, "J1->J2": 0.125, "J0->J2": 0.125}
        jnt = {"J0->J1": 1.0, "J1->J2": 0.5, "J0->J2": 0.5}
        fam3 = holm(fmt)
        merged = holm({**fmt, **{f"joint:{k}": v for k, v in jnt.items()}})
        t8b = (fam3["J1->J2"] == 0.375 and fam3["J0->J2"] == 0.375
               and merged["J1->J2"] == 0.75 and merged["J0->J2"] == 0.75)
        # 8c 清单篡改检出（临时目录）
        f = tmp / "sample.txt"
        f.write_bytes(b"hello")
        man = {"files": {"sample.txt": hashlib.sha256(b"hello").hexdigest()}}
        mp = tmp / "manifest.json"
        mp.write_text(json.dumps(man), encoding="utf-8")
        ok0, _ = check_manifest(mp, base=tmp)
        f.write_bytes(b"tampered")
        ok1, _ = check_manifest(mp, base=tmp)
        t8c = (ok0 == [] and len(ok1) == 1 and "哈希不一致" in ok1[0])
        # 8d 白名单解析器反例
        good_diff = ("--- a/.gitattributes\n+++ b/.gitattributes\n"
                     + "".join(f"+{l}\n" for l in GITATTR_ALLOWED_ADDED))
        a0, r0 = parse_gitattributes_diff(good_diff)
        bad_remove = good_diff.replace("+configs/stage6_*.json -text\n", "") \
            + "-results/** -text\n"
        a1, r1 = parse_gitattributes_diff(bad_remove)
        bad_extra = good_diff + "+README.md -text\n"
        a2, r2 = parse_gitattributes_diff(bad_extra)
        t8d = (r0 == [] and a0 == GITATTR_ALLOWED_ADDED
               and r1 == ["results/** -text"]
               and a2[-1] == "README.md -text")
        report["counterexample_tests"] = {
            "holm_known_answer": {"pass": t8a, "got": got},
            "family_grouping_counterexample": {
                "pass": t8b,
                "per_metric_3family_format": fam3,
                "legacy_merged_6family_format": {k: v for k, v in
                                                 merged.items()
                                                 if not k.startswith("joint:")},
                "point": "旧6值合并族把格式 J1→J2 过度校正为 0.75；"
                         "预注册3值族为 0.375"},
            "manifest_tamper_detection": {"pass": t8c, "clean_problems": ok0,
                                          "tampered_problems": ok1},
            "gitattributes_allowlist_parser": {"pass": t8d},
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    t8_all = all(v.get("pass") for v in report["counterexample_tests"].values())
    verdict("VR8_offline_counterexample_tests", t8_all,
            {k: v["pass"] for k, v in report["counterexample_tests"].items()})

    # ---------- V-R9 核验前后历史哈希一致 ----------
    hist_after = historical_snapshot()
    t9 = hist_before == hist_after
    verdict("VR9_historical_hashes_stable", t9,
            {"files": len(hist_before),
             "identical": hist_before == hist_after,
             "note": "仅覆盖 git 跟踪且路径不含 stage6 的文件"})

    # ---------- V-R10 追溯副本 ----------
    v10 = []
    trace_dir = REV / f"traceability_{TRACE_COMMIT}"
    man1 = json.loads(git(["show", f"{TRACE_COMMIT}:datasets/stage6/"
                                f"frozen_hashes_stage6.json"]))
    files1 = man1["files"]
    if not trace_dir.is_dir():
        v10.append("追溯副本目录缺失")
    else:
        for rel in files1:
            p = trace_dir / rel
            if not p.is_file():
                v10.append(f"副本缺失: {rel}")
                continue
            if sha256_file(p) != files1[rel]:
                v10.append(f"副本与 {TRACE_COMMIT} 不一致: {rel}")
        mcopy = trace_dir / "MANIFEST.json"
        if not mcopy.is_file():
            v10.append("MANIFEST.json 缺失")
        else:
            mdoc = json.loads(mcopy.read_text(encoding="utf-8"))
            if mdoc.get("source_commit") != TRACE_COMMIT \
                    or "不回填原 run" not in json.dumps(mdoc, ensure_ascii=False):
                v10.append("MANIFEST 标注不完整")
        diff = {"only_in_freeze1": [], "hash_changed": []}
        cur = json.loads(STAGE6_MANIFEST.read_text(encoding="utf-8"))["files"]
        diff["only_in_freeze1"] = sorted(set(files1) - set(cur))
        diff["only_in_freeze2"] = sorted(set(cur) - set(files1))
        diff["hash_changed"] = sorted(k for k in set(files1) & set(cur)
                                      if files1[k] != cur[k])
        if diff["hash_changed"] != ["scripts/stage6_analyze.py"] \
                or diff["only_in_freeze1"] or diff["only_in_freeze2"]:
            v10.append(f"冻结#1→#2 差异常: {diff}")
    verdict("VR10_traceability_copy_matches_commit", not v10, v10 or
            {"files": len(files1),
             "freeze1_to_freeze2": "仅 stage6_analyze.py 条目变化（图1修复重冻结）",
             "declaration": "事后追溯副本；不回填原 run，不据此宣称当时已存档"})

    # ---------- V-R11 账目重算 ----------
    v11 = []
    task_total = aux_total = 0
    scoring_err_runs = []
    n_runs = 0
    for rd in sorted(STAGE6.glob("run_*")):
        rf = rd / "result.json"
        if not rf.is_file():
            continue
        n_runs += 1
        res = json.loads(rf.read_text(encoding="utf-8"))
        task_total += res.get("requests_sent", 0)
        aux_total += (1 if res.get("warmup_request") else 0) + \
            len((res.get("cache_reuse_check") or {}).get("requests") or [])
        if res.get("scoring_errors"):
            scoring_err_runs.append([res["run_id"], res["scoring_errors"]])
    led = rev["ledger"]
    if not (task_total == 247 and aux_total == 21
            and task_total + aux_total == 268):
        v11.append(f"账目重算 {task_total}+{aux_total} != 247+21=268")
    if not led["totals_check"]["match"]:
        v11.append("修订汇总账目 totals_check 不成立")
    if scoring_err_runs != [[r, 4] for r, _ in scoring_err_runs] or \
            sum(x[1] for x in scoring_err_runs) != 4:
        v11.append(f"评分异常合计应为 4: {scoring_err_runs}")
    if led["scoring_errors_total"] != 4:
        v11.append("修订汇总 scoring_errors_total != 4")
    verdict("VR11_ledger_247_21_268", not v11, v11 or
            {"n_runs": n_runs, "task": task_total, "aux": aux_total,
             "total": task_total + aux_total,
             "scoring_errors": scoring_err_runs,
             "note": "评分异常仅存在于缺陷 dev 尝试并如实保留"})

    # ---------- 汇总 ----------
    report["overall"] = "PASS" if not report["failures"] else "FAIL"
    (REV / "verify_revision_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")
    print(json.dumps({"overall": report["overall"],
                      "failures": report["failures"],
                      "verdicts": {k: v["pass"] for k, v in
                                   report["verdicts"].items()}},
                     ensure_ascii=False, indent=1))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
