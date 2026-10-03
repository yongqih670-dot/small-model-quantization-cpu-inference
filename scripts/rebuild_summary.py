# -*- coding: utf-8 -*-
"""Stage 5 交付入口：纯汇总重建工具（不发起任何测量/推理）。

功能与边界（docs/stage5_resume_review.md、docs/stage5_closeout_revision.md、
docs/stage5_rev3_patch.md）：
- 执行**现存快照一致性检查**：把 run 目录启动时写入的
  snapshot_frozen_hashes_stage4.json 与当前冻结清单比对；不一致/缺失/损坏
  即拒绝。该检查只证明"现存快照与当前清单一致"，**不构成对原始启动快照
  可信性的证明**（Stage 4 bench 运行的快照是恢复时覆盖后的版本）；
- 只读原始记录重建汇总，写入**新文件** result_rebuilt.json：保留原始
  started_at_utc，另记重建时间；不覆盖旧 result、快照、失败记录、请求
  记录；不发起任何测量/推理；
- 完整性判定（rev3 严格化）：
  * 单元集合必须恰为 6 线程档 × 5 轮，各出现一次（缺失/多余/重复均
    记入 findings，不得返回 complete）；
  * bench 测量集合必须恰为上述 30 单元 × 3 负载；
  * HTTP 请求必须恰为上述 30 单元 × 计划指定的 5 个 task_id；
  * 核对请求记录与单元的对应关系（unit_key 必须属于网格单元）；
  * 缺失计数字段（tests_valid_count 等）不得用 0 掩盖：记入 findings
    并把状态降为 unverifiable；
- 输出明确的 completeness_status（complete/incomplete/unverifiable）
  与原因，不泛称"完整性通过"。

用法:
    python scripts/rebuild_summary.py --run-id <run_id> --matrix bench|http
    # --run-dir 仅作测试钩子（合成数据只进临时目录）
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")
FROZEN_MANIFEST = ROOT / "datasets" / "frozen_hashes_stage4.json"

EXPECTED_TIERS = [1, 2, 4, 8, 16, 32]
EXPECTED_ROUNDS = [1, 2, 3, 4, 5]
EXPECTED_WORKLOADS = ["pp256", "tg64", "pg256+64"]
TRUTHY_FLAGS = ("execution_complete", "metrics_valid", "cleanup_success")


def sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slots_idle_boolean_strict(body, ok: bool) -> dict:
    """Stage 5 布尔严格版空闲判定（软件测试与将来工具共用）。

    确认空闲当且仅当：请求成功、body 为非空列表、每个元素为含
    `is_processing` 键的 dict、且该键为**布尔值 False**。
    null/0/空字符串/缺字段/空列表/非列表/请求失败均不确认。
    """
    if not ok or not isinstance(body, list) or not body:
        return {"freed": False, "reason": "请求失败或 body 非非空列表"}
    for s in body:
        if not isinstance(s, dict) or "is_processing" not in s:
            return {"freed": False, "reason": "元素缺 is_processing 字段"}
        v = s["is_processing"]
        if not isinstance(v, bool):
            return {"freed": False, "reason": f"is_processing 非布尔值: {v!r}"}
        if v is True:
            return {"freed": False, "reason": "存在忙 slot"}
    return {"freed": True, "reason": "全部 slot 的 is_processing 为布尔 False"}


def check_snapshot_binding(run_dir: Path) -> dict:
    """现存快照一致性检查（不构成对原始启动快照可信性的证明）。"""
    snap_path = run_dir / "snapshot_frozen_hashes_stage4.json"
    if not snap_path.is_file():
        return {"ok": False,
                "reason": f"缺少运行时快照 {snap_path.name}，拒绝重建"}
    try:
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "reason": f"快照损坏: {e}"}
    try:
        cur = json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "reason": f"当前冻结清单不可读: {e}"}
    if snap.get("files", {}) != cur.get("files", {}):
        return {"ok": False,
                "reason": ("现存快照与当前冻结清单不一致；不猜测历史状态，"
                           "拒绝重建")}
    return {"ok": True, "bound_files": len(snap.get("files", {})),
            "note": ("仅证明现存快照与当前清单一致；Stage 4 bench 运行的"
                     "快照是恢复时覆盖后的版本，不代表原始启动时刻")}


def unit_grid_findings(units: list, findings: list) -> dict:
    """单元网格核验：必须恰为 6 档 × 5 轮，各一次。"""
    grid = Counter()
    for u in units:
        tier, rd = u.get("tier"), u.get("round")
        if tier is None or rd is None:
            findings.append(
                f"[unverifiable] 单元缺少 tier/round 字段，无法纳入网格: "
                f"{u.get('file')}")
            continue
        grid[(rd, tier)] += 1
    expected = Counter({(rd, t): 1 for rd in EXPECTED_ROUNDS
                        for t in EXPECTED_TIERS})
    missing = sorted(expected - grid)
    extra = sorted(grid - expected)
    dup = sorted(k for k, c in grid.items() if c > 1)
    if missing:
        findings.append(f"[incomplete] 单元网格缺失 {len(missing)} 个 (轮,档)，"
                        f"例 {missing[:4]}")
    if extra:
        findings.append(f"[incomplete] 单元网格多余 {len(extra)} 个，"
                        f"例 {extra[:4]}")
    if dup:
        findings.append(f"[incomplete] 单元网格重复 {len(dup)} 个，"
                        f"例 {dup[:4]}")
    return {"missing": missing, "extra": extra, "duplicates": dup,
            "complete": not (missing or extra or dup)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--matrix", choices=["bench", "http"], required=True)
    ap.add_argument("--run-dir", default=None,
                    help="测试钩子：显式指定 run 目录（默认 results/raw/<run-id>）")
    args = ap.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else (
        ROOT / "results" / "raw" / args.run_id)
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        print(f"[拒绝] run 不存在: {run_dir}")
        return 1

    binding = check_snapshot_binding(run_dir)
    if not binding["ok"]:
        print(f"[拒绝] {binding['reason']}")
        return 1

    original = json.loads(result_path.read_text(encoding="utf-8"))
    findings = []  # 每条以 [unverifiable] 或 [incomplete] 开头标注类别

    if original.get("run_id") != args.run_id:
        findings.append(f"[unverifiable] result.json run_id="
                        f"{original.get('run_id')!r} 与 --run-id 不符")
    if original.get("matrix") not in (None, args.matrix):
        findings.append(f"[unverifiable] result.json matrix="
                        f"{original.get('matrix')!r} 与 --matrix 不符")

    # ---- 单元清单：按三态旗标判定，不按文件名 ----
    all_units, complete_units, flagged_units, partial_files = [], [], [], []
    for m in sorted(run_dir.glob("unit_*.json")):
        d = json.loads(m.read_text(encoding="utf-8"))
        base = {"file": m.name, "unit_key": d.get("unit_key"),
                "tier": d.get("tier"), "round": d.get("round"),
                "execution_complete": d.get("execution_complete"),
                "metrics_valid": d.get("metrics_valid"),
                "cleanup_success": d.get("cleanup_success"),
                "tests_valid_count": d.get("tests_valid_count"),
                "timed_requests_sent": d.get("timed_requests_sent")}
        all_units.append(base)
        if "_partial" in m.name:
            partial_files.append(base)
        elif all(d.get(k) is True for k in TRUTHY_FLAGS):
            complete_units.append(base)
        else:
            flagged_units.append(base)

    # ---- 网格核验（对全部非 partial 单元） ----
    grid = unit_grid_findings(
        [u for u in all_units if u not in partial_files], findings)

    # ---- bench 测量集合：恰为 30 单元 × 3 负载 ----
    expected_meas = Counter({(f"r{rd}_t{t}", wl): 1
                             for rd in EXPECTED_ROUNDS
                             for t in EXPECTED_TIERS
                             for wl in EXPECTED_WORKLOADS})
    actual_valid_tests = None
    field_missing = []
    if args.matrix == "bench":
        obs_meas = Counter()
        for m in sorted(run_dir.glob("unit_*.json")):
            if "_partial" in m.name:
                continue
            d = json.loads(m.read_text(encoding="utf-8"))
            key = None
            if d.get("round") is not None and d.get("tier") is not None:
                key = f"r{d['round']}_t{d['tier']}"
            n_field = d.get("tests_valid_count")
            n_actual = sum(1 for t in d.get("tests", []) if t.get("valid"))
            actual_valid_tests = (actual_valid_tests or 0) + n_actual
            if n_field is None:
                field_missing.append(m.name)
            elif n_field != n_actual:
                findings.append(
                    f"[unverifiable] {m.name}: tests_valid_count({n_field})"
                    f" 与实际有效条目({n_actual})不一致")
            for t in d.get("tests", []):
                if t.get("valid"):
                    wl = t.get("workload")
                    if key is None or wl not in EXPECTED_WORKLOADS:
                        findings.append(
                            f"[incomplete] {m.name}: 测量条目缺少可归属的"
                            f"(轮,档)或负载未知: {wl!r}")
                    else:
                        obs_meas[(key, wl)] += 1
        if field_missing:
            findings.append(
                f"[unverifiable] {len(field_missing)} 个单元缺少 "
                f"tests_valid_count 字段（不用 0 掩盖）: {field_missing[:3]}")
        missing = sorted(expected_meas - obs_meas)
        extra = sorted(obs_meas - expected_meas)
        dup = sorted(k for k, c in obs_meas.items() if c > 1)
        if missing:
            findings.append(f"[incomplete] bench 测量集合缺失 {len(missing)} "
                            f"键，例 {missing[:3]}")
        if extra:
            findings.append(f"[incomplete] bench 测量集合多余 {len(extra)} "
                            f"键，例 {extra[:3]}")
        if dup:
            findings.append(f"[incomplete] bench 测量集合重复 {len(dup)} 键，"
                            f"例 {dup[:3]}")
        meas_complete = not (missing or extra or dup)
        grid_complete = grid["complete"] and not field_missing

    # ---- HTTP 请求集合：恰为 30 单元 × 5 个指定 task_id ----
    request_records, duplicate_keys = 0, []
    req_file = run_dir / "requests_threads.jsonl"
    recs = []
    if req_file.is_file():
        recs = [json.loads(l) for l in req_file.read_text(encoding="utf-8")
                .splitlines() if l.strip()]
        request_records = len(recs)
        bad_run = [x for x in recs if x.get("run_id") != args.run_id]
        if bad_run:
            findings.append(f"[unverifiable] {len(bad_run)} 条请求记录 "
                            f"run_id 不匹配")
        cnt = Counter((x.get("unit_key"), x.get("task_id"), x.get("round"))
                      for x in recs)
        duplicate_keys = [list(k) for k, c in cnt.items() if c > 1]
        if duplicate_keys:
            findings.append(f"[incomplete] {len(duplicate_keys)} 个重复请求"
                            f"键（原样保留，不重跑不去重）")
    http_set_complete = None
    if args.matrix == "http":
        plan_path = run_dir / "snapshot_stage4_plan.json"
        if not plan_path.is_file():
            plan_path = ROOT / "configs" / "stage4_plan.json"
            plan_note = "使用当前计划文件（run 目录快照缺失）"
        else:
            plan_note = "使用 run 目录快照计划"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        tasks = plan["matrix_B_http"]["task_order_fixed"]
        expected_req = Counter({(f"r{rd}_t{t}", task): 1
                                for rd in EXPECTED_ROUNDS
                                for t in EXPECTED_TIERS for task in tasks})
        obs_req = Counter((x.get("unit_key"), x.get("task_id")) for x in recs)
        missing = sorted(expected_req - obs_req)
        extra = sorted(obs_req - expected_req)
        dup = sorted(k for k, c in obs_req.items() if c > 1)
        if missing:
            findings.append(f"[incomplete] HTTP 请求集合缺失 {len(missing)} "
                            f"键，例 {missing[:3]}")
        if extra:
            findings.append(f"[incomplete] HTTP 请求集合多余 {len(extra)} "
                            f"键，例 {extra[:3]}")
        if dup:
            findings.append(f"[incomplete] HTTP 请求集合重复 {len(dup)} 键，"
                            f"例 {dup[:3]}")
        # 请求-单元对应关系：请求的 unit_key 必须属于网格单元
        known_keys = {f"r{rd}_t{t}" for rd in EXPECTED_ROUNDS
                      for t in EXPECTED_TIERS}
        unknown = sorted({x.get("unit_key") for x in recs
                          if x.get("unit_key") not in known_keys})
        if unknown:
            findings.append(f"[unverifiable] {len(unknown)} 个请求的 unit_key "
                            f"不属于网格单元: {unknown[:3]}")
        # 每单元应有恰好 5 条（每题一条）
        per_unit = Counter(x.get("unit_key") for x in recs)
        bad_count = sorted(k for k, c in per_unit.items() if c != len(tasks))
        if bad_count:
            findings.append(f"[incomplete] {len(bad_count)} 个单元的请求数"
                            f"≠{len(tasks)}: {bad_count[:3]}")
        http_set_complete = not (missing or extra or dup or unknown
                                 or bad_count)

    # ---- 汇总（缺失不补 0；辅助数为推算值） ----
    rebuilt = {
        "rebuilt_from": args.run_id,
        "rebuild_tool": "scripts/rebuild_summary.py",
        "rebuild_time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rebuild_note": "纯汇总重建：未发起任何测量/推理；原始 result.json/"
                        "快照/失败记录/请求记录未改动",
        "snapshot_binding": binding,
        "original_started_at_utc": original.get("started_at_utc"),
        "original_started_at_note": (
            "取自现存 result.json；Stage 4 bench 的该字段为恢复时重写的"
            "开始时间，原始启动时刻仅留存于 run_id 与文件时间戳"),
        "units_complete": len(complete_units),
        "units_flagged_incomplete": flagged_units,
        "units_partial": partial_files,
        "unit_grid": grid,
    }
    if args.matrix == "bench":
        # 仅对字段齐全的单元求和；任何字段缺失已进入 findings，
        # 状态降级，绝不用 0 掩盖
        rebuilt["measurements_total"] = sum(
            u["tests_valid_count"] for u in complete_units
            if u.get("tests_valid_count") is not None)
        rebuilt["measurements_total_note"] = (
            "仅累计 tests_valid_count 字段齐全的完整单元；"
            "字段缺失单元已记入 findings")
        rebuilt["measurements_expected"] = 90
        rebuilt["measurements_actual_valid_entries"] = actual_valid_tests
        rebuilt["measurement_set_complete"] = meas_complete
        rebuilt["measurement_grid_note"] = "期望集合=30 单元×3 负载"
    else:
        rebuilt["timed_requests_total"] = request_records
        rebuilt["timed_requests_expected"] = 150
        rebuilt["timed_requests_note"] = "逐条实测记录计数，缺失不补 0"
        rebuilt["aux_requests_total_derived"] = 3 * len(complete_units)
        rebuilt["aux_requests_note"] = ("推算值(3×完整单元数)：预热/缓存检查"
                                        "未逐条落盘，不标为实测")
        rebuilt["duplicate_request_keys"] = duplicate_keys
        rebuilt["request_set_complete"] = http_set_complete
        rebuilt["request_set_note"] = (f"期望集合=30 单元×5 题（{plan_note}）；"
                                       "请求-单元对应关系已核验")
        rebuilt["requests_are_preserved_verbatim"] = True
    if findings:
        rebuilt["consistency_findings"] = findings

    # ---- 明确的完整性状态 ----
    unverifiable = [f for f in findings if f.startswith("[unverifiable]")]
    incomplete = [f for f in findings if f.startswith("[incomplete]")]
    expected_units = 30
    if unverifiable:
        status, reason = "unverifiable", f"{len(unverifiable)} 项无法核验"
    elif findings or flagged_units or partial_files:
        status, reason = "incomplete", f"{len(incomplete)} 项组合/计数问题"
    elif len(complete_units) != expected_units:
        status, reason = ("incomplete",
                          f"完整单元 {len(complete_units)}/{expected_units}")
    elif (args.matrix == "bench"
          and rebuilt["measurements_total"] != rebuilt["measurements_expected"]):
        status, reason = ("incomplete",
                          f"测量值 {rebuilt['measurements_total']}/90")
    elif (args.matrix == "http"
          and request_records != rebuilt["timed_requests_expected"]):
        status, reason = ("incomplete",
                          f"计时请求 {request_records}/150")
    else:
        status, reason = "complete", (
            f"{len(complete_units)} 单元三态全真，网格/测量或请求集合完整")

    rebuilt["completeness_status"] = status
    rebuilt["completeness_reason"] = reason

    out_path = run_dir / "result_rebuilt.json"
    if out_path.exists():
        print(f"[拒绝] {out_path.name} 已存在，不覆盖历史重建产物")
        return 1
    out_path.write_text(json.dumps(rebuilt, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    try:
        shown = str(out_path.relative_to(ROOT))
    except ValueError:  # --run-dir 测试钩子可能指向项目外
        shown = str(out_path)
    print(f"[ok] 重建完成 -> {shown}")
    print(f"  completeness_status: {status} ({reason})")
    print(f"  findings: {len(findings)}")
    print(f"  原始开始时间: {rebuilt['original_started_at_utc']}"
          f"（注意上方 started_at_note）")
    print(f"  完整单元: {rebuilt['units_complete']}, "
          f"三态不全: {len(flagged_units)}, partial: {len(partial_files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
