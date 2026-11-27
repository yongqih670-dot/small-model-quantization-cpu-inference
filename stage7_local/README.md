# Stage 7　同机模型加载与服务启动

2026 年 11 月 27 日在 Stage 1–6 使用的 Ryzen 9 9955HX（16 核 32 线程、31.18 GiB 内存、Windows 11 Build 26100）上复测。正式运行前后均接通电源。权重仍为同一 Qwen2.5-0.5B-Instruct 的三份官方 GGUF，均通过冻结清单的 SHA256 核验。每次先顺序预读模型，再启动独立 CPU 服务。数值为六次启动的均值 ± 样本标准差，单位 ms。

| 量化版本 | 成功 / 尝试 | 模型加载函数 | 后端初始化 | 启动到可用 |
|---|---:|---:|---:|---:|
| Q4_K_M | 6 / 6 | 353.5 ± 10.7 | 802.7 ± 35.2 | 892.5 ± 34.9 |
| Q5_K_M | 6 / 6 | 318.2 ± 9.1 | 773.9 ± 55.8 | 867.6 ± 59.8 |
| Q8_0 | 6 / 6 | 332.0 ± 11.7 | 805.6 ± 31.7 | 899.7 ± 37.2 |

Q5 的加载函数均值比 Q4 短 35.3 ms。三版本启动到可用的均值范围为 867.6–899.7 ms，最大差 32.1 ms，量级接近各版本的轮间波动。本结果限于本机、文件预读、mmap 与默认内置预热的条件。Windows 可按需调页，加载函数返回时间只涵盖该函数的调用区间。

[逐次测量 CSV](runs/20261127_225012_formal_d8a1d1/measurements.csv) · [正式汇总与环境](runs/20261127_225012_formal_d8a1d1/summary.json) · [逐次 JSON 与原始日志](runs/20261127_225012_formal_d8a1d1/) · [原版与计时版试跑](runs/20261127_225007_smoke_00e564/)

## 测量方法

- 后端为 llama.cpp b11347 的官方 Windows x64 CPU 发布包，提交 5fc4f3c8c。将原版 llama.dll 保存为 llama_original.dll，计时版保留全部 277 个导出的名称和序号，仅在原版加载函数调用前后读取 QueryPerformanceCounter。其余导出直接转发。此次使用 MSVC 14.51.36231 x64 工具链构建计时 DLL。
- 原始日志的 STAGE7_LOAD 记录计数器差值、频率、成功状态和加载参数。主指标只取一次成功的完整加载（no_alloc=0、vocab_only=0），容量检查中的元数据调用另记。
- 后端初始化从 load_model: loading model 到 llama_server: model loaded，包含上下文初始化和内置预热。启动到可用从客户端启动进程前到读完首个成功健康响应，探测间隔 10 ms、单次超时 50 ms。三个区间包含关系见原始日志，不能相加。
- 固定 4 个生成线程和 4 个批处理线程、ctx=1024、单 slot、mmap、纯 CPU。六轮遍历三版本的六种排列，各版本在每个位置出现两次。Q4 的原版和计时版试跑返回相同文本和停止原因。试跑使用开发题，返回「未提供」，与金标不同。

## Windows 复现

准备 Python 3.12、psutil，以及 MinGW-w64 的 gcc 与 objdump 或 Visual Studio x64 Native Tools 命令提示符。将官方 ZIP 放入 assets，并解压一份到 assets/backend 供原版试跑。三个模型可用下载脚本获取，也可直接放置清单中同哈希的文件。

```powershell
python download_models.py
python instrument/build.py
python run_experiment.py smoke
python run_experiment.py formal --smoke-evidence "runs/<实际试跑目录>/summary.json"
```

[官方发布包](https://github.com/ggml-org/llama.cpp/releases/tag/b11347) · [模型清单](model_manifest.json) · [官方模型仓库](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF)
