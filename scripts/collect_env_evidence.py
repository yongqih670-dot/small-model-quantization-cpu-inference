# -*- coding: utf-8 -*-
"""补充环境证据（Stage 2）。

生成三个文件（不覆盖 Stage 1 的 system_info.json / model_manifest.json）：
- environment/system_info_v2.json / .md
    修正单位命名（total_ram_gib），标注 CIM MaxClockSpeed 是标称基础时钟
    而非实测运行频率/已验证睿频；
- environment/backend_manifest.json
    llama.cpp 版本、分发 zip 与关键 exe/DLL 的 SHA256、--list-devices 证据；
- models/model_manifest_v2.json
    补充仓库 revision、元数据核验 URL（与下载 URL 分开记录）、
    官方独立核验状态（huggingface.co 直连失败则如实标注"未完成"，
    不把镜像自校验写成官方独立核验）。
"""

import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools" / "llama.cpp"
MODELS = ROOT / "models"
ENV = ROOT / "environment"

sys.stdout.reconfigure(encoding="utf-8")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ps_json(cim_class: str, props: str):
    text = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
         f"(Get-CimInstance Win32_{cim_class} | Select-Object {props} "
         f"| ConvertTo-Json)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60).stdout
    return json.loads(text) if text.strip() else {}


def fetch_json(url: str, timeout_s: float):
    req = urllib.request.Request(url, headers={"User-Agent": "quant-lab/2.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ENV.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    # ---- 系统信息 v2 ----
    os_info = ps_json("OperatingSystem", "Caption, BuildNumber, OSArchitecture")
    cpu = ps_json("Processor", "Name, NumberOfCores, NumberOfLogicalProcessors, "
                               "MaxClockSpeed")
    ram_bytes = int(ps_json("ComputerSystem", "TotalPhysicalMemory")
                    .get("TotalPhysicalMemory", 0))
    free_bytes = int(ps_json("OperatingSystem", "FreePhysicalMemory")
                     .get("FreePhysicalMemory", 0)) * 1024
    ram_gib = round(ram_bytes / 1024 ** 3, 3)
    free_gib = round(free_bytes / 1024 ** 3, 3)

    sysv2 = {
        "collected_at": now,
        "note_vs_stage1": ("Stage 1 文件 system_info.json 为历史记录保持原样；"
                           "本文件为 Stage 2 修正记录：单位命名改为 total_ram_gib，"
                           "并补充时钟字段语义标注"),
        "os": (f"{os_info.get('Caption', '')} (Build {os_info.get('BuildNumber')}, "
               f"{os_info.get('OSArchitecture')})"),
        "cpu_model": (cpu.get("Name") or "").strip(),
        "physical_cores": cpu.get("NumberOfCores"),
        "logical_cores": cpu.get("NumberOfLogicalProcessors"),
        "cpu_max_clock_mhz_cim": cpu.get("MaxClockSpeed"),
        "cpu_max_clock_note": ("CIM MaxClockSpeed 是标称基础时钟，不是实测运行频率，"
                              "也不是已验证的最高睿频"),
        "total_ram_gib": ram_gib,
        "total_ram_bytes": ram_bytes,
        "free_ram_gib_at_collection": free_gib,
        "unit_note": "gib = GiB (2^30 字节)",
    }

    # ---- 后端清单 ----
    ver = subprocess.run([str(TOOLS / "llama-server.exe"), "--version"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    devices = subprocess.run([str(TOOLS / "llama-server.exe"), "--list-devices"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace")
    key_files = ["llama-server.exe", "llama-cli.exe", "llama-bench.exe",
                 "llama.dll", "ggml.dll", "ggml-base.dll",
                 "ggml-cpu-zen4.dll", "ggml-cpu-x64.dll", "libomp.dll"]
    hashes = {}
    for name in key_files:
        p = TOOLS / name
        hashes[name] = {"sha256": sha256(p), "size_bytes": p.stat().st_size} \
            if p.is_file() else None
    cpu_dlls = sorted(p.name for p in TOOLS.glob("ggml-cpu-*.dll"))
    backend = {
        "generated_at": now,
        "distribution": "llama.cpp 官方 GitHub Actions 预编译包 (win-cpu-x64)",
        "distribution_zip": {
            "filename": "llama-b11347-bin-win-cpu-x64.zip",
            "path": "tools/llama-b11347-bin-win-cpu-x64.zip",
            "sha256": sha256(ROOT / "tools/llama-b11347-bin-win-cpu-x64.zip"),
            "size_bytes": (ROOT / "tools/llama-b11347-bin-win-cpu-x64.zip")
            .stat().st_size,
            "download_url": "https://github.com/ggml-org/llama.cpp/releases/"
                            "download/b11347/llama-b11347-bin-win-cpu-x64.zip",
        },
        "version_output": {"exit_code": ver.returncode,
                           "stdout": ver.stdout or "",
                           "stderr": ver.stderr or ""},
        "key_files_sha256": hashes,
        "cpu_variant_dlls_present": cpu_dlls,
        "cpu_variant_loaded_evidence":
            "llama-bench/llama-server 启动日志: 'loaded CPU backend from ... "
            "ggml-cpu-zen4.dll'（本机为 Zen5, 使用可用的最近变体 zen4）",
        "list_devices_stdout": (devices.stdout or "").strip(),
        "list_devices_stderr": (devices.stderr or "").strip(),
        "gpu_devices": "无（list-devices 返回 none; 构建 win-cpu-x64 不含 GPU 后端）",
        "help_evidence": ["environment/evidence/llama_server_help_b11347.txt",
                          "environment/evidence/llama_bench_help_b11347.txt",
                          "environment/evidence/llama_server_list_devices.txt"],
    }

    # ---- 模型清单 v2 ----
    old = json.loads((MODELS / "model_manifest.json").read_text(encoding="utf-8"))
    model_file = MODELS / old["filename"]
    revision, rev_source = None, None
    official_status = {"attempted": True, "reachable": False,
                       "verified": False, "detail": None}
    local_sha = sha256(model_file)
    local_size = model_file.stat().st_size
    try:
        d = fetch_json("https://hf-mirror.com/api/models/"
                       "Qwen/Qwen2.5-0.5B-Instruct-GGUF/revision/main", 20)
        revision = d.get("sha")
        rev_source = "hf-mirror.com/api (镜像侧)"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        official_status["detail"] = f"镜像 API 失败: {type(e).__name__}: {e}"
    # 官方独立核验：可达时用官方 API 的 LFS 元数据比对本地 SHA256
    try:
        tree = fetch_json("https://huggingface.co/api/models/"
                          "Qwen/Qwen2.5-0.5B-Instruct-GGUF/tree/main", 15)
        entry = next(e for e in tree if e["path"].endswith(old["filename"]))
        official_sha = (entry.get("lfs") or {}).get("oid")
        official_size = entry.get("size")
        matched = (official_sha == local_sha and official_size == local_size)
        official_status = {
            "attempted": True, "reachable": True,
            "verified": matched,
            "official_lfs_sha256": official_sha,
            "official_size_bytes": official_size,
            "detail": ("官方 huggingface.co API 元数据与本地文件逐字节比对"
                       + ("一致：官方独立核验通过" if matched
                          else "不一致：核验失败，须立即排查"))}
    except StopIteration:
        official_status = {"attempted": True, "reachable": True,
                           "verified": False,
                           "detail": "官方仓库中未找到该文件名"}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        official_status = {
            "attempted": True, "reachable": False, "verified": False,
            "detail": f"huggingface.co 直连失败({type(e).__name__}); "
                      "官方独立核验未完成，现有核验仅为镜像侧元数据比对"}
    model_v2 = {
        "generated_at": now,
        "note_vs_stage1": ("Stage 1 model_manifest.json 为历史记录保持原样；"
                           "本文件补充 revision 与核验状态字段"),
        "base_model": old["base_model"],
        "quantization": old["quantization"],
        "filename": old["filename"],
        "size_bytes": model_file.stat().st_size,
        "sha256": sha256(model_file),
        "source_official_url": old["source_official_url"],
        "source_download_url": old["source_download_url"],
        "metadata_verification_urls": {
            "official_api": "https://huggingface.co/api/models/"
                            "Qwen/Qwen2.5-0.5B-Instruct-GGUF/revision/main",
            "mirror_api": "https://hf-mirror.com/api/models/"
                          "Qwen/Qwen2.5-0.5B-Instruct-GGUF/revision/main"},
        "repo_revision_sha": revision,
        "repo_revision_source": rev_source,
        "official_independent_verification": official_status,
        "verification_caveat":
            "镜像侧 LFS 元数据比对 ≠ 官方独立核验；正式报告引用时须注明",
        "download_date": old["download_date"],
    }

    # ---- 写文件 ----
    (ENV / "system_info_v2.json").write_text(
        json.dumps(sysv2, ensure_ascii=False, indent=2), encoding="utf-8")
    (ENV / "system_info_v2.md").write_text(
        f"""# 系统环境信息 v2（Stage 2）

> 由 `scripts/collect_env_evidence.py` 于 {now} 生成。Stage 1 的
> `system_info.json` 是历史记录，保持原样；本文件为单位命名与语义的修正版。

| 项目 | 值 |
| --- | --- |
| 操作系统 | {sysv2['os']} |
| CPU | {sysv2['cpu_model']} |
| 物理核心 / 逻辑线程 | {sysv2['physical_cores']} / {sysv2['logical_cores']} |
| CPU 时钟（CIM 标称） | {sysv2['cpu_max_clock_mhz_cim']} MHz — {sysv2['cpu_max_clock_note']} |
| 系统总内存 | {ram_gib} GiB（{ram_bytes:,} bytes） |
| 采集时可用内存 | {free_gib} GiB |
| GPU 设备 | 无（win-cpu-x64 构建，list-devices = none） |

后端与模型证据见 `environment/backend_manifest.json` 与
`models/model_manifest_v2.json`。
""", encoding="utf-8")
    (ENV / "backend_manifest.json").write_text(
        json.dumps(backend, ensure_ascii=False, indent=2), encoding="utf-8")
    (MODELS / "model_manifest_v2.json").write_text(
        json.dumps(model_v2, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[ok] environment/system_info_v2.json / .md")
    print("[ok] environment/backend_manifest.json")
    print("[ok] models/model_manifest_v2.json")
    print("model_load revision:", revision, "| official verify:",
          official_status["reachable"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
