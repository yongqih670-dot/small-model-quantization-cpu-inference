# Stage 5 审查说明：stage4_exec.py --resume 的限制与交付口径

本文件是 Stage 5 审查产物，如实记录对 Stage 4 恢复机制的代码审查结论。
Stage 4 被冻结代码保持原样（不为其"伪装当时已修复"而改动）。

## 一、现存恢复证据支持什么、不支持什么

`run_20261003_121218_stage4bench_a2dc0f` 的恢复记录（2026-10-03 12:15）
仅支持以下事实：

- 指纹核对通过（**恢复时刻的**冻结清单 `frozen_hashes_stage4.json` 与
  当时的 tools/ 与模型文件一致；该清单在恢复前刚因汇总修复而重建，
  见 Stage 4 报告）；
- 30 个 marker 完整的单元被跳过，汇总从落盘记录重建为 90/90。

**不支持**更强的"原始实验期间指纹始终未变"的保证：矩阵 A 的测量发生
在恢复之前，当时没有逐单元的指纹留痕可与其后重建的清单比对。

## 二、审查发现的 stage4_exec.py --resume 具体限制

1. **只核对当前冻结清单**：`verify_fingerprints()` 读取
   `datasets/frozen_hashes_stage4.json` 的**当前内容**，不与该 run 启动时
   写入 run 目录的 `snapshot_frozen_hashes_stage4.json` 比对。若清单在
   运行后曾被重建（本例确实发生过），恢复时核对的是新清单而非原快照。
2. **覆盖运行目录快照**：恢复路径会再次执行 `snapshot_files()`，
   用当前计划/配置/清单**覆盖** run 目录内的 `snapshot_*.json`
   （Stage 4 首次恢复即覆盖了原快照，原始快照没有独立留存）。
3. **重置 started_at**：恢复运行把 `result.json` 的 `started_at_utc`
   更新为恢复时刻，原始开始时间只在被覆盖前存在。
4. 恢复与继续测量耦合：没有"只重建汇总、不追加测量"的独立路径
   （Stage 4 的恢复实际未发新请求，但机制上允许）。

## 三、Stage 5 交付口径

- **交付入口**：`scripts/rebuild_summary.py` —— 纯汇总重建工具：
  - 绑定 run 目录内的不可变快照（`snapshot_frozen_hashes_stage4.json`），
    与**当前**冻结清单比对；不一致即拒绝（不猜、不修补）；
  - 只读原始记录重建 `result_rebuilt.json`（新文件），保留原始
    `started_at_utc`，另记重建时间与说明；**不覆盖**旧 result、快照、
    失败记录、请求记录；**不发起任何推理或测量**；
  - 缺少可信快照（快照文件缺失/损坏）→ 拒绝重建并说明。
- **继续测量的恢复**：`stage4_exec.py --resume` 保留为历史工具，
  按本文件 §二 的限制如实标注；**交付流程不使用该路径追加测量**，
  也不为其扩展复杂恢复系统（无可靠原始快照时明确拒绝）。
- `/slots` 空闲判定：Stage 4 的 `strict_slots_idle` 已拒绝空列表/结构
  异常/解析失败，但 `is_processing` 仅按真值判断——`0`、空字符串、
  `null` 会被误判为空闲。Stage 5 提供布尔严格版
  `rebuild_summary` 同文件的 `slots_idle_boolean_strict`（仅 `False`
  布尔值算空闲），并配离线测试；历史冻结代码不回改。

## 四、资源监控的证据口径（供最终报告引用）

- HTTP 单元：`resource_before` / `resource_after` 均有；
- bench 单元：**只有 `resource_before`，缺结束快照**；
- 所有快照字段为 CPU 采样、空闲内存、全局 llama-server 进程数、时间，
  **不含 CircuitPilot 与 AC 供电字段**；正式运行开始前存在带时间戳的
  交互式检查（当时无 CircuitPilot 进程、电池状态 2=AC），但
  **没有逐单元的独立记录**——最终报告不得写"全程无 CircuitPilot/
  全程 AC"这类确定性陈述，也不以当前检查补作历史证明。
