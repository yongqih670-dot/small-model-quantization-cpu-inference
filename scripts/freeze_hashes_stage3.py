# -*- coding: utf-8 -*-
"""Stage 3 冻结清单：生成 datasets/frozen_hashes_stage3.json。

Stage 2 的 datasets/frozen_hashes.json 保留不动；本清单覆盖 Stage 3 正式
运行依赖的全部文件（数据集/模板/评分器/配置/计划/协议/runner/preflight）。
冻结后任何被列文件再改动都会导致 preflight 失败，必须说明理由并重新冻结。
"""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FROZEN_FILES = [
    "datasets/dev/dev_tasks.json",
    "datasets/test/test_tasks.json",
    "datasets/prompt_template.json",
    "scripts/score_answer.py",
    "scripts/llama_server_ctl.py",
    "scripts/run_tasks.py",
    "scripts/run_stage3_bench.py",
    "scripts/run_stage3_requestperf.py",
    "scripts/preflight.py",
    "configs/formal_config.json",
    "configs/stage3_plan.json",
    "docs/EXPERIMENT_PROTOCOL.md",
    "docs/STAGE3_PLAN.md",
    "docs/stage3_dataset_changes.md",
]
OUT = ROOT / "datasets" / "frozen_hashes_stage3.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    entries = {}
    for rel in FROZEN_FILES:
        p = ROOT / rel
        if not p.is_file():
            print(f"缺少文件: {rel}")
            return 1
        entries[rel] = sha256(p)
    payload = {
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "algorithm": "sha256",
        "supersedes": "datasets/frozen_hashes.json (Stage 2 清单保留作历史)",
        "rule": ("正式结果产生后不再调整评分规则/数据集/计划；冻结后再改动任何"
                 "被列文件将导致 preflight 失败，须说明理由并重新冻结"),
        "files": entries,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"已冻结 {len(entries)} 个文件 -> {OUT.relative_to(ROOT)}")
    for k, v in entries.items():
        print(f"  {v[:16]}…  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
