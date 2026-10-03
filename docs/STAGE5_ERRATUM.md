# 勘误（STAGE5_ERRATUM，rev4）

本勘误针对 Stage 4 冻结材料中的表述。原 Stage 4 计划
（`configs/stage4_plan.json`、`docs/STAGE4_PLAN.md`）是**历史冻结材料**，
其内容保持冻结时原样（本仓库曾于 rev3 轮误将其文字修改并重新冻结，
rev4 已按 rev2 交付包原始字节恢复，恢复来源与哈希见
`docs/stage5_closeout_revision.md` 追加节）。**相关表述以本勘误为准**：

1. **llama-bench 加载模式**：原计划写作"llama-bench 无 -lm 参数"。
   准确表述为：**本次实验的 llama-bench 命令未显式传入 `-lm`**，
   其加载模式为工具内部默认（auto/mmap 类）；server 路径显式
   `-lm mmap`。两条路径分别记录，不宣称统一。
2. **上下文**：`ctx=1024` 仅限定于**正式 HTTP 矩阵**（llama-server）；
   矩阵 A 的 llama-bench 命令未传入 `-c`，其上下文由负载隐式决定
   （depth 0 + prompt 256 + gen 64）。
3. 其余勘误项（若有）随收尾修订记录追加于本文件。

以最终报告 `docs/FINAL_REPORT.md` 与本勘误为准；历史冻结材料不再通过
全文替换修改，亦不再重新冻结。
