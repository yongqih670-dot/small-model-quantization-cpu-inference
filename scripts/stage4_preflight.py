# -*- coding: utf-8 -*-
"""Stage 4 preflight（只检查、不推理）。

相对 Stage 3 的增强：
- 后端核验覆盖 tools/llama.cpp 下**全部** exe/DLL 指纹（llama/ggml/CPU
  后端/libomp 等实现文件），不仅检查启动器 exe；
- 实时运行 llama-server 与 llama-bench 的 --version（构建号/commit 与
  b11347/5fc4f3c8c 比对），不依赖历史记录中的版本字符串；
- 硬件复核：CPU 型号/物理核/逻辑线程与预注册值（16C/32T）一致，
  变化则停止并要求人工核对，不沿用旧假设；
- 冻结清单（含来源清单与后端全量指纹）逐文件校验。

用法: python scripts/stage4_preflight.py
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")

FROZEN_MANIFEST = ROOT / "datasets" / "frozen_hashes_stage4.json"
TOOLS = ROOT / "tools" / "llama.cpp"
EXPECTED_BUILD, EXPECTED_COMMIT = "11347", "5fc4f3c8c"
EXPECTED_CPU = {"cores": 16, "threads": 32, "name_contains": "Ryzen 9 9955HX"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def live_version(exe: Path) -> dict:
    proc = subprocess.run([str(exe), "--version"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    text = (proc.stderr or "") + (proc.stdout or "")
    return {"exit_code": proc.returncode, "text": text.strip(),
            "build_ok": EXPECTED_BUILD in text and EXPECTED_COMMIT in text}


def main() -> int:
    import llama_server_ctl as ctl
    print("=== Stage 4 preflight（只检查，不启动模型、不推理） ===")
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
              + (f" | {detail}" if detail and not ok else ""))

    # 1. 冻结清单
    try:
        manifest = json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8"))
        bad = []
        for rel, expected in manifest.get("files", {}).items():
            p = ROOT / rel
            if not p.is_file():
                bad.append(f"缺失 {rel}")
            elif sha256(p) != expected:
                bad.append(f"哈希不匹配 {rel}")
        add(f"冻结清单文件校验（{len(manifest.get('files', {}))} 个）",
            not bad, "; ".join(bad[:5]))
        # 2. 后端全量指纹
        bad2 = []
        for rel, meta in manifest.get("tools_inventory", {}).items():
            p = TOOLS / rel
            if not p.is_file():
                bad2.append(f"缺失 {rel}")
            elif sha256(p) != meta["sha256"]:
                bad2.append(f"指纹不匹配 {rel}")
        add(f"后端实现文件全量指纹（{manifest.get('tools_inventory_count')} 个 exe/DLL）",
            not bad2, "; ".join(bad2[:5]))
    except (OSError, json.JSONDecodeError) as e:
        add("冻结清单可读", False, repr(e))
        print(f"\n结果: 未通过（{type(e).__name__}）")
        return 1

    # 3. 实时版本核验（server + bench）
    for exe_name in ("llama-server.exe", "llama-bench.exe"):
        exe = TOOLS / exe_name
        if not exe.is_file():
            add(f"{exe_name} 存在", False)
            continue
        ver = live_version(exe)
        add(f"{exe_name} 实时 --version 为 b{EXPECTED_BUILD}/{EXPECTED_COMMIT}",
            ver["exit_code"] == 0 and ver["build_ok"],
            ver["text"].splitlines()[0] if ver["text"] else "无输出")

    # 4. 模型（Q4_K_M）
    man3 = json.loads((ROOT / "models/model_manifest_v3.json")
                      .read_text(encoding="utf-8"))
    q4 = next(m for m in man3["models"] if m["quantization"] == "Q4_K_M")
    p = ROOT / q4["path"]
    add("Q4_K_M 存在且 SHA256 与 manifest v3 一致",
        p.is_file() and sha256(p) == q4["sha256"], q4["path"])

    # 5. 硬件复核（预注册假设：16C/32T）
    cpu = json.loads(subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
         "(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,"
         "NumberOfLogicalProcessors | ConvertTo-Json)"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace").stdout or "{}")
    hw_ok = (cpu.get("NumberOfCores") == EXPECTED_CPU["cores"]
             and cpu.get("NumberOfLogicalProcessors") == EXPECTED_CPU["threads"]
             and EXPECTED_CPU["name_contains"] in (cpu.get("Name") or ""))
    add(f"硬件与预注册一致（{EXPECTED_CPU['cores']}C/{EXPECTED_CPU['threads']}T）",
        hw_ok, str({k: cpu.get(k) for k in
                    ("Name", "NumberOfCores", "NumberOfLogicalProcessors")}))

    # 6. 资源快照（信息性，不作门禁）
    snap = ctl.resource_snapshot()
    print(f"\n资源快照: {json.dumps(snap, ensure_ascii=False)}")

    passed = all(c["ok"] for c in checks)
    print(f"\n结果: {'通过' if passed else '未通过（拒绝进入正式模式）'}")
    (ROOT / "logs" / "stage4_preflight_latest.json").write_text(
        json.dumps({"passed": passed, "checks": checks,
                    "resource_snapshot": snap}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
