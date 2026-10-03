# 证据索引（Evidence Index）

每张核心表/图对应的 run_id、原始文件、分析脚本与统计口径。
所有分析脚本显式绑定 run_id，禁止自动选最新目录。

## Stage 3（量化对照）

| 产物 | run_id / 原始文件 | 分析脚本 | 统计口径 |
| --- | --- | --- | --- |
| 质量三档统计（§6.2 表） | `run_20261003_112320_formal_Q4_K_M_7eda40`、`run_20261003_112342_formal_Q5_K_M_8b86de`、`run_20261003_112411_formal_Q8_0_37181d` 的 `requests.jsonl` | `scripts/audit_stage_results.py`（重打分核对）+ `scripts/score_answer.py`（冻结评分器） | 3 模型 × 50 题，分母含全部请求；Wilson 95% CI |
| McNemar 配对表 | 同上 | `scripts/analyze_stage3_revision.py` | 联合正确口径，双侧精确检验，事后探索性 |
| `plots/stage3rev_latency.png` | `run_20261003_112135_stage3perfB_0ffb9d/requests_perf.jsonl` | `scripts/analyze_stage3_revision.py` | 每轮五题平均→5 轮均值±样本标准差（每模型 25 条请求/5 轮） |
| `plots/stage3rev_memory.png` | 同上 run 的 `startup_*.json`（每模型 5 个独立进程） | 同上 | 生命周期 peak_wset，均值线+散点 |
| 量化吞吐（§6.1 表） | `run_20261003_112023_stage3benchA_a9c6c1`（15 次调用/45 测量） | `scripts/audit_stage_results.py` | 5 轮均值±样本标准差；pg 为组合指标 |
| `plots/stage3_bench_pp_tg_pg.png`、`stage3_e2e_latency.png`（原图保留） | 同上 | `scripts/analyze_stage3.py` | Stage 3 原口径（延迟图 n=75 口径已被 stage3rev 修订版取代） |

## Stage 4（线程实验，Q4_K_M）

| 产物 | run_id / 原始文件 | 分析脚本 | 统计口径 |
| --- | --- | --- | --- |
| 吞吐分档表（§7.1） | `run_20261003_121218_stage4bench_a2dc0f`（30 单元/90 测量；汇总重建 `result_rebuilt.json`） | `scripts/analyze_stage4.py --run-bench <id>` | 6 档×5 轮；均值±样本标准差+CV；加速比基准=本阶段 T=1 |
| `plots/stage4_throughput_vs_threads.png`、`stage4_speedup.png` | 同上 | 同上 | 误差条=5 轮样本标准差 |
| `plots/stage4_latency_vs_threads.png` | `run_20261003_121547_stage4http_f03b1a/requests_threads.jsonl`（150 计时+90 辅助） | 同上 | 每轮五题平均→5 轮统计；贪心输出逐档一致 |
| `plots/stage4_memory_startup.png` | 同上 run 的 `unit_*.json`（每档 5 个独立进程） | 同上 | 生命周期 peak_wset；启动到就绪 |

## 审计与重建

- 离线审计（40 项核验，含重算与报告引用值核对）：
  `scripts/audit_stage_results.py` → `results/summary/audit_stage5/audit_results.json`
- 案例原始输出：`results/summary/audit_stage5/case_outputs.json`
- 汇总重建（只读、不测量）：`scripts/rebuild_summary.py` → 各 run 的
  `result_rebuilt.json`
