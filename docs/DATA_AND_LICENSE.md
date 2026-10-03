# 数据来源、许可与标注说明

## 模型与后端

| 项 | 来源 | 许可 | 核验 |
| --- | --- | --- | --- |
| Qwen2.5-0.5B-Instruct GGUF（Q4_K_M/Q5_K_M/Q8_0） | Qwen 官方 Hugging Face 仓库 `Qwen/Qwen2.5-0.5B-Instruct-GGUF`，revision `9217f5db79a29953eb74d5343926648285ec7e67` | Apache-2.0 | 本地 SHA256 与官方 API LFS 元数据逐字节比对一致（`models/model_manifest_v3.json`） |
| llama.cpp b11347 | ggml-org 官方 GitHub Actions 预编译包（win-cpu-x64） | MIT | 50 个 exe/DLL 全量 SHA256（`environment/backend_manifest.json`、stage4 冻结清单） |

下载通道说明：本网络环境直连 huggingface.co 不稳定，实际下载经公开镜像
hf-mirror.com 完成；镜像仅作通道，**核验以官方 API 元数据为准**，两者在
manifest 中分开记录。

## 评测数据

- **全部自建**：开发集 10 题、正式测试集 50 题
  （`datasets/dev/`、`datasets/test/`），均为本课题为评测小模型
  抽取/分类/规范化能力而编写合成文本，不含任何真实个人信息；
- **标注规范**：每题含唯一 ID、任务类型、完整输入、标准答案与评分方式；
  标准答案可由题面唯一确定（规则内嵌于题目），无人工主观判断项；
- **数据划分**：开发集与正式集输入零重叠（校验器强制），并做近重复
  （同规则同金标同前缀）检查；
- **修订记录**：test-012 金标、test-020/029 字段澄清等逐项变更见
  `docs/stage3_dataset_changes.md`（正式结果产生后未再改动）；
- **冻结**：`datasets/frozen_hashes_stage3.json`（Stage 3 起口径）与
  `frozen_hashes_stage4.json`。
