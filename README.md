# 小模型量化与 CPU 推理性能实验

《计算机技术与应用》大模型期末大作业课题14：研究同一个约 0.5B 参数规模的小语言模型（Qwen2.5-0.5B-Instruct）在不同 GGUF 量化版本与 CPU 运行参数下的**文件大小、内存占用、提示处理速度、逐 Token 生成速度、端到端延迟与简单任务质量**。

**当前阶段：Stage 5 已完成 —— 审查修正、最终报告与可复现交付。**
历史证据保持原样；结论限定本机、Q4_K_M、b11347、固定负载。

**最终报告：[docs/FINAL_REPORT.md](docs/FINAL_REPORT.md)**｜证据索引：[docs/EVIDENCE_INDEX.md](docs/EVIDENCE_INDEX.md)｜答辩讲稿：[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)

- 指标定义与测量边界：[docs/EXPERIMENT_PROTOCOL.md](docs/EXPERIMENT_PROTOCOL.md)
- Stage 1 问题与修正对照：[docs/stage2_corrections.md](docs/stage2_corrections.md)
- 冻结哈希（数据集/模板/评分器/配置）：[datasets/frozen_hashes.json](datasets/frozen_hashes.json)

## 项目结构

```text
project_root/
├── docs/               # 实验协议与阶段修正记录
├── environment/        # 环境信息（v1 历史记录 + v2 修正版）与依赖锁定
│   ├── system_info.json / system_info.md          # Stage 1 历史记录
│   ├── system_info_v2.json / system_info_v2.md    # Stage 2 修正版
│   ├── backend_manifest.json                      # 后端文件级 SHA256 证据
│   └── evidence/                                  # llama-server/bench --help 等
├── models/
│   ├── model_manifest.json                        # Stage 1 历史记录
│   ├── model_manifest_v2.json                     # revision + 官方独立核验状态
│   └── *.gguf                                     # 不入 Git
├── scripts/            # 采集/控制/评分/运行/校验脚本（见下）
├── configs/            # smoke_test_v2 / dev_config / bench_dev
├── datasets/
│   ├── dev/dev_tasks.json          # 10 条开发题（口径调试用）
│   ├── test/test_tasks.json        # 50 条独立正式题（本阶段禁止运行）
│   ├── prompt_template.json        # 固定提示模板（含 system prompt）
│   └── frozen_hashes.json          # 冻结哈希
├── results/
│   ├── raw/smoke_test.json         # Stage 1 历史证据（不改）
│   ├── raw/<run_id>/               # Stage 2 起每次运行独立目录
│   └── summary/                    # 配置级汇总（Stage 3+）
├── logs/smoke_test.log             # Stage 1 历史日志（不改）
├── logs/<run_id>/                  # 每次运行的完整日志（含服务原始输出）
├── tests/              # 离线验证套件与 fixtures（无 pytest 依赖）
├── tools/llama.cpp/    # llama.cpp 预编译程序（不入 Git）
└── README.md
```

## Environment Setup

目标环境：Windows 10/11 x64，CPU only，无需 GPU、无需编译器。

### 1. Python 虚拟环境

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

运行依赖 `psutil`（进程监控）与 `matplotlib`（分析图表）；HTTP 使用标准库 `urllib`。锁定版本见
`environment/requirements-lock.txt`。

### 2. llama.cpp 推理后端（官方预编译版，免编译）

- **版本**：`0.5.0-dev (build 11347, commit 5fc4f3c8c)`，官方 GitHub Actions
  Windows CPU x64 构建
- **下载**：
  `https://github.com/ggml-org/llama.cpp/releases/download/b11347/llama-b11347-bin-win-cpu-x64.zip`
- **安装**：解压到 `tools/llama.cpp/`；zip 与关键 exe/DLL 的 SHA256 见
  `environment/backend_manifest.json`；`--help`/`--list-devices` 证据见
  `environment/evidence/`
- 无 GPU 设备（`--list-devices` = none），全部推理仅 CPU。

### 3. 运行条件约定（Stage 2 起生效，详见协议）

- 单推理进程、**显式 `--parallel 1`**（默认 auto 会开 4 slot）、单请求并发；
- 线程 `-t 4 -tb 4`（固定开发配置，**不是**最终最佳配置）、`-fa off`、
  `-ngl 0`、`--cache-reuse 0`、请求体 `cache_prompt: false`（缓存复用禁用已
  经"同提示两次"验证）；
- 生成参数：temperature=0（贪心）、top_p=1、seed=42、max_tokens 有限；
- 每次真实运行唯一 `run_id`，结果写 `results/raw/<run_id>/`，日志写
  `logs/<run_id>/`，历史运行永不覆盖。

## Model

- **基座模型**：`Qwen2.5-0.5B-Instruct`（官方 GGUF 仓库，revision
  `9217f5db79a29953eb74d5343926648285ec7e67`）
