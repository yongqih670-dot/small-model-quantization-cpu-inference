# -*- coding: utf-8 -*-
"""采集本机环境信息，生成 environment/system_info.json 与 system_info.md。

所有数据在运行时从当前系统实际读取（WMI / PowerShell / 平台 API），
不使用任何硬编码硬件信息。可重复执行以刷新结果。

用法:
    python scripts/collect_system_info.py
"""

import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "environment"
TOOLS_DIR = ROOT / "tools"


def run_cmd(args, timeout=30, combine=False):
    """运行命令并返回 stdout（combine=True 时合并 stderr），失败返回 None。"""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            out = proc.stdout or ""
            if combine and proc.stderr:
                out = (out + "\n" + proc.stderr).strip()
            return out.strip() if out else None
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def ps_query(cim_class, keys):
    """通过 PowerShell CIM 查询硬件信息（从系统实际读取，不做猜测）。

    PowerShell 默认按控制台代码页（GBK）输出，这里强制其以 UTF-8 输出，
    避免 Windows 中文系统下读取到的字符串乱码。
    """
    props = ", ".join(keys)
    text = run_cmd([
        "powershell", "-NoProfile", "-Command",
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"(Get-CimInstance Win32_{cim_class} | Select-Object {props} "
        f"| ConvertTo-Json)"
    ])
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def detect_compilers():
    found = {}
    for name in ("gcc", "g++", "cmake", "cl", "make", "ninja"):
        path = shutil.which(name)
        found[name] = path if path else None
    return found


def detect_backend():
    """检测 llama.cpp 预编译程序与 llama-cpp-python。"""
    info = {"llama_cpp_binaries": {}, "llama_cpp_version": None,
            "llama_cpp_source": None, "llama_cpp_python": None}

    # 1) llama.cpp 原生程序（tools/llama.cpp 下）
    if TOOLS_DIR.is_dir():
        for exe in sorted(TOOLS_DIR.rglob("*.exe")):
            rel = str(exe.relative_to(TOOLS_DIR))
            info["llama_cpp_binaries"][rel] = str(exe)
    server = None
    for cand in info["llama_cpp_binaries"].values():
        if Path(cand).name.lower() == "llama-server.exe":
            server = cand
            break
    if server:
        # llama-server --version 把版本信息打印到 stderr（退出码 0），需合并读取
        ver = run_cmd([server, "--version"], timeout=30, combine=True)
        info["llama_cpp_version"] = ver
        marker = TOOLS_DIR / "llama.cpp" / "DOWNLOAD_SOURCE.txt"
        if marker.is_file():
            info["llama_cpp_source"] = marker.read_text(encoding="utf-8").strip()

    # 2) llama-cpp-python（当前解释器环境）
    pip_show = run_cmd([sys.executable, "-m", "pip", "show", "llama-cpp-python"])
    info["llama_cpp_python"] = pip_show if pip_show else None
    return info


