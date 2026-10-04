# -*- coding: utf-8 -*-
"""Stage 6 冻结清单生成器（在正式诊断输出产生前运行一次）。

对 configs/stage6_plan.json::freezing.frozen_before_formal 所列文件
计算 SHA256，写出 datasets/stage6/frozen_hashes_stage6.json（映射: 路径 -> sha256）。
清单不含自身（避免自指）。重新生成须说明理由并在报告中记录（历史清单不重冻结）。

用法: C:/Anaconda/python.exe scripts/stage6_freeze.py
"""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")

MANIFEST = ROOT / "datasets" / "stage6" / "frozen_hashes_stage6.json"
FILES = [
    "configs/stage6_plan.json",
    "configs/stage6_config.json",
    "docs/STAGE6_PLAN.md",
    "datasets/stage6/stage6_cls_variants.json",
    "datasets/stage6/stage6_json_variants.json",
    "datasets/stage6/stage6_dev_variants.json",
    "datasets/stage6/stage6_source_map.json",
    "datasets/stage6/stage6_order.json",
    "scripts/stage6_build_variants.py",
    "scripts/stage6_exec.py",
    "scripts/stage6_chain_check.py",
    "scripts/stage6_analyze.py",
    "scripts/score_answer.py",
    "scripts/llama_server_ctl.py",
    "datasets/prompt_template.json",
    "datasets/test/test_tasks.json",
    "datasets/dev/dev_tasks.json",
    "configs/formal_config.json",
    "datasets/frozen_hashes_stage3.json",
    "models/model_manifest_v3.json",
    "environment/backend_manifest.json",
    "results/stage6/chain_check/chain_check_report.json",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refreeze-reason", default=None,
                    help="重新冻结理由（写入清单 refreeze_log；首次冻结不填）")
    args = ap.parse_args()

    hashes = {}
    missing = []
    for rel in FILES:
        p = ROOT / rel
        if not p.is_file():
            missing.append(rel)
            continue
        hashes[rel] = sha256_file(p)
    if missing:
        print(f"[ABORT] 文件缺失，不生成清单: {missing}")
        return 1
    doc = {
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "algorithm": "sha256",
        "rule": ("正式诊断输出产生前冻结（configs/stage6_plan.json::freezing）。"
                 "正式运行前由 scripts/stage6_exec.py 逐文件核验；"
                 "冻结后再改动任何被列文件须记录理由并重新冻结；"
                 "历史冻结清单（Stage 2/3/4）不重冻结、不覆盖。"),
        "not_frozen_note": "scripts/stage6_verify.py 为核验工具本身，不列入（避免自指）",
        "files": hashes}
    if args.refreeze_reason:
        prior = {}
        if MANIFEST.is_file():
            try:
                prior = json.loads(MANIFEST.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                prior = {}
        log = prior.get("refreeze_log", [])
        log.append({"at": doc["frozen_at"], "reason": args.refreeze_reason})
        doc["refreeze_log"] = log
    MANIFEST.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                        encoding="utf-8", newline="\n")
    print(f"冻结清单已生成: {MANIFEST} ({len(hashes)} 文件)"
          + (f" [重新冻结: {args.refreeze_reason}]" if args.refreeze_reason else ""))
    for rel, h in hashes.items():
        print(f"  {h[:16]}… {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
