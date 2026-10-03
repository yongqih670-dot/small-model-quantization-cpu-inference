# Stage 5 收尾修订记录（Closeout Revision v1）

本轮为交付前的收尾修订，**未启动任何推理**（未运行 llama-server /
llama-bench / 任何真实推理请求；未运行含真实集成测试的
`tests/run_tests.py` 原入口；仅运行重构后的离线套件 `tests/run_offline.py`
与离线审计）。逐项修订如下。

## 一、测试入口拆分（修正"默认入口含真实集成测试"）

- 事实记录：**Stage 5 此前运行的 `tests/run_tests.py` 全套件包含真实
  集成测试**——E2 启动了 llama-server 并对开发集 fixture（fix-001）
  发起了一条真实模型请求；F2 启动了真实服务并发送 404 请求；
  E/F 还向 `results/raw/` 写入了 testcorrupt/testbadmodel 失败运行
  目录。这些是真实执行记录，**保留不删**（`results/raw/run_*_testcorrupt_*`、
  `run_*_testbadmodel_*` 已入库）；区分口径："未新增正式实验"成立
  （未触碰正式测试集与正式矩阵），"未调用模型"**不成立**（历史集成
  测试调用过）。
- 修正：测试入口拆分——`tests/run_offline.py`（默认交付验收入口）
  只运行纯离线段（评分器/性能解析/日志解析/组合规则/哈希校验/渲染
  一致性/slots 语义/McNemar 已知值/指纹正检/S4-S5-S6 反例），不启动
  后端、不访问推理服务、不向正式 `results/raw` 写入；
  `tests/run_tests.py --integration` 为显式 opt-in 的集成入口
  （E/E2/F/F2 移入 `run_integration_suite()`），本轮未执行。
- 当前离线套件：**111 项断言全部通过**（本轮仅运行该入口）。

## 二、审计 v2（`scripts/audit_stage_results_v2.py` → `results/summary/audit_stage5_v2/`）

- **完整组合集合比对**（替代仅数总数）：S3_A 45 键（模型×轮×负载）、
  S3_B 75 键（模型×轮×题）、S3_C 每模型恰等于数据集 50 个 task_id、
  S4_A 90 键（档×轮×负载）、S4_B 150 键（档×轮×题）——缺失/多余/重复
  逐一列出；v1 审计（40 项）保留为历史；
- 配置非空且等于预期（config_id、temperature/seed/max_tokens、
  bench argv 的 -t/-fa/-ngl 与档位一致）；失败、截断、
  `performance_record_valid`、单元三态逐条检查；
- **评分器/数据集哈希门控**：先比对 `frozen_hashes_stage3.json` 中
  score_answer.py 与三个数据文件的哈希，一致后才允许"冻结评分器重打分"
  表述（本轮核验一致，150 条重打分 0 不一致）；
- **报告数值表** `report_tables.json`：最终报告的表格数值由本审计从
  原始记录生成/核对——自动核对范围以此文件为界，报告其余文字数字
  由人工依据审计结果撰写，**不再声称"10 项参考值等于全文逐项核对"**；
- 反例测试（S6，临时目录）：组合集合的缺失/多余/重复键均被检出，
  完全集判定为 complete；
- 审计过程中发现并修正了 v2 脚本自身的一处变量复用 bug（S4_A config
  检查误用 S3_A 的结果对象）；修正后 **31/31 通过**。

## 三、重建工具修正（`scripts/rebuild_summary.py` 重写）

- 措辞更正：快照绑定检查改称**"现存快照一致性检查"**——只证明现存
  快照与当前清单一致，**不构成对原始启动快照可信性的证明**；明确
  Stage 4 bench 运行的快照是恢复时覆盖后的版本；
- 完整性**不按文件名认定**：非 partial 单元必须三态全真才计为完整，
  否则列入 `units_flagged_incomplete`；partial 文件单独列出；
- 缺失指标不补 0（bench 同时报告字段合计与实际有效条目并交叉核验）；
- 辅助请求数改称**推算值**（`aux_requests_total_derived`，3×完整单元数），
  不标为实测；
