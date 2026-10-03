# -*- coding: utf-8 -*-
"""Stage 5 交付入口：纯汇总重建工具（不发起任何测量/推理）。

功能与边界（docs/stage5_resume_review.md、docs/stage5_closeout_revision.md）：
- 执行**现存快照一致性检查**：把 run 目录启动时写入的
  snapshot_frozen_hashes_stage4.json 与当前冻结清单比对；不一致/缺失/损坏
  即拒绝。该检查只证明"快照与当前清单一致"，**不构成对原始启动快照
  可信性的证明**（Stage 4 bench 运行的快照是恢复时覆盖后的版本）；
- 只读原始记录重建汇总，写入**新文件** result_rebuilt.json：保留原始
  started_at_utc，另记重建时间；不覆盖旧 result、快照、失败记录、请求
  记录；不发起任何测量/推理；
- 完整性判定**不按文件名**：非 partial 单元文件必须三态
  （execution_complete/metrics_valid/cleanup_success）全真才计为完整，
  否则列入 flagged；partial 文件单独列出；
- 缺失指标不补 0；辅助请求数为**推算值**（3×完整单元数）并明确标注，
  不标为实测；
- 交叉核验：请求记录的 run_id 归属、unit_key 重复、bench 实际有效测试
  条目数与字段合计的一致性；
- 输出明确的 completeness_status（complete / incomplete / unverifiable），
  不泛称"完整性通过"。

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
    snap_files = snap.get("files", {})
    cur_files = cur.get("files", {})
    if snap_files != cur_files:
        diff = sorted(set(snap_files) ^ set(cur_files))[:5]
        changed = [k for k in set(snap_files) & set(cur_files)
                   if snap_files[k] != cur_files[k]][:5]
        return {"ok": False,
                "reason": ("现存快照与当前冻结清单不一致；"
                           f"键差异: {diff}; 哈希变化: {changed}；"
                           "不猜测历史状态，拒绝重建")}
    return {"ok": True, "bound_files": len(snap_files),
            "note": ("仅证明现存快照与当前清单一致；Stage 4 bench 运行的"
                     "快照是恢复时覆盖后的版本，不代表原始启动时刻")}
    # （上一行 return 携带说明字段，供 result_rebuilt 原样引用）


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
    findings = []

    # ---- 交叉核验 1：result 的 run_id / matrix 与调用参数一致 ----
    if original.get("run_id") != args.run_id:
        findings.append(
            f"result.json run_id={original.get('run_id')!r} 与 --run-id 不符")
    if original.get("matrix") not in (None, args.matrix):
        findings.append(
            f"result.json matrix={original.get('matrix')!r} 与 --matrix 不符")

    # ---- 单元清单：按三态旗标判定，不按文件名 ----
    complete_units, flagged_units, partial_files = [], [], []
    for m in sorted(run_dir.glob("unit_*.json")):
        d = json.loads(m.read_text(encoding="utf-8"))
        base = {"file": m.name, "unit_key": d.get("unit_key"),
                "tier": d.get("tier"), "round": d.get("round"),
                "execution_complete": d.get("execution_complete"),
                "metrics_valid": d.get("metrics_valid"),
                "cleanup_success": d.get("cleanup_success"),
                "tests_valid_count": d.get("tests_valid_count"),
                "timed_requests_sent": d.get("timed_requests_sent")}
        if "_partial" in m.name:
            partial_files.append(base)
        elif all(d.get(k) is True for k in
                 ("execution_complete", "metrics_valid", "cleanup_success")):
            complete_units.append(base)
        else:
            flagged_units.append(base)

    # ---- 交叉核验 2：请求记录归属与重复 ----
    request_records = 0
    duplicate_keys = []
    req_file = run_dir / "requests_threads.jsonl"
    if req_file.is_file():
        recs = [json.loads(l) for l in req_file.read_text(encoding="utf-8")
                .splitlines() if l.strip()]
        request_records = len(recs)
        bad_run = [x for x in recs if x.get("run_id") != args.run_id]
        if bad_run:
            findings.append(f"{len(bad_run)} 条请求记录 run_id 不匹配")
        cnt = Counter((x.get("unit_key"), x.get("task_id"), x.get("round"))
                      for x in recs)
        duplicate_keys = [list(k) for k, c in cnt.items() if c > 1]
        if duplicate_keys:
            findings.append(f"{len(duplicate_keys)} 个重复请求键（原样保留，"
                            "不重跑不去重）")

    # ---- 交叉核验 3：bench 实际有效测试条目 vs 字段合计 ----
    actual_valid_tests = None
    if args.matrix == "bench":
        actual_valid_tests = 0
        for m in sorted(run_dir.glob("unit_*.json")):
            if "_partial" in m.name:
                continue
            d = json.loads(m.read_text(encoding="utf-8"))
            actual_valid_tests += sum(1 for t in d.get("tests", [])
                                      if t.get("valid"))
        field_sum = sum(u.get("tests_valid_count") or 0 for u in complete_units)
        if actual_valid_tests != field_sum:
            findings.append(
                f"bench 实际有效测试条目({actual_valid_tests})与"
                f"字段合计({field_sum})不一致")

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
    }
    if args.matrix == "bench":
        rebuilt["measurements_total"] = sum(
            (u.get("tests_valid_count") or 0) for u in complete_units)
        rebuilt["measurements_expected"] = 90
        rebuilt["measurements_actual_valid_entries"] = actual_valid_tests
        rebuilt["measurements_note"] = ("字段合计与实际条目已交叉核验；"
                                        "缺失不补 0")
    else:
        rebuilt["timed_requests_total"] = request_records
        rebuilt["timed_requests_expected"] = 150
        rebuilt["timed_requests_note"] = "逐条实测记录计数，缺失不补 0"
        rebuilt["aux_requests_total_derived"] = 3 * len(complete_units)
        rebuilt["aux_requests_note"] = ("推算值(3×完整单元数)：预热/缓存检查"
                                        "未逐条落盘，不标为实测")
        rebuilt["duplicate_request_keys"] = duplicate_keys
        rebuilt["requests_are_preserved_verbatim"] = True

    if findings:
        rebuilt["consistency_findings"] = findings

    # ---- 明确的完整性状态 ----
    expected_complete, expected_total = {
        "bench": (30, 90), "http": (30, 150)}[args.matrix]
    if findings:
        status = "unverifiable"
        status_reason = "存在一致性发现（见 consistency_findings）"
    elif flagged_units or partial_files:
        status = "incomplete"
        status_reason = (f"{len(flagged_units)} 个单元三态不全, "
                         f"{len(partial_files)} 个 partial 单元")
    elif len(complete_units) != expected_complete:
        status = "incomplete"
        status_reason = (f"完整单元 {len(complete_units)}/{expected_complete}")
    elif args.matrix == "bench" and rebuilt["measurements_total"] != expected_total:
        status = "incomplete"
        status_reason = (f"测量值 {rebuilt['measurements_total']}/"
                         f"{expected_total}")
    elif args.matrix == "http" and request_records != expected_total:
        status = "incomplete"
        status_reason = f"计时请求 {request_records}/{expected_total}"
    else:
        status = "complete"
        status_reason = (f"{len(complete_units)} 个单元三态全真且组合完整"
                         if not rebuilt.get("units_flagged_incomplete")
                         else "见 flagged 列表")
    rebuilt["completeness_status"] = status
    rebuilt["completeness_reason"] = status_reason

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
    print(f"  completeness_status: {status} ({status_reason})")
    print(f"  原始开始时间: {rebuilt['original_started_at_utc']}"
          f"（注意上方 started_at_note）")
    print(f"  完整单元: {rebuilt['units_complete']}, "
          f"三态不全: {len(flagged_units)}, partial: {len(partial_files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