- **当前使用**：`models/qwen2.5-0.5b-instruct-q4_k_m.gguf`，量化 `Q4_K_M`
- **大小**：491,400,032 bytes ≈ 468.64 MiB；**SHA256**
  `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`
  —— 已完成**官方独立核验**（与 huggingface.co 官方 API LFS 元数据逐字节一致，
  见 `models/model_manifest_v2.json`）
- **来源**：<https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF>（下载经
  hf-mirror.com，URL 与核验 URL 在 manifest 中分开记录）；许可 Apache-2.0
- 三个模型（Q4_K_M/Q5_K_M/Q8_0）均已下载并通过官方独立核验，
  明细见 `models/model_manifest_v3.json`；`models/*.gguf` 不入 Git。

## Smoke Test（修正版 v2）

```bash
.venv/Scripts/python.exe scripts/smoke_test.py
```

与 Stage 1 的区别（详见 `docs/stage2_corrections.md`）：链路成功 / 答案正确
（`output.strip()=="5"`）/ 清理成功三态分离；`server_startup_to_ready_s` 与
后端日志统计的 `model_load_time_s` 分列；速度仅取后端 timings（缺失即 null，
不编造）；显式并发与缓存控制并验证；finally 清理，terminate 如实记录为主动
强制终止。**通过标准**（预注册）：`link_success ∧ answer_correct ∧
cleanup_success ∧ cache_reuse_check.passed ∧ performance.performance_record_valid`。
最近一次结果：`results/raw/run_20261003_001306_smoke_v2_cecfb7/result.json`
（success=true）。

## 两条测试路径

| 路径 | 工具 | 说明 |
| --- | --- | --- |
| 性能路径 | `scripts/run_bench.py` + 同版本 `llama-bench` | 固定 token 工作负载（pp/tg/pg/深度 d），`-r 3` 重复，`-o json` 保留 `samples_*` 逐重复原始值。**它是批处理微基准，与 HTTP 端到端延迟不是同一种指标**，两者不混用 |
| 任务路径 | `scripts/run_tasks.py` + `llama-server` | 真实提示逐条执行并评分；记录 task_id/run_id/config_id/输入/原始输出/usage/timings/finish_reason/端到端时间/评分/错误；无重试，截断与失败如实计入分母 |

```bash
# 任务路径（开发集；正式集被脚本硬拒绝，见下）
.venv/Scripts/python.exe scripts/run_tasks.py --dataset datasets/dev/dev_tasks.json --run-label dev10
# 性能路径（采集链路验证配置）
.venv/Scripts/python.exe scripts/run_bench.py --run-label benchdev
```

## 数据集与评分

- `datasets/dev/dev_tasks.json`：10 条开发题（单字段 3 / JSON 3 / 分类 2 /
  规范化 2），**仅用于提示与评分口径调试**；
- `datasets/test/test_tasks.json`：50 条独立正式题（单字段 15 / JSON 15 /
  分类 10 / 规范化 10），含干扰项、缺失字段与边界情况；**Stage 2 未运行**，
  `run_tasks.py` 对 `datasets/test/` 路径硬拒绝（退出码 2）：未加
  `--formal` 一律拒绝；加 `--formal` 后还需 preflight 全部通过才解锁；
- 评分三档：格式 / 内容 / 联合正确，分母=全部请求；JSON 解析规则、引号类
  标点对称剥离、多余字段、围栏与额外说明的处理均预注册于
  `scripts/score_answer.py` 文档头与协议 §4；
- 数据集、模板、评分器与配置的 SHA256 已冻结（`datasets/frozen_hashes.json`）；
  冻结后变更须说明理由并重新冻结（变更史见 `docs/stage2_corrections.md` §三）。

## 验证与测试

```bash
.venv/Scripts/python.exe tests/run_tests.py       # 离线套件（不调用模型）
.venv/Scripts/python.exe scripts/validate_datasets.py
.venv/Scripts/python.exe scripts/collect_env_evidence.py   # 刷新环境证据
.venv/Scripts/python.exe scripts/probe_server.py  # 服务行为探针
```

离线套件覆盖：错误答案（含含"5"的错误串）不判对、缺失 timings 不伪造速度、
损坏输入留结构化失败记录、重复运行不覆盖历史证据（Stage 1 证据哈希持续校验）、
评分三档口径、失败与清理路径无残留服务进程、`results/` 无合成记录。

## 当前状态与下一步

- ✅ Stage 1：环境 + 冒烟（提交 `6484457`）
- ✅ Stage 2：测量程序修正、协议与数据集冻结、Q4 开发验证（`c5d2c02`）
- ✅ Stage 3：Q5/Q8 下载与官方独立核验、预注册矩阵（`configs/stage3_plan.json`）、
  正式对照三矩阵执行与汇总（`results/summary/stage3_summary.json`、`plots/`）