- 交叉核验：run_id 归属、matrix 一致、unit_key 重复键、请求记录归属；
- 输出明确的 `completeness_status`（complete/incomplete/unverifiable）
  与原因，不泛称"完整性通过"；
- 两个 Stage 4 run 的旧 `result_rebuilt.json` 归档为
  `result_rebuilt_v1.json` 后用新工具重新生成（status 均为 complete，
  且 started_at 附带"恢复时重写"的说明字段）。

## 四、交付文字统一

- README：移除"0.5B 基座能力上限"等撤回结论 → "当前任务/提示/评分下
  共同失败较多，各因素贡献无法隔离"；
- FINAL_REPORT：llama-bench 措辞改为"**本次命令未显式传入 -lm**"；
  "未提供"误触发案例移除 test-001（该题属职称后缀错误，已在 §8.2
  单列）；`ctx=1024` 限定为 server/HTTP 正式矩阵口径（bench 上下文
  隐式 d+p+n=320）；"生成分辨率"笔误改为"生成吞吐"；
- DEMO_SCRIPT：McNemar 不一致对更正为 5/3/4 个；补机制解释为待验证
  假设的表述；补资源快照覆盖范围说明（bench 无结束快照）；
- CircuitPilot/供电历史检查：检查输出未归档入交付包，**无法由交付
  内容独立核验**——报告与讲稿均按此口径，不补造历史记录。

## 五、本轮实际执行的命令

```bash
.venv/Scripts/python.exe tests/run_offline.py            # 111 项断言全绿（无后端）
.venv/Scripts/python.exe scripts/audit_stage_results_v2.py   # 31/31 通过
.venv/Scripts/python.exe scripts/rebuild_summary.py --run-id <S4_A> --matrix bench
.venv/Scripts/python.exe scripts/rebuild_summary.py --run-id <S4_B> --matrix http
.venv/Scripts/python.exe scripts/validate_datasets.py    # 数据集校验通过
# 随后：交付包 rev2 生成与核验（见下）
```

未运行：`tests/run_tests.py`（含集成段）、任何推理入口。

## 六、rev3 修补追加（最小修补轮）

- **rebuild_summary 完整性判定严格化**：单元集合必须恰为 6 档×5 轮
  （缺失/多余/重复逐一记入 findings，不得返回 complete）；bench 测量
  集合恰为 30 单元×3 负载；HTTP 请求恰为 30 单元×计划指定 5 题；核对
  请求-单元对应关系（unit_key 必须属于网格）；`tests_valid_count` 等
  计数字段缺失不用 0 掩盖（记 [unverifiable] 并降级状态）。
- **S7 反例测试**：同一单元复制 30 个不同文件名 → 网格重复 (1,1)×30、
  缺失 29 组合、测量集合缺失 87 键全部检出，状态≠complete
  （合成文件仅存临时目录）。离线套件现为 118 项断言。
- **文档残留修正**：删除 FINAL_REPORT 中"（该工具不提供该参数）"括号
  说明（改为"本次命令未显式传入 -lm"）；`ctx=1024` 限定为正式 HTTP
  矩阵（bench 上下文隐式 d+p+n=320）；README 补 matplotlib 依赖、
  三模型已下载并官方核验、`--formal` 守卫语义（未加必拒、加后还需
  preflight）；configs/stage4_plan.json 与 docs/STAGE4_PLAN.md 的
  "无 -lm 参数"同步改为"本次命令未显式传入"。逐项 grep 确认无残留。
- **重新冻结说明**：上述两处措辞修正触及 Stage 4 冻结清单所列文件，
  按规程重新执行 `freeze_hashes_stage4.py`（理由=本轮 ordered 文字
  修正；历史清单内容可经 git 提交 789c50f/17e6905 查证）；Stage 3
  冻结清单未动（评分器/数据集哈希门控不受影响）。
- 两个 S4 run 的 `result_rebuilt.json` 再次归档为 `result_rebuilt_v2.json`
  后用严格化工具重生成：`completeness_status=complete`、findings=0。
