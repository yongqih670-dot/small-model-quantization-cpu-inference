# -*- coding: utf-8 -*-
"""Stage 4 冻结清单：datasets/frozen_hashes_stage4.json。

除代码/配置/计划/协议外，还包含：
- tools_inventory：tools/llama.cpp 下**全部** exe 与 DLL 的 SHA256
  （llama-server.exe 的启动器身份不足以代表实际实现，须覆盖
  llama/ggml/CPU 后端/libomp 等实现 DLL）；
- 来源清单本身：models/model_manifest_v3.json、
  environment/backend_manifest.json、Stage 3 冻结清单。
"""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools" / "llama.cpp"
OUT = ROOT / "datasets" / "frozen_hashes_stage4.json"

FROZEN_FILES = [
    "configs/stage4_plan.json",
    "configs/stage4_config.json",
    "configs/formal_config.json",
    "scripts/stage4_preflight.py",
    "scripts/stage4_exec.py",
    "scripts/analyze_stage3_revision.py",
    "scripts/analyze_stage4.py",
    "scripts/score_answer.py",
    "scripts/llama_server_ctl.py",
    "datasets/dev/dev_tasks.json",
    "datasets/prompt_template.json",
    "datasets/frozen_hashes_stage3.json",
    "models/model_manifest_v3.json",
    "environment/backend_manifest.json",
    "docs/STAGE4_PLAN.md",
    "docs/stage3_analysis_revision.md",
]


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
    # 后端实现文件全量指纹（exe + dll）
    inventory = {}
    for p in sorted(TOOLS.rglob("*")):
        if p.suffix.lower() in (".exe", ".dll") and p.is_file():
            inventory[str(p.relative_to(TOOLS))] = {
                "sha256": sha256(p), "size_bytes": p.stat().st_size}
    payload = {
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "algorithm": "sha256",
        "stage3_baseline_commit": "0b014a6",
        "rule": ("Stage 4 正式结果产生后不再调整计划/配置/评分；冻结后再改动"
                 "任何被列文件将导致 stage4 preflight 失败"),
        "files": entries,
        "tools_inventory": inventory,
        "tools_inventory_count": len(inventory),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"已冻结 {len(entries)} 个文件 + {len(inventory)} 个后端实现文件指纹")
    return 0


if __name__ == "__main__":
    sys.exit(main())
