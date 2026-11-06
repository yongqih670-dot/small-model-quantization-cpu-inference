# 本机首次加载实验

研究对象为 Qwen2.5-0.5B-Instruct 的 Q4_K_M、Q5_K_M、Q8_0。该目录独立于 Stage 1–6 的历史结果。

## 2026 年 11 月 6 日本机结果

Windows 11、Ryzen AI 9 H 365、10 核 20 线程、31.29 GiB 内存。每次正式启动前后均记录为电池供电、电量 78%。三份模型均通过项目清单的 SHA256 校验。表中单位为毫秒，数值为 6 次独立进程启动的均值 ± 样本标准差。

| 量化版本 | 成功 / 尝试 | 模型加载函数 | 后端初始化 | 启动到可用 |
| --- | --- | ---: | ---: | ---: |
| Q4_K_M | 6 / 6 | 297.2 ± 5.4 | 685.9 ± 22.9 | 768.5 ± 28.0 |
| Q5_K_M | 6 / 6 | 275.7 ± 8.2 | 674.5 ± 20.1 | 754.2 ± 16.7 |
| Q8_0 | 6 / 6 | 282.4 ± 3.3 | 688.5 ± 19.7 | 773.0 ± 27.8 |

在本机、预读模型文件并使用 mmap 的条件下，Q5 的加载函数平均用时最短，比 Q4 少 21.5 ms。三者的启动到可用均值最大相差 18.8 ms，和各版本启动等待时间的轮间波动相近。这组结果只描述同机预读后的新进程启动；历史 Stage 3 使用另一台 CPU。Q4 烟雾测试中原版与计时版的输出和停止原因一致，两者均将 dev-001 答为“未提供”，该题金标为“周雨桐”。该检查核对的是计时模块接入前后的行为。

已逐条核对 18 份原始日志中的加载计数与初始化时间戳，验证六种排列完整、每个模型在每个位置出现两次，并独立重算上表均值及标准差。

[逐次测量 CSV](runs/20261106_191932_formal_4a2840/measurements.csv) · [正式汇总与环境记录](runs/20261106_191932_formal_4a2840/summary.json) · [原始日志和逐次 JSON](runs/20261106_191932_formal_4a2840/) · [对应烟雾测试](runs/20261106_184820_smoke_569204/)

## 测量方法

- 使用 b11347 官方 Windows x64 CPU 发布包（提交 5fc4f3c8c）。`instrument/build.py` 先核验发布包 SHA256，在私有的 `timed_backend` 目录中生成计时 DLL。官方的 llama.dll 以 llama_original.dll 的名称保存，字节相同。277 个导出的名称和序号逐一核对。除加载函数外的导出直接转发；计时函数使用该版本官方 C 头文件和 QueryPerformanceCounter 包围原函数调用，返回原函数结果。频率查询、日志写入排除在加载函数计时之外。
- 原始日志中的 `STAGE7_LOAD` 记录每次调用的计数器差值、频率、成功状态及模型参数。只有 `no_alloc=0` 且 `vocab_only=0` 的一次完整加载返回成功才可计入主指标。后端自动容量检查的元数据调用另外保留。
- 后端初始化从 `load_model: loading model` 到 `llama_server: model loaded`，包含上下文和后端内置预热。对用户可见的总耗时从客户端调用 Popen 之前到首次成功读完 HTTP 200、status=ok 的健康响应。健康检查休眠间隔 10 ms，单次连接/读超时 50 ms，探测存在调度延迟。三个时段互相包含，不能相加。
- 每次先顺序预读整个 GGUF，然后新建进程。Windows 可以自行管理页缓存，mmap 模式下允许后续按需调页；本实验测量预读条件下的函数返回时间。
- 固定 CPU-only、4 个生成与批处理线程、ctx=1024、单 slot、mmap、保留默认内置预热等设置，详见各运行的 protocol.json。模型与后端来源见 model_manifest.json 和下方复现命令。
- 先用 Q4 在原版和计时版各跑一遍开发题 dev-001，要求两者实际输出与停止原因一致，并核验计时标记。正式实验与试运行分开存档。正式实验每轮轮换三个版本，共六种排序，每档在每个位置恰出现两次。失败保留记录，统计使用有效观测及样本标准差。

## Windows 复现命令

在本目录操作。依赖为 Python 3、psutil、Windows curl、MinGW x64 的 gcc 与 objdump。将官方发布包放入 assets，再执行以下命令。下载器核验模型最终 SHA256；也可以直接放入同哈希的模型文件。

```powershell
python download_models.py
python instrument/build.py
python run_experiment.py smoke
# 填入上一步产生的实际 summary.json 路径
python run_experiment.py formal --smoke-evidence "<完整的 smoke summary.json 路径>"
```

首次在新目录复现时还需把官方 zip 解压至 `assets/backend`，供原版烟雾测试使用。每次运行单独创建 `runs` 子目录，保留原始日志、逐次 JSON、CSV、协议及带样本数的汇总。

[官方发布包](https://github.com/ggml-org/llama.cpp/releases/tag/b11347) · [模型仓库](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF) · [后端容量检查](https://github.com/ggml-org/llama.cpp/blob/b11347/common/fit.cpp#L57-L60) · [加载与预热](https://github.com/ggml-org/llama.cpp/blob/b11347/common/common.cpp#L1234-L1453)
