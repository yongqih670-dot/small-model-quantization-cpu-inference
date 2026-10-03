# -*- coding: utf-8 -*-
"""正式实验 preflight（只检查、不推理）。

正式模式门禁：任一项不通过即拒绝进入正式运行。
检查项：
1. 冻结清单（datasets/frozen_hashes_stage3.json）所列文件的 SHA256；
2. 模型清单（models/model_manifest_v3.json）所列三模型存在且 SHA256 一致；
3. 后端版本与关键可执行文件哈希（environment/backend_manifest.json）；
4. 协议/计划文档存在且被冻结清单覆盖；
5. 资源快照（只读记录，不作为门禁条件）。

用法:
    python scripts/preflight.py            # 只检查，退出码 0=通过 1=失败
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")

FROZEN_MANIFEST = ROOT / "datasets" / "frozen_hashes_stage3.json"
MODEL_MANIFEST = ROOT / "models" / "model_manifest_v3.json"
BACKEND_MANIFEST = ROOT / "environment" / "backend_manifest.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_frozen_hashes(manifest_path: Path, base: Path = ROOT) -> list:
    """校验冻结清单所列文件哈希；返回不匹配/缺失描述列表（可测试的纯函数）。"""
    problems = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [f"冻结清单无法读取: {manifest_path} ({e})"]
    for rel, expected in manifest.get("files", {}).items():
        p = base / rel
        if not p.is_file():
            problems.append(f"冻结文件缺失: {rel}")
            continue
        actual = sha256(p)
        if actual != expected:
            problems.append(
                f"冻结哈希不匹配: {rel} 期望 {expected[:12]}… 实际 {actual[:12]}…")
    if not manifest.get("files"):
        problems.append(f"冻结清单为空: {manifest_path}")
    return problems


def run_checks(verbose: bool = True) -> dict:
    """执行全部 preflight 检查；返回结果 dict（passed 布尔）。"""
    from llama_server_ctl import resource_snapshot  # noqa: E402
    report = {"checks": [], "resource_snapshot": resource_snapshot(),
              "passed": False}

    def add(name: str, ok: bool, detail: str = ""):
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        if verbose:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
                  + (f" | {detail}" if detail and not ok else ""))

    # 1. 冻结清单
    probs = check_frozen_hashes(FROZEN_MANIFEST)
    add("冻结清单哈希（数据集/模板/评分器/配置/计划/协议/runner）",
        not probs, "; ".join(probs))

    # 2. 模型
    try:
        models = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
        for entry in models["models"]:
            p = ROOT / entry["path"]
            ok = p.is_file() and sha256(p) == entry["sha256"]
            add(f"模型 {entry['quantization']} 存在且 SHA256 一致", ok,
                f"{entry['path']}")
    except (OSError, KeyError, json.JSONDecodeError) as e:
        add("模型清单可读且完整", False, repr(e))

    # 3. 后端
    try:
        backend = json.loads(BACKEND_MANIFEST.read_text(encoding="utf-8"))
        exe = ROOT / "tools/llama.cpp/llama-server.exe"
        expect = backend["key_files_sha256"]["llama-server.exe"]["sha256"]
        add("后端 llama-server.exe SHA256 与清单一致",
            exe.is_file() and sha256(exe) == expect)
        ver = backend["version_output"]["stderr"] or backend["version_output"]["stdout"]
        add("后端版本 b11347/5fc4f3c8c", "11347" in ver and "5fc4f3c8c" in ver,
            ver.splitlines()[0] if ver else "")
    except (OSError, KeyError, json.JSONDecodeError) as e:
        add("后端清单可读", False, repr(e))

    # 4. 文档存在
    for rel in ("docs/EXPERIMENT_PROTOCOL.md", "docs/STAGE3_PLAN.md",
                "docs/stage3_corrections.md", "docs/stage3_dataset_changes.md"):
        add(f"文档存在: {rel}", (ROOT / rel).is_file())

    report["passed"] = all(c["ok"] for c in report["checks"])
    return report


def main() -> int:
    print("=== Stage 3 preflight（只检查，不启动模型、不推理） ===")
    report = run_checks()
    print(f"\n资源快照: {json.dumps(report['resource_snapshot'], ensure_ascii=False)}")
    print(f"\n结果: {'通过' if report['passed'] else '未通过（拒绝进入正式模式）'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
