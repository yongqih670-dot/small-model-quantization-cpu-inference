# -*- coding: utf-8 -*-
"""真实运行快照回归检查（只读，不调用模型、不写 results/raw）。

针对两个显式绑定的 Stage 4 run：
1. check_snapshot_binding 均须通过（现存快照 ↔ 当前冻结清单一致）；
2. 冻结清单所列文件 + 后端实现文件 + 模型 与实际文件哈希一致；
3. 将两 run 的必要输入复制到临时目录（排除已有 result_rebuilt*.json），
   执行纯汇总重建，核验：
   - completeness_status == complete、无 consistency_findings；
   - 除 rebuild_time_utc 等运行元数据外，与 run 目录中已保存的
     result_rebuilt.json 一致；
   - 正式目录中的历史重建产物不被覆盖（逐字节比对）。

用法: python scripts/verify_snapshot_regression.py
"""

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import rebuild_summary  # noqa: E402
from rebuild_summary import check_snapshot_binding  # noqa: E402
from stage4_preflight import sha256  # noqa: E402

RUNS = {
    "S4_A_bench": ("run_20261003_121218_stage4bench_a2dc0f", "bench"),
    "S4_B_http": ("run_20261003_121547_stage4http_f03b1a", "http"),
}
COPY_PATTERNS = ["result.json", "snapshot_*.json", "unit_*.json",
                 "requests_threads.jsonl"]
METADATA_KEYS = {"rebuild_time_utc", "rebuild_tool", "rebuild_note"}


def main() -> int:
    manifest_path = ROOT / "datasets" / "frozen_hashes_stage4.json"
    ok = True

    # ---- 1. 冻结清单 vs 实际文件 ----
    from preflight import check_frozen_hashes
    problems = check_frozen_hashes(manifest_path)
    print(f"[{'PASS' if not problems else 'FAIL'}] 冻结清单与实际文件一致"
          + (f" | {problems[:3]}" if problems else ""))
    ok &= not problems

    for label, (rid, matrix) in RUNS.items():
        run_dir = ROOT / "results" / "raw" / rid
        print(f"\n== {label}: {rid} ==")

        # ---- 2. 现存快照一致性 ----
        binding = check_snapshot_binding(run_dir)
        print(f"  [{'PASS' if binding['ok'] else 'FAIL'}] 现存快照一致性检查"
              f"（bound_files={binding.get('bound_files')}）")
        ok &= binding["ok"]

        # ---- 3. 临时目录纯重建 ----
        with tempfile.TemporaryDirectory() as td:
            tmp_run = Path(td) / "run"
            tmp_run.mkdir()
            copied = 0
            for pat in COPY_PATTERNS:
                for f in run_dir.glob(pat):
                    shutil.copyfile(f, tmp_run / f.name)
                    copied += 1
            # 保险：确认没有任何重建产物被带入
            leaked = list(tmp_run.glob("result_rebuilt*.json"))
            print(f"  [PASS] 复制必要输入 {copied} 个文件"
                  f"（排除 result_rebuilt*: {'无泄漏' if not leaked else leaked}）")
            ok &= not leaked

            saved = json.loads((run_dir / "result_rebuilt.json")
                               .read_text(encoding="utf-8"))
            real_before = hashlib.sha256((run_dir / "result_rebuilt.json")
                                         .read_bytes()).hexdigest()
            proc = _run_rebuild(matrix, rid, tmp_run)
            print(f"  [{'PASS' if proc.returncode == 0 else 'FAIL'}] "
                  f"临时目录纯重建（exit={proc.returncode}）")
            ok &= proc.returncode == 0
            rb = json.loads((tmp_run / "result_rebuilt.json")
                            .read_text(encoding="utf-8"))

            status_ok = rb.get("completeness_status") == "complete"
            find_ok = not rb.get("consistency_findings")
            print(f"  [{'PASS' if status_ok else 'FAIL'}] completeness_status="
                  f"{rb.get('completeness_status')}")
            print(f"  [{'PASS' if find_ok else 'FAIL'}] consistency_findings="
                  f"{rb.get('consistency_findings') or '无'}")
            ok &= status_ok and find_ok

            saved_wo = {k: v for k, v in saved.items() if k not in METADATA_KEYS}
            rb_wo = {k: v for k, v in rb.items() if k not in METADATA_KEYS}
            same = saved_wo == rb_wo
            diff_keys = (sorted(set(saved_wo) ^ set(rb_wo))
                         or [k for k in saved_wo
                             if saved_wo.get(k) != rb_wo.get(k)][:5])
            print(f"  [{'PASS' if same else 'FAIL'}] 与已保存重建结果一致"
                  f"（除运行元数据 {sorted(METADATA_KEYS)}）"
                  + (f" | 差异键: {diff_keys}" if not same else ""))
            ok &= same

            # ---- 4. 正式目录历史重建产物未被覆盖（前后字节哈希对比） ----
            real_after = hashlib.sha256((run_dir / "result_rebuilt.json")
                                        .read_bytes()).hexdigest()
            untouched = real_before == real_after
            print(f"  [{'PASS' if untouched else 'FAIL'}] 正式目录历史重建"
                  f"产物未被覆盖")
            ok &= untouched

    print(f"\n回归检查: {'通过' if ok else '未通过'}")
    return 0 if ok else 1


def _run_rebuild(matrix: str, rid: str, run_dir: Path):
    import subprocess
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/rebuild_summary.py"),
         "--matrix", matrix, "--run-id", rid, "--run-dir", str(run_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")


if __name__ == "__main__":
    sys.exit(main())