def main():
    OUT_DIR.mkdir(exist_ok=True)

    # ---- OS ----
    os_info = ps_query("OperatingSystem", ["Caption", "Version", "BuildNumber", "OSArchitecture"])
    os_caption = (os_info or {}).get("Caption", platform.system())
    os_version = (os_info or {}).get("Version", "")
    os_build = (os_info or {}).get("BuildNumber", "")
    os_arch = (os_info or {}).get("OSArchitecture", platform.machine())

    # ---- CPU ----
    cpu = ps_query("Processor", ["Name", "NumberOfCores",
                                 "NumberOfLogicalProcessors", "MaxClockSpeed"]) or {}
    cpu = cpu if isinstance(cpu, dict) else {}

    # ---- RAM（精确字节数） ----
    cs = ps_query("ComputerSystem", ["TotalPhysicalMemory"]) or {}
    ram_bytes = int(cs.get("TotalPhysicalMemory", 0))

    # ---- Python / Git ----
    python_version = platform.python_version()
    python_path = sys.executable
    git_version = run_cmd(["git", "--version"]) or ""

    # ---- 编译器与后端 ----
    compilers = detect_compilers()
    backend = detect_backend()
    pip_list = run_cmd([sys.executable, "-m", "pip", "list", "--format=freeze"]) or ""
    key_deps = {ln.split("==")[0].lower(): ln.split("==")[1]
                for ln in pip_list.splitlines() if "==" in ln}
    deps_subset = {k: v for k, v in sorted(key_deps.items())
                   if k in ("psutil", "requests", "numpy", "llama-cpp-python")}

    total_ram_gib = round(ram_bytes / 1024 ** 3, 2) if ram_bytes else 0

    data = {
        "os": f"{os_caption} (Build {os_build}, {os_arch})",
        "cpu_model": cpu.get("Name", ""),
        "physical_cores": cpu.get("NumberOfCores", 0),
        "logical_cores": cpu.get("NumberOfLogicalProcessors", 0),
        "cpu_max_clock_mhz": cpu.get("MaxClockSpeed", 0),
        "total_ram_gb": total_ram_gib,
        "total_ram_bytes": ram_bytes,
        "python_version": python_version,
        "python_executable": python_path,
        "git_version": git_version,
        "project_path": str(ROOT),
        "inference_backend": ("llama.cpp (official prebuilt CPU binaries, llama-server)"
                              if backend["llama_cpp_version"] else "未安装"),
        "backend_version": backend["llama_cpp_version"],
        "backend_source": backend["llama_cpp_source"],
        "backend_binaries": sorted(backend["llama_cpp_binaries"].keys()),
        "llama_cpp_python_pip": (backend["llama_cpp_python"].splitlines()[0]
                                 if backend["llama_cpp_python"] else None),
        "compilers": compilers,
        "compiler_available": any(compilers.values()),
        "python_key_dependencies": deps_subset,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
    }

    # ---- 写 JSON ----
    json_path = OUT_DIR / "system_info.json"
    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 写 Markdown ----
    comp_lines = "\n".join(
        f"| {k} | {v or '未找到'} |" for k, v in compilers.items())
    bin_lines = "\n".join(f"- `{b}`" for b in data["backend_binaries"]) or "- 无"
    backend_ver = (data["backend_version"] or "未安装").replace("\n", "；").strip()

    md = f"""# 系统环境信息

> 由 `scripts/collect_system_info.py` 于 {data['collected_at']} 自动采集，全部数据来自本机实际查询（PowerShell CIM / 平台 API），未做任何猜测。重新运行该脚本可刷新。

## 硬件

| 项目 | 值 |
| --- | --- |
| CPU 型号 | {data['cpu_model']} |
| 物理核心数 | {data['physical_cores']} |
| 逻辑线程数 | {data['logical_cores']} |
| CPU 最大时钟 | {data['cpu_max_clock_mhz']} MHz |
| 系统总内存 | {ram_bytes:,} bytes ≈ {total_ram_gib} GiB |

## 操作系统与基础软件

| 项目 | 值 |
| --- | --- |
| 操作系统 | {data['os']} |
| Python | {python_version}（{python_path}） |
| Git | {git_version or '未安装'} |
| 项目路径 | {data['project_path']} |

## 推理后端

- 后端：{data['inference_backend']}
- 版本：`{backend_ver}`
- 来源：{data['backend_source'] or '未记录'}
- 关键可执行文件：
{bin_lines}
- llama-cpp-python（pip）：{data['llama_cpp_python_pip'] or '未安装'}

## 编译器（llama.cpp 使用官方预编译版，编译器非必需）

| 工具 | 状态 |
| --- | --- |
{comp_lines}

## 当前环境关键 Python 依赖

| 包 | 版本 |
| --- | --- |
""" + "\n".join(f"| {k} | {v} |" for k, v in deps_subset.items())

    (OUT_DIR / "system_info.md").write_text(md, encoding="utf-8")

    print(f"[ok] wrote {json_path.name} and system_info.md")
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