- ✅ Stage 4：分析修订（`docs/stage3_analysis_revision.md`、`stage3rev_*`）+
  线程实验（T∈{1,2,4,8,16,32}×5轮：90 个 bench 测量 + 150 条计时请求）
- ✅ Stage 5：离线审计（40 项核验全通过）、最终报告、演示材料、交付打包
  （`delivery/`，排除大文件，SHA256 清单见 `delivery/SHA256SUMS.txt`）

## 命令一览（⚠ = 会实际启动模型推理）

| 命令 | 用途 | 启动推理 |
| --- | --- | --- |
| `.venv/Scripts/python.exe scripts/stage4_preflight.py` | Stage 4 深度核验（指纹/实时版本/硬件） | 否 |
| `.venv/Scripts/python.exe scripts/audit_stage_results.py` | 离线审计：重算全部报告数字 | 否 |
| `.venv/Scripts/python.exe scripts/validate_datasets.py` | 数据集与渲染一致性校验 | 否 |
| `.venv/Scripts/python.exe scripts/rebuild_summary.py --run-id <id> --matrix bench\|http` | 纯汇总重建（快照绑定，拒绝不匹配） | 否 |
| `.venv/Scripts/python.exe tests/run_tests.py` | 离线验收套件 | 否 |
| `.venv/Scripts/python.exe scripts/freeze_hashes_stage4.py` | 重建 Stage 4 冻结清单 | 否 |
| `.venv/Scripts/python.exe scripts/smoke_test.py` | 冒烟测试 | **是** |
| `.venv/Scripts/python.exe scripts/run_tasks.py --dataset datasets/dev/dev_tasks.json --run-label dev` | 开发集任务路径 | **是** |
| `.venv/Scripts/python.exe scripts/run_tasks.py --dataset datasets/test/test_tasks.json --config configs/formal_config.json --formal --model-key Q4_K_M` | 正式 50 题 | **是** |
| `.venv/Scripts/python.exe scripts/run_stage3_bench.py` / `run_stage3_requestperf.py` | Stage 3 两矩阵 | **是** |
| `.venv/Scripts/python.exe scripts/stage4_exec.py --matrix bench\|http` | Stage 4 线程矩阵 | **是** |
| `.venv/Scripts/python.exe scripts/analyze_stage3_revision.py` | Stage 3 修订分析（显式 run_id） | 否 |
| `.venv/Scripts/python.exe scripts/analyze_stage4.py --run-bench <id> --run-http <id>` | Stage 4 分析（显式 run_id） | 否 |

## Stage 4 线程实验速览（Q4_K_M, 统一线程预算）

- pp256：T=32 最佳（1550.6 tok/s，T=1 的 7.57 倍）；tg64：T=8 最佳
  （121.1 tok/s，1.87 倍），T=16/32 反而下降（1.84/1.33）；组合 pg256+64
  依 3% 实用规则推荐 T=8；固定任务组合延迟最佳 T=16（0.249s，T=8 为 0.264s）
- 进程峰值内存随线程档缓增：518→535 MiB；各档输出逐字一致（贪心解码）
- 本机实用建议：**单档推荐 T=8**（生成最优、组合 3% 内、延迟仅差 15ms、
  内存更低）；追求最低端到端延迟选 T=16；纯提示处理场景可更高
- 详见 `results/summary/stage4_summary.json`、`plots/stage4_*.png`

## Stage 3 正式对照速览

- 模型：Q4_K_M / Q5_K_M / Q8_0（同 revision `9217f5db`，SHA256 官方独立核验通过）
- 配置：`formal_stage3_t4_c1024_v1`（t=4/tb=4, ctx=1024, b=2048/ub=512, KV f16,
  fa=off, lm=mmap, parallel=1, cache_prompt=false, temp=0, seed=42, max_tokens=96）
- 矩阵 A（llama-bench，5轮×3模型，pp256/tg64/pg256+64）：
  tg 生成速度 Q4 105.4±0.7 > Q5 99.7±0.9 > Q8 82.3±0.9 tok/s；
  pp 提示处理 Q8 774±27 ≈ Q4 734±23 > Q5 440±9 tok/s
- 矩阵 B（75 条真实计时请求）：端到端 Q4 0.359s ≈ Q8 0.385s < Q5 0.503s
- 矩阵 C（正式质量 3×50 题，唯一正式质量组）：
  联合正确 Q4 16/50、Q5 17/50、Q8 15/50——Wilson 95% CI 重叠，无稳定优势；
  三模型同错 31 题（当前任务/提示/评分下共同失败较多，各因素贡献无法隔离），分歧仅 6 题
- 详见 `docs/stage3_corrections.md`、`results/summary/stage3_summary.json`
