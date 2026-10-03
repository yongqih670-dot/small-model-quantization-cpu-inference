# Stage 3 正式实验计划（STAGE3_PLAN v1，正式结果产生前冻结）

机器可读版本：`configs/stage3_plan.json`（plan_id `stage3_plan_v1`，
其 SHA256 纳入 `datasets/frozen_hashes_stage3.json`）。本计划冻结后，
如因工具实际限制调整实施细节，须在正式结果产生前于
`docs/stage3_corrections.md` 记录原因，且不扩大到线程/上下文扫描。

## 范围与不变量

- 只比较 **Q4_K_M / Q5_K_M / Q8_0**（同一官方仓库 revision `9217f5db79a29953eb74d5343926648285ec7e67`）。
- 后端保持 llama.cpp b11347 / commit `5fc4f3c8c`，不升级。
- 不执行线程扫描、上下文扫描、自行量化、浮点基线。
- 正式公共配置 `formal_stage3_t4_c1024_v1`：CPU-only、单进程、单 slot
  （`--parallel 1`）、串行请求；`-t 4 -tb 4 -c 1024 -b 2048 -ub 512
  -ctk f16 -ctv f16 -fa off -lm mmap -ngl 0 --cache-reuse 0`；请求体
  `cache_prompt=false`（每次启动后以两次同提示请求核实 `cache_n=0`）；
  temperature=0、top_p=1、seed=42、max_tokens=96 统一；模型内嵌聊天模板
  （三版本 `/props.chat_template` 哈希一致性核对）。bench 与 server 两条
  路径的 Flash Attention 统一为 **off**（Stage 2 曾分别为 auto/off，
  本次统一并记录）。

## 矩阵 A：固定工作量性能实验（llama-bench）

- 工作负载：pp256、tg64、pg256+64，depth=0；单次调用同时产出三个测试条目。
- 5 轮 × 3 模型 = **15 次 bench 进程启动**（内部 repetitions=1），
  合计 **45 个工作负载测量值**；每轮各模型顺序由种子 20261003 预生成
  （见 stage3_plan.json `round_orders`），执行脚本只校验不重排。
- 保留 bench 默认 warmup、全部原始输出与逐重复 samples。
- **pg 是组合工作负载指标，不称生成速度。**

## 矩阵 B：固定真实请求性能实验

- 任务固定顺序：dev-001, dev-003, dev-004, dev-007, dev-009（冻结开发集）。
- 3 模型 × 5 轮 × 5 题 = **75 条计时请求**；每模型每轮独立启动服务，
  预热 1 条 + 缓存检查 2 条另计，不混入计时总数。
- 记录：`server_startup_to_ready_s`、`backend_model_init_interval_s`
  （不称纯权重加载）、进程生命周期峰值内存与采样峰值（间隔 0.05s）、
  逐请求端到端与后端 timings。
- 各模型输出长度可能不同 → 这是实际任务响应比较；相同计算量的吞吐结论
  以矩阵 A 为依据。**本矩阵输出不作为质量样本。**

## 矩阵 C：正式质量评测

- 三模型分别**各回答一次**冻结的 50 题（`test_tasks_v2`，文件顺序即题目
  顺序），共 **150 条评测请求**；同一配置、模板、评分器。
- 不重试；失败/超时/截断计入对应题型分母；评分三档（格式/内容/联合）
  分开报告；未完成整套 50 题不得宣布完整正式正确率。
- 中断续跑：`--resume <run_id>`，已完成 task_id 不重复执行。
- **这是唯一一组正式质量评测**；矩阵 B 的输出不计入质量统计。

## 失败处理与资源规则（预先确定）

- 请求超时 ≠ 服务端已停止：超时后 `GET /slots` 等待空闲（≤60s）；
  未空闲则终止本程序启动的服务，剩余任务标 `not_attempted`，不继续发请求。
- 不静默重跑失败样本；确需补测用新 run_id 并记录原因与关联。
- 正式采集前与每轮记录只读资源快照；CPU 总占用低≠完全无干扰；
  **不看完速度再挑选"好数据"**。
- 正式模式门禁：preflight 全部通过（冻结哈希、模型 SHA256、后端版本）
  才允许 `--formal`；哈希不匹配在启动模型前拒绝。

## 汇总方法（预注册）

- 性能：5 轮报均值、样本标准差（ddof=1）与原始散点；
  不同题目的输出差异不当作同一负载的运行波动。
- 质量：格式/内容/联合的"答对数/50"，附 Wilson 95% 置信区间与
  逐题三模型对照；50 题差一题=2 个百分点，不据此宣称稳定优势。
- 文件大小用 manifest 实际字节数（GGUF 文件），不用 bench 的
  `model_size`/tensor 统计值。
- Q8_0 仍是量化模型；结论限定本机、本模型、本配置、本测试集。
