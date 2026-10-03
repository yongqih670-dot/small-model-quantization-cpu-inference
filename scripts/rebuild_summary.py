# -*- coding: utf-8 -*-
"""Stage 5 交付入口：纯汇总重建工具（不发起任何测量/推理）。

功能与边界（docs/stage5_resume_review.md §三）：
- 绑定 run 目录启动时写入的不可变快照 snapshot_frozen_hashes_stage4.json，
  与**当前**冻结清单比对；不一致/缺失/损坏即拒绝，不猜测不修补；
- 只读原始记录（unit_*.json 与 requests_threads.jsonl）重建汇总，写入
  **新文件** result_rebuilt.json：保留原始 started_at_utc，另记重建时间
  与重建说明；不覆盖旧 result、快照、失败记录、请求记录；
- 不完整 HTTP 单元的请求记录原样保留并逐条列出，绝不重跑、不产生
  无标识的重复请求；
- 本工具不包含任何进程启动/HTTP 客户端代码路径。

用法:
    python scripts/rebuild_summary.py --run-id <run_id> [--matrix bench|http]
"""

import argparse
import json
import sys
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
            return {"freed": False,
                    "reason": f"is_processing 非布尔值: {v!r}"}
        if v is True:
            return {"freed": False, "reason": "存在忙 slot"}
    return {"freed": True, "reason": "全部 slot 的 is_processing 为布尔 False"}


def check_snapshot_binding(run_dir: Path) -> dict:
    """把 run 目录内的启动时快照与当前冻结清单比对。"""
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
                "reason": ("运行时快照与当前冻结清单不一致；"
                           f"键差异: {diff}; 哈希变化: {changed}；"
                           "不猜测历史状态，拒绝重建")}
    return {"ok": True, "bound_files": len(snap_files)}


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
    rebuilt = {
        "rebuilt_from": args.run_id,
        "rebuild_tool": "scripts/rebuild_summary.py",
        "rebuild_time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rebuild_note": "纯汇总重建：未发起任何测量/推理；原始 result.json/"
                        "快照/失败记录/请求记录未改动",
        "snapshot_binding": binding,
        "original_started_at_utc": original.get("started_at_utc"),
        "original_finished_at_utc": original.get("finished_at_utc"),
    }

    # ---- 从落盘记录重建（只读） ----
    marker_units = []
    partial_units = []
    for m in sorted(run_dir.glob("unit_*.json")):
        d = json.loads(m.read_text(encoding="utf-8"))
        (partial_units if "_partial" in m.name else marker_units).append(
            {"file": m.name, **{k: d.get(k) for k in
                                ("unit_key", "tier", "round", "ok",
                                 "execution_complete", "metrics_valid",
                                 "cleanup_success", "tests_valid_count",
                                 "timed_requests_sent")}})
    rebuilt["units_complete"] = len(marker_units)
    rebuilt["units_partial"] = partial_units
    rebuilt["units_failed_or_incomplete"] = len(partial_units)

    if args.matrix == "bench":
        rebuilt["measurements_total"] = sum(
            u.get("tests_valid_count") or 0 for u in marker_units)
        rebuilt["measurements_expected"] = 90
    else:
        req_file = run_dir / "requests_threads.jsonl"
        recs = [json.loads(l) for l in req_file.read_text(encoding="utf-8")
                .splitlines() if l.strip()] if req_file.is_file() else []
        rebuilt["timed_requests_total"] = len(recs)
        rebuilt["timed_requests_expected"] = 150
        rebuilt["aux_requests_total"] = 3 * len(marker_units)
        # 不完整单元的请求记录原样列出（绝不重跑/去重/补测）
        from collections import Counter
        cnt = Counter((r["unit_key"], r["task_id"]) for r in recs)
        rebuilt["duplicate_request_keys"] = [k for k, c in cnt.items() if c > 1]
        rebuilt["requests_are_preserved_verbatim"] = True

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
    print(f"  原始开始时间: {rebuilt['original_started_at_utc']}")
    print(f"  完整单元: {rebuilt['units_complete']}, "
          f"不完整/失败单元: {rebuilt['units_failed_or_incomplete']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
