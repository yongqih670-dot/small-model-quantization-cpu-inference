# 实验协议（EXPERIMENT_PROTOCOL v1，Stage 2 冻结）

本文档规定本课题的指标定义、测量边界、评分口径与数据处理规则。
**冻结内容**：数据集、提示模板、评分器与开发配置的 SHA256 见
`datasets/frozen_hashes.json`；冻结后修改任何被冻结文件必须说明理由、
重新冻结并在 `docs/stage2_corrections.md` 记录变更。

---

## 1. 指标定义：边界、单位与数据来源

| 指标 | 定义（开始→结束边界） | 单位 | 来源 |
| --- | --- | --- | --- |
| `server_startup_to_ready_s` | 客户端 `Popen()` 返回 → 首次 `/health` 返回 200 且 `status=ok`。含进程拉起、服务初始化、模型加载、后端内置 warmup；**不含**任何清理、结果写盘或退出耗时 | 秒 | 客户端 `time.perf_counter()` |
| `model_load_time_s` | 服务端日志时间戳差值：`load_model: loading model` → `llama_server: model loaded`。**仅取后端日志自报统计**；日志缺失/格式异常时为 `null` 并记录原因，**不得用启动就绪时间替代**，也不称为"磁盘冷启动"（未做冷/热缓存区分） | 秒 | 服务端日志（格式 `M.SS.mmm.uuu`，进程启动为原点） |
| 预热（warmup） | 服务端内置 warmup（默认开启，日志行 `warming up the model with an empty run`）+ 每次运行首条**显式标记 `warmup`** 的请求；预热不计入任何统计 | — | 请求记录 `marked_as=warmup` |
| `end_to_end_client_s`（请求级） | 客户端发出 HTTP 请求 → 收到完整响应。含聊天模板组装、推理、JSON 序列化；不含服务启动与清理 | 秒 | 客户端计时 |
| 提示处理速度 | `timings.prompt_per_second`（后端自报；分母为 `prompt_n`） | tok/s | 服务端 `timings` |
| 生成速度 | `timings.predicted_per_second`（后端自报；分母为 `predicted_n`）。**禁止**用 `completion_tokens / end_to_end_s` 或 `predicted_ms` 猜算替代 | tok/s | 服务端 `timings` |
| `usage` / `timings` 原始值 | 完整保存、不取整；展示层才四舍五入 | — | 服务端响应 |
| `peak_wset_mib` | llama-server 进程**生命周期**峰值工作集（Windows `peak_wset`）。**不能**当作单个请求的峰值 | MiB | psutil（进程级） |
| `sampled_peak_rss_mib` | 定时采样 RSS 的采样峰值，采样间隔记录于 `memory.sample_interval_s`（当前 0.05 s） | MiB | psutil 采样线程 |
| llama-bench `pp/tg/pg` | `-p`=每次重复提示 token 数；`-n`=每次重复生成 token 数；`-pg pp,tg`=先处理 pp 再生成 tg 的组合；`-d`=测量前预填充的 KV 上下文深度（默认 0）。b11347 **无 `-c` 参数**（上下文隐式=d+p+n），线程为单一 `-t` | tok/s | llama-bench `-o json`（含 `samples_ns/samples_ts` 逐重复原始值） |

**llama-bench 与任务路径不可比**：llama-bench 是同进程批处理微基准（无 HTTP、
无模板组装、无每请求调度），其 pp/tg 吞吐**不是**HTTP 请求端到端延迟，
两者不混用、不互推，图表中分开呈现。

## 2. 运行结构与三层结果关系

- **请求级**（`results/raw/<run_id>/requests.jsonl` 每行一条）：一次 HTTP
  推理请求的完整记录——task_id、run_id、config_id、输入、原始输出、
  usage_raw、timings_raw、finish_reason、end_to_end_client_s、评分、错误。
- **进程级**（run 目录内）：服务启动、加载、内存、清理（`cleanup`、
  `server_startup_to_ready_s`、`memory`）。同一 run 的请求共享一个进程。
- **配置级汇总**（`results/summary/`，后续阶段生成）：对同 config_id 的多次
  run 做聚合（均值±标准差）。聚合只能建立在请求级/进程级原始记录之上，
  不允许只存汇总丢原始值。

## 3. 运行条件（固定）

- 推理后端：llama.cpp 官方预编译 CPU 构建（`environment/backend_manifest.json`）；
  **无 GPU**（win-cpu-x64 构建 + `-ngl 0` + `--list-devices` 为 none 三重证据）。
