# -*- coding: utf-8 -*-
"""冻结数据集、提示模板、评分器与本次开发配置的 SHA256。

任何被冻结文件在冻结后再改动，哈希将不再匹配；此时必须：
1) 说明改动理由；2) 重新运行本脚本再冻结；3) 在协议文档记录变更。
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
    "configs/dev_config.json",
    "configs/smoke_test_v2.json",
    "configs/bench_dev.json",
]
OUT = ROOT / "datasets" / "frozen_hashes.json"


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
        "rule": "冻结后如需改动，必须说明理由并重新冻结、在协议文档记录变更",
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
