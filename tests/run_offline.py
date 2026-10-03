# -*- coding: utf-8 -*-
"""默认离线测试入口（本交付的推荐验收命令）。

只运行纯离线段：评分器、性能解析、日志解析、组合规则、哈希校验、
渲染一致性、slots 语义、McNemar 已知值、指纹正检等。
- 不启动 llama-server / llama-bench；
- 不访问任何推理服务；
- 不向正式 results/raw 写入任何记录。

集成测试（E/E2/F/F2：会启动后端并写 results/raw，E2 含真实模型请求）
必须显式运行：python tests/run_tests.py --integration
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_tests import main  # noqa: E402

if __name__ == "__main__":
    print("离线验收套件（不含真实模型调用）")
    sys.exit(main("offline"))