- **单推理进程、单 slot、单请求并发**：显式 `--parallel 1`（默认 -1=auto 曾给出
  4 slot，必须显式覆盖），生效证据取服务端日志 `n_slots = 1` 行。
- 线程：生成 `-t N`、批处理/提示 `-tb M`（当前开发配置 N=M=4，**不是**最终最佳
  配置）；`-fa off`（显式）、`--cache-reuse 0`、请求体 `cache_prompt: false`。
- 缓存复用禁用验证：同一提示连续两次，两次 `cache_n=0` 且第二次 `prompt_n`
  与第一次相同且 >1（完整重算）；验证记录存于冒烟结果 `cache_reuse_check`。
- 采样温度 0（贪心）+ 固定 seed；`max_tokens` 有限；聊天模板使用 GGUF 内嵌的
  Qwen ChatML（`/props.chat_template` 证据），system prompt 固定于提示模板文件。

## 4. 评分口径（冻结于 `scripts/score_answer.py`）

三个独立布尔量，分母均为**全部请求**（失败/超时/截断/格式错误都计入，无剔除）：

- `format_correct`（格式）
- `content_correct`（内容）
- `correct` = 格式 ∧ 内容（联合）

| 任务类型 | content_correct | format_correct |
| --- | --- | --- |
| exact（单字段抽取/规范化） | `normalize(output) == normalize(gold)`；normalize=去首尾空白+去结尾句读 | `output.strip() == gold.strip()`（"5。"内容对但格式错） |
| label（分类） | 同上 | `output.strip() ∈ labels` |
| json（多字段抽取） | 所有 gold 字段存在、类型一致、值相等（数值按值比 5==5.0，bool 不混同数字；字符串值**对称剥离成对外层引号/书名号**后精确比较——对应模板"字段值使用输入原文表述"） | 直接解析为 JSON 对象（无围栏、无额外说明）**且无多余字段** |

JSON 解析规则（提取载荷，不是修复）：① 直接 `json.loads`；② 围栏内容；③
首个 `{` 到最后一个 `}` 子串；④ 不可解析 → 该条判错并保留原文。**不做语法
修复、不重试、不强制 grammar。** 截断（`finish_reason=="length"`）标记
`truncated_output` 并照常计分（截断答案按其已生成内容判）。

## 5. 失败、超时与缺失指标处理

- 请求超时（`request_timeout_s`，当前 120s）：记录 `status=timeout`，计分全错，
  **计入分母**。
- HTTP 失败 / 服务提前退出：结构化失败记录（`results/raw/<run_id>/result.json`
  含 `error`、`failure_stage`）+ 完整日志；已完成的请求记录不丢失（逐条落盘）。
- timings 缺失：速度字段为 `null` + `missing_reason` +
  `performance_record_valid=false`；该 run 总体 `success=false`；**不编造兜底值**。
- 预热与正式计时分开标记（`marked_as=warmup` / 正式请求）。
- **无重试**：任何请求失败不自动重发。

## 6. 正式性能实验规则（Stage 3+ 适用）

- 重复测量：每组配置 ≥3 次重复，报告**均值与标准差**，同时保留
  逐重复原始值（llama-bench 用 `samples_*`，HTTP 路径用请求记录）。
- **不因结果不符合预期删除有效样本**；剔除只能依据预先注册的规则（如超时
  上限），且剔除清单必须公开。
- 对照实验每次只变一个因子（量化版本 → 线程数 → 上下文长度），其余固定。
- 结论限定在"本机、本模型、本配置、本测试集"范围内；Q8 仍是量化模型，
  不可作为未量化基准，不可声称量化完全无损。

## 7. 证据与核验

- 模型：`models/model_manifest.json`（Stage 1 历史记录）+
  `models/model_manifest_v2.json`（revision、核验 URL 分列、官方独立核验状态）。
- 后端：`environment/backend_manifest.json`（zip 与关键 exe/DLL 的 SHA256、
  `--list-devices` 输出、帮助文本证据）。
- 真实推理记录带 `is_real_inference: true, synthetic: false`；**合成响应只允许
  出现在 `tests/` 中**且不得进入 `results/`（离线测试 H 项验证该不变量）。
- 每个真实运行：唯一 `run_id`（时间戳+随机后缀）+ 独立目录，历史 run 永不覆盖。
