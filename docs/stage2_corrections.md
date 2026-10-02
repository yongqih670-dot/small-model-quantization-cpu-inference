# Stage 2 修正记录（Corrections）

本文档区分**历史记录**（Stage 1 产物，保持原样不改）与**修正后的指标定义**
（Stage 2 起生效）。Stage 1 原始 JSON、日志与环境记录一律不覆盖、不修改。

## 一、历史记录清单（未改动）

| 文件 | 状态 |
| --- | --- |
| `results/raw/smoke_test.json` | Stage 1 冒烟结果，保持原样（哈希由 tests/run_tests.py G 项持续校验） |
| `logs/smoke_test.log` | Stage 1 日志，保持原样 |
| `environment/system_info.json` / `.md` | Stage 1 环境记录，保持原样（其中 `total_ram_gb` 字段名沿用了 gb 名称但数值实为 GiB，见下） |
| `models/model_manifest.json` | Stage 1 模型清单，保持原样 |
| git 提交 `6484457` | 历史提交，不重写；git 身份未改动 |

Stage 1 版 smoke 测试脚本可经 `git show 6484457:scripts/smoke_test.py` 取回；
`scripts/smoke_test.py` 已被修正版覆盖属预期（代码随阶段演进，结果证据不动）。

## 二、Stage 1 问题 → Stage 2 修正对照

| # | Stage 1 问题 | Stage 2 修正 | 证据位置 |
| --- | --- | --- | --- |
| 1 | `success` 只要求"输出非空"，链路成功/答案正确/清理未分离 | 三态分离：`link_success` / `answer_correct`（`output.strip()=="5"` 预注册）/ `cleanup_success`；总体 success = 四项预注册验收条件（含缓存校验与性能记录有效） | `scripts/smoke_test.py`；`results/raw/run_*_smoke_v2_*/result.json` |
| 2 | "model_load_time_s" 实为"启动到就绪"时间 | 改名 `server_startup_to_ready_s`（含 Popen→/health 200，记录轮询间隔与 warmup 设置）；`model_load_time_s` 仅取后端日志时间戳差值（`loading model`→`model loaded`），取不到为 null+原因 | `scripts/llama_server_ctl.py::parse_load_time_from_log`；冒烟结果 `model_load_time_source` 字段 |
| 3 | 结果文件中数值全部 round 过（丢失精度） | 原始值不取整（如 `0.597119`），取整仅出现在日志展示；`usage_raw`/`timings_raw` 完整保存 | `results/raw/run_20261003_001306_smoke_v2_cecfb7/result.json` |
| 4 | 生成速度用 `completion_tokens/e2e` 兜底计算的风险 | 速度仅取 `timings.predicted_per_second`（后端自报）；`extract_perf()` 单一实现：缺失 timings → null+原因+`performance_record_valid=false`，绝不猜算；`predicted_n` 与 `completion_tokens` 分母交叉核对 | `scripts/llama_server_ctl.py::extract_perf`；`tests/run_tests.py` B 项 |
| 5 | 未显式控制并发：默认 `--parallel -1(auto)` 实际给了 4 个 slot | 显式 `--parallel 1`、`-t`/`-tb`、`-fa off`、`--cache-reuse 0`；生效配置从服务端日志提取（`n_slots = 1`、`n_threads = 4 (n_threads_batch = 4)`） | 探针证据 `results/raw/run_20261002_234950_probe_77ec6f/`；帮助文本 `environment/evidence/` |
| 6 | 跨请求提示缓存未验证（默认 cache_prompt=true） | 请求体显式 `cache_prompt: false` + 同一提示连发两次验证 `cache_n=0` 且 `prompt_n` 相同（完整重算）；预热与正式计时分开标记 | 冒烟结果 `cache_reuse_check`（passed=true） |
| 7 | 清理不在可靠路径，exit code 1 未解释 | 清理移入 `finally`（覆盖正常/异常/KeyboardInterrupt）；只终止本程序启动的进程；Windows terminate 如实记录为"主动强制终止"，不称优雅退出 | `scripts/llama_server_ctl.py::stop`；`tests/run_tests.py` F 项 |
| 8 | 异常处理中会重复读取可能已损坏的清单；版本信息只取单一输出流 | `load_json()` 一次失败即记录原因不重读；`capture_backend_version` 保留 stdout/stderr 与退出码 | `scripts/smoke_test.py`、`scripts/llama_server_ctl.py`；`tests/run_tests.py` E 项 |
| 9 | `total_ram_gb` 命名不准确；CIM 时钟字段无语义标注 | 新增 `environment/system_info_v2.json`：`total_ram_gib` + 时钟字段标注"标称基础时钟，非实测频率/已验证睿频"；Stage 1 文件不动 | `environment/system_info_v2.md` |
| 10 | 模型核验仅有镜像侧 LFS 元数据比对（huggingface.co 当时不可达），存在把镜像自校验当官方核验的风险 | 新增 `models/model_manifest_v2.json`：revision `9217f5db…`、下载 URL 与元数据核验 URL 分列；官方独立核验在窗口期内完成：本地 SHA256 与 **huggingface.co 官方 API** LFS 元数据一致（verified=true） | `models/model_manifest_v2.json::official_independent_verification` |
| 11 | 后端只有版本字符串，无文件级证据 | `environment/backend_manifest.json`：分发 zip 与关键 exe/DLL SHA256（含实际加载的 `ggml-cpu-zen4.dll`）、`--list-devices`（none，无 GPU） | 该文件 + `environment/evidence/` |

## 三、Stage 2 开发调试导致的口径/数据变更（均记录在案）

1. **评分器缺陷修复**（代码对齐已冻结文档口径）：`format_correct` 漏判"多余
   JSON 字段"，修正为直接解析 ∧ 无多余字段（离线测试 A 项暴露）。
2. **JSON 字符串值对称剥离成对外层引号/书名号**：开发运行暴露金标"原文表述"
   规则与书名号冲突（模型输出 `《机器学习导论》` 被误判错）；归一对金标与输出
   对称作用，规则写入评分器 docstring 与协议 §4。
3. **dev-005 金标修正**：快递公司 `中通` → `中通快递`（对齐"使用输入原文表述"
   预注册规则，属金标缺陷修复，非迁就模型）。
4. **开发集近重复修复**：dev-007/dev-008 与正式集 test-037/test-033 同规则同
   金标（validate_datasets.py 近重复检查暴露），改为不同标签域（停课判定、
   二手出售/求助）。
5. 上述变更后 `scripts/freeze_hashes.py` 重新执行（冻结历史：v1 首冻 → v2 评分
   器修复后重冻 → v3 引号剥离+金标修正后重冻），当前哈希以
   `datasets/frozen_hashes.json` 为准。

## 四、llama-bench 参数语义核实（b11347）

失败记录保留了两次真实试错（`run_20261003_001547_benchdev_a9098f`：`-tpp`
不存在；`run_20261003_001617_benchdev_9a1e2f`：`-c` 不存在），据此确认：
单一 `-t`（pp/tg 共用）、无 `-c`（上下文隐式=d+p+n）、`-d` 控制 KV 深度。
成功运行：`run_20261003_001723_benchdev_ff8776`（3 测试 × 3 重复，含
`samples_ns/samples_ts` 逐重复值）。

## 五、合成/真实分离

离线测试使用固定断言与 fixtures（`tests/fixtures/`），不产生任何模型输出；
`results/` 目录不存在 `synthetic: true` 记录（tests/run_tests.py H 项持续校验）。
