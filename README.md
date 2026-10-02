# 小模型量化与 CPU 推理性能实验

《计算机技术与应用》大模型期末大作业课题14：研究同一个约 0.5B 参数规模的小语言模型（Qwen2.5-0.5B-Instruct）在不同 GGUF 量化版本与 CPU 运行参数下的**文件大小、内存占用、提示处理速度、逐 Token 生成速度、端到端延迟与简单任务质量**。

**当前阶段：Stage 1 —— 实验环境建立 + 最小可运行验证（smoke test）。**
性能数字当前仅用于环境验证，不作为正式实验结果；Q4/Q5/Q8 对照实验尚未开始。

## 项目结构

```text
project_root/
├── environment/        # 环境信息与依赖锁定
│   ├── system_info.json / system_info.md
│   └── requirements-lock.txt
├── models/             # GGUF 模型文件与清单（*.gguf 不入 Git）
│   └── model_manifest.json
├── scripts/            # 采集环境信息、冒烟测试等脚本
├── configs/            # 实验配置（smoke_test.json 等）
├── datasets/
│   ├── dev/            # 开发集（后续阶段）
│   └── test/           # 独立测试集（后续阶段）
├── results/
│   ├── raw/            # 每次运行的原始 JSON 结果
│   └── summary/        # 汇总表（后续阶段）
├── logs/               # 运行日志
├── plots/              # 图表（后续阶段）
├── tools/              # llama.cpp 预编译程序（不入 Git）
└── README.md
```

## Environment Setup

目标环境：Windows 10/11 x64，CPU only，无需 GPU、无需编译器。

### 1. Python 虚拟环境

```bash
# 在项目根目录执行；本机基准解释器为 Anaconda Python 3.12（C:\Anaconda\python.exe）
python -m venv .venv

# Windows (Git Bash / PowerShell)
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

依赖只有 `psutil`（用于读取 llama-server 进程峰值内存）。HTTP 请求使用 Python
标准库 `urllib`，无其他网络依赖。完整锁定版本见
`environment/requirements-lock.txt`。

### 2. llama.cpp 推理后端（官方预编译版，免编译）

本机没有 CMake/MSVC 编译器，因此直接使用 ggml-org 官方发布的 Windows CPU
预编译包：

- **版本**：`b11347`（llama.cpp GitHub Actions 官方构建）
- **下载地址**：
  `https://github.com/ggml-org/llama.cpp/releases/download/b11347/llama-b11347-bin-win-cpu-x64.zip`
- **安装**：解压到 `tools/`，保持目录名 `tools/llama.cpp/`（解压得到的
  `bin/` 内含 `llama-server.exe`、`llama-cli.exe`、`llama-bench.exe` 等）
- 精确版本串以后端自报为准，见 `environment/system_info.json` 的
  `backend_version` 字段，以及 `tools/llama.cpp/DOWNLOAD_SOURCE.txt`。

脚本通过 `tools/llama.cpp/**/llama-server.exe` 自动定位可执行文件，无需加入 PATH。

### 3. 运行条件约定（全阶段有效）

- 仅 CPU 推理：服务启动固定 `-ngl 0`（GPU offload 层数为 0）
- 单进程：同一时刻只有一个 llama-server 推理进程，测试脚本仅为其驱动/观测端
- 冒烟测试固定参数：`threads=16`（= 物理核心数）、`context=512`、
  `max_tokens=64`、`temperature=0`（贪心解码）、`seed=42`

## Model

- **基座模型**：`Qwen2.5-0.5B-Instruct`（官方 GGUF 仓库）
- **当前使用**：`models/qwen2.5-0.5b-instruct-q4_k_m.gguf`，量化 `Q4_K_M`
- **大小**：491,400,032 bytes ≈ 468.64 MiB
- **SHA256**：`74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`
  （与官方仓库 LFS 元数据逐字节比对一致）
- **来源**：<https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF>
  （直连超时，实际经公开镜像 `hf-mirror.com` 下载同一文件，已在 manifest 中注明）
- **许可**：Apache-2.0
- **存放位置**：`models/*.gguf`（已被 `.gitignore` 排除，不入 Git）

完整元数据（含下载日期与校验方式）见 `models/model_manifest.json`。
本阶段仅下载 Q4_K_M 一个模型；后续阶段再按同一流程添加并记录 Q5/Q8。

## Smoke Test

冒烟测试只验证链路可用性：**模型加载 → CPU 推理 → 文本输出 → 正常退出 →
基础计时**，不评测任务质量、不做性能结论。

```bash
.venv/Scripts/python.exe scripts/smoke_test.py
```

脚本会：

1. 读取 `configs/smoke_test.json` 与 `models/model_manifest.json`；
2. 启动 llama-server（`-ngl 0`、`-c 512`、`-t 16`，端口自动分配）；
3. 轮询 `/health` 计时首次加载时间（含服务启动与内置 warmup）；
4. 用 `POST /v1/chat/completions` 发送一条固定数学题
   （“请只回答一个数字：2 + 3 = ?”，temperature=0）；
5. 优雅关闭服务进程。

### 正常运行应产生/更新的文件

| 文件 | 内容 |
| --- | --- |
| `results/raw/smoke_test.json` | 本次运行完整结果：输出文本、token 数、加载时间、端到端耗时、生成速度（含服务端 timings 原始值）、进程峰值内存、`success` 字段 |
| `logs/smoke_test.log` | 全过程日志（含 llama-server 原始输出与启动命令，失败时含完整报错） |

### 通过标准

`results/raw/smoke_test.json` 中：`success == true`、输出文本非空、
`output_contains_expected == true`（2+3 应答 “5”）、服务正常退出。
失败时脚本同样会写入结果文件与完整日志，并返回非零退出码——请保留现场排查，
不要删除日志重跑掩盖问题。

### 复现说明（另一台机器）

1. 安装 Python ≥3.10，按上文创建 `.venv` 并 `pip install -r requirements.txt`；
2. 按 “Environment Setup → 2” 下载并解压 llama.cpp 到 `tools/llama.cpp/`；
3. 按 “Model” 一节下载模型放入 `models/`，并用
   `models/model_manifest.json` 中的 SHA256 校验文件完整性；
4. 运行 `python scripts/collect_system_info.py` 重新生成本机环境记录；
5. 运行冒烟测试。速度类数字因机器而异属正常，不影响“环境可用”的判定。

## 记录约定

- 所有检测结果均来自本机实际查询（PowerShell CIM / psutil / 后端自报版本）；
- 修改 `configs/` 中任何参数前，旧的 `results/raw/` 结果不覆盖删除；
- 阶段性状态与结论记录在各阶段结果 JSON 与本 README 中。
