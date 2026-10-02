# 系统环境信息

> 由 `scripts/collect_system_info.py` 于 2026-10-02T22:58:46 自动采集，全部数据来自本机实际查询（PowerShell CIM / 平台 API），未做任何猜测。重新运行该脚本可刷新。

## 硬件

| 项目 | 值 |
| --- | --- |
| CPU 型号 | AMD Ryzen 9 9955HX 16-Core Processor            |
| 物理核心数 | 16 |
| 逻辑线程数 | 32 |
| CPU 最大时钟 | 2500 MHz |
| 系统总内存 | 33,483,378,688 bytes ≈ 31.18 GiB |

## 操作系统与基础软件

| 项目 | 值 |
| --- | --- |
| 操作系统 | Microsoft Windows 11 家庭版 中文版 (Build 26100, 64 位) |
| Python | 3.12.7（F:\LLM Course\Experimental Study on Quantization of Small Models and CPU Inference Performance\.venv\Scripts\python.exe） |
| Git | git version 2.55.0.windows.2 |
| 项目路径 | F:\LLM Course\Experimental Study on Quantization of Small Models and CPU Inference Performance |

## 推理后端

- 后端：llama.cpp (official prebuilt CPU binaries, llama-server)
- 版本：`version: 0.5.0-dev (build 11347, commit 5fc4f3c8c)；built with Clang 20.1.8 for Windows x86_64`
- 来源：backend: llama.cpp
version/build: b11347 (official GitHub Actions prebuilt, Windows CPU x64)
downloaded_at: 2026-10-02
url: https://github.com/ggml-org/llama.cpp/releases/download/b11347/llama-b11347-bin-win-cpu-x64.zip
method: 直接下载官方 release 资产并解压到 tools/llama.cpp/（未编译，未修改）
- 关键可执行文件：
- `llama.cpp\ggml-rpc-server.exe`
- `llama.cpp\llama-batched-bench.exe`
- `llama.cpp\llama-bench.exe`
- `llama.cpp\llama-cli.exe`
- `llama.cpp\llama-completion.exe`
- `llama.cpp\llama-fit-params.exe`
- `llama.cpp\llama-gemma3-cli.exe`
- `llama.cpp\llama-gguf-split.exe`
- `llama.cpp\llama-imatrix.exe`
- `llama.cpp\llama-llava-cli.exe`
- `llama.cpp\llama-minicpmv-cli.exe`
- `llama.cpp\llama-mtmd-cli.exe`
- `llama.cpp\llama-mtmd-debug.exe`
- `llama.cpp\llama-perplexity.exe`
- `llama.cpp\llama-quantize.exe`
- `llama.cpp\llama-qwen2vl-cli.exe`
- `llama.cpp\llama-results.exe`
- `llama.cpp\llama-server.exe`
- `llama.cpp\llama-tokenize.exe`
- `llama.cpp\llama-tts.exe`
- `llama.cpp\llama.exe`
- llama-cpp-python（pip）：未安装

## 编译器（llama.cpp 使用官方预编译版，编译器非必需）

| 工具 | 状态 |
| --- | --- |
| gcc | 未找到 |
| g++ | 未找到 |
| cmake | 未找到 |
| cl | 未找到 |
| make | 未找到 |
| ninja | 未找到 |

## 当前环境关键 Python 依赖

| 包 | 版本 |
| --- | --- |
| psutil | 7.2.2 |