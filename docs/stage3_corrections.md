# Stage 3 修正记录（Corrections）

本文件区分历史记录（Stage 1/2 产物，保持原样）与 Stage 3 的修正内容。
Stage 2 的 `docs/stage2_corrections.md` 保留不动。

## 一、对既有表述的修正（应用户与验收要求）

**"Stage 1 逐字节未动"的表述不准确**，本阶段起区分三种不同保证：

1. **换行归一后内容一致**：Stage 1 的四个证据文件
   （`results/raw/smoke_test.json`、`logs/smoke_test.log`、
   `environment/system_info.json`、`models/model_manifest.json`）
   与 git 提交 `6484457` 中的内容在忽略行尾（CRLF/LF）后**完全一致**——
   工作区文件因 `core.autocrlf=true` 存在 CRLF，仓库 blob 为 LF，
   差异仅限行尾（2026-10-03 核验：`blob 1924B / 0 CRLF` vs
   `工作区 1969B / 45 CRLF`，归一后逐字节相同）。
2. **测试前后未变化**：离线测试套件在每次运行前后对这些文件取哈希对比
   （tests/run_tests.py G 项），保证测试活动本身不修改历史证据。
3. **原始字节一致**：受行尾归一化影响，工作区文件与 git blob **不是**
   原始字节一致。为使今后跨机器校验稳定，Stage 3 添加 `.gitattributes`
   将 `results/**`、`logs/**`、`datasets/**`、`environment/**`、`docs/**`
   标记为 `-text`（禁用行尾转换）。
   本阶段未为满足验收而改写任何历史文件。

## 二、runner 与有效性判定的修正

| # | Stage 2 问题 | Stage 3 修正 | 证据 |
| --- | --- | --- | --- |
| 1 | 任务路径请求未做统一性能校验；HTTPError 一律 `status=null` | 每条请求统一走 `extract_perf()`；HTTPError 单独捕获，保存状态码与错误响应体 | `scripts/llama_server_ctl.py`；`tests/run_tests.py` F2 |
| 2 | 性能字段未检查有限性/范围；单一 valid 标志 | null/NaN/Infinity/负耗时无效；提示处理、生成、客户端端到端有效性**分开表示**；`predicted_n<=1`（无可测解码步）生成速度标注不可靠且不输出；`predicted_n==completion_tokens` 仅作交叉核对，不据此声明分母语义 | `tests/run_tests.py` C2/B 段 |
| 3 | 内存未在服务退出前统一采集；`model_load_time_s` 语义仍模糊 | 每次运行退出前采集生命周期峰值 + 采样峰值 + 间隔；日志区间改名 `backend_model_init_interval_s`（不称纯权重加载）；`model_load_time_s` 留空 + 原因 | `run_tasks.py`/`run_stage3_requestperf.py` 结果 JSON |
| 4 | success 仅由执行+清理组成，语义未细分 | 四态分离：`execution_complete` / `quality_evaluation_valid` / `performance_record_valid` / `cleanup_success`；`compose_run_success()` 预注册组合；预热失败不继续正式测量 | `tests/run_tests.py` C3 |
| 5 | 未显式记录未完成任务 | 中断/失败路径自动将剩余任务标 `not_attempted`；已完成请求保留 | `tests/run_tests.py` E2 |
| 6 | 超时后直接发下一请求（潜在隐藏重叠） | 超时后 `GET /slots` 等待空闲（≤60s），未空闲则终止服务并标记剩余 `not_attempted` | `configs/formal_config.json::timeout_recovery`；`llama_server_ctl.py::slots_idle` |
| 7 | 清理检查用全局同名进程计数 | 只针对本程序启动的 PID；区分"服务自行退出"与"主动强制终止"；全局计数仅作环境记录 | `llama_server_ctl.py::stop`；测试 F 段适配 |
| 8 | 正式运行无启动前核验 | `scripts/preflight.py`：冻结哈希/模型 SHA256/后端哈希与版本/文档存在性；任一不通过即拒绝 `--formal`；提供只检查不推理的独立命令 | `scripts/preflight.py`；实际拒绝案例见本文件 §五 |

## 三、数据集一致性修复（test_tasks_v2，revision 2）

逐项变更见 `docs/stage3_dataset_changes.md`：test-012 金标改 `38.5`
（对齐无单位规则，单位移入字段名）；模板澄清单位/标点口径（不删答案内部
必要字符）；test-020/029 字段澄清消除唯一答案歧义；test-006 标注含推导；
test-023 预注册科室严格口径。新增逐题"渲染提示—输入—金标—评分规则"
一致性校验（`validate_datasets.py`，不调用模型）。预注册 JSON 重复键
（后者覆盖）与 NaN/Infinity（按 Python json 语义解析）的处理，并配测试。
原 Stage 2 冻结清单保留；新清单 `datasets/frozen_hashes_stage3.json`。

## 四、llama-bench 与 server 的 FA 统一

Stage 2 中 bench 用默认 `auto`、server 用 `off`，两条路径不一致。
Stage 3 统一为 **`-fa off`**（依据：正式配置固定需显式、且 CPU 路径
`auto` 的实际选择未单独验证），已在 `configs/formal_config.json` 与
`docs/STAGE3_PLAN.md` 记录。

## 五、preflight 实际生效记录

2026-10-03 03:16（UTC）首次 preflight 因 `docs/stage3_corrections.md`
（本文件）不存在而正确拒绝进入正式模式；补齐文件并重新冻结后通过。
第二次拒绝因模型清单 v3 尚未建立（Q5/Q8 未下载）——下载并校验后通过。
门禁有效性由两次真实失败案例佐证。

## 六、实施细节调整（正式结果产生前完成并记录）

`scripts/run_tasks.py` 在矩阵 C 启动前发现缺少选择三模型之一的入口
（formal_config 的 `model_paths` 为字典），增加了 `--model-key` 参数并
重新冻结（调整时机：矩阵 A/B 已完成，**矩阵 C 的正式质量结果尚未产生**；
该调整不触及评分规则、数据集、模板与计划矩阵，属工具入口补齐）。
之后 preflight 重新通过，矩阵 C 才开始执行。

## 七、软件故障测试与真实推理的分离

- 测试 fixture（`tests/fixtures/`：损坏 JSON、非法 GGUF、坏配置）不产生
  任何模型输出；`results/` 中无 `synthetic: true` 记录，且**凡带模型输出
  的记录必须显式 `is_real_inference: true`**（H 段加强断言）。
- 未启动模型的失败（如清单损坏）记录 `is_real_inference: false,
  requests_sent: 0`；服务启动但零请求同样不算真实推理（E/F 段断言）。
