# -*- coding: utf-8 -*-
"""llama-bench 封装（性能路径，Stage 2 仅验证采集链路，不做性能结论）。

- 使用与 llama-server 完全相同的官方预编译包中的 llama-bench.exe；
- 显式 -ngl 0（CPU only）、-t / -tpp / -tg 线程、-c 上下文；
- -p / -n / -pg / -d 语义已按 environment/evidence/llama_bench_help_b11347.txt 核实：
    p  = 每次重复的提示处理 token 数 (pp)
    n  = 每次重复的生成 token 数 (tg)
    pg = 组合测试: 先处理 pp 个提示 token, 再生成 tg 个 token
    d  = 测量前预填充的上下文深度 (KV 深度), 默认 0
- 输出：stdout 原始文本(md 表格, 含逐重复行) + stderr 上的 json 汇总(-oe json)
  全部保存；未取整数值以工具自报为准。
- 注意：llama-bench 是批处理微基准，其 pp/tg 吞吐与 HTTP 请求端到端延迟
  不是同一种指标，两者不混用、不互推（见协议文档）。

用法: python scripts/run_bench.py --config configs/bench_dev.json --run-label bench
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import llama_server_ctl as ctl
from llama_server_ctl import ROOT, capture_backend_version, count_orphan_servers

TOOLS_DIR = ROOT / "tools" / "llama.cpp"


def find_bench_exe() -> Path:
    hits = sorted(TOOLS_DIR.rglob("llama-bench.exe"))
    if not hits:
        raise FileNotFoundError("未找到 llama-bench.exe")
    return hits[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/bench_dev.json")
    ap.add_argument("--run-label", default="bench")
    args = ap.parse_args()

    run_id = ctl.new_run_id(args.run_label)
    run_dir = ROOT / "results" / "raw" / run_id
    log_dir = ROOT / "logs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    log = logging.getLogger("run_bench")

    result = {"run_id": run_id, "script": "scripts/run_bench.py",
              "is_real_inference": True, "synthetic": False,
              "purpose": "llama-bench 采集链路验证(非性能结论)",
              "started_at_utc": ctl.utc_now_iso()}
    exit_code = 1
    try:
        cfg = json.loads((ROOT / args.config).read_text(encoding="utf-8"))
        result["config_snapshot"] = cfg
        result["config_id"] = cfg.get("config_id", Path(args.config).stem)
        model = ROOT / cfg["model_path"]
        if not model.is_file():
            raise RuntimeError(f"模型不存在: {model}")
        bench = find_bench_exe()
        result["backend_version"] = capture_backend_version(TOOLS_DIR / "llama-server.exe")
        result["bench_executable"] = str(bench)

        argv = [str(bench), "-m", str(model),
                "-ngl", "0",
                "-t", str(cfg["threads"]),
                "-p", str(cfg["pp_tokens"]),
                "-n", str(cfg["tg_tokens"]),
                "-pg", f"{cfg['pg_pp']},{cfg['pg_tg']}",
                "-d", str(cfg.get("depth", 0)),
                "-r", str(cfg["repetitions"]),
                "-o", "json", "-oe", "json",
                "--progress"]
        # 注（b11347 语义核实，失败记录见 run_20261003_001547/001617 两个目录）：
        # 1) llama-bench 只有单一 -t（pp/tg 共用），无 -tpp/-tg；
        # 2) llama-bench 无 -c 参数：每次测试的上下文由负载隐式决定
        #    （深度 d + 提示 p + 生成 n），config 的 context_length 仅用于
        #    llama-server 任务路径；KV 深度影响用 -d 控制。
        result["bench_param_semantics"] = {
            "p": "每次重复的提示处理 token 数 (pp)",
            "n": "每次重复的生成 token 数 (tg)",
            "pg": "组合测试: 先处理 pp 个提示 token 再生成 tg 个",
            "d": "测量前预填充的上下文深度 (KV 深度), 默认 0",
            "threads": "单一 -t, pp/tg 共用",
            "context": "无显式 -c 参数, 上下文=隐式(d+p+n)"}
        result["argv"] = argv
        log.info("运行 llama-bench: %s", " ".join(argv))
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=cfg.get("timeout_s", 1800))
        (run_dir / "stdout_raw.txt").write_text(proc.stdout or "",
                                                encoding="utf-8")
        (run_dir / "stderr_raw.txt").write_text(proc.stderr or "",
                                                encoding="utf-8")
        result["exit_code"] = proc.returncode
        # stdout 为 json 数组(-o json)：每个测试一个条目，含 samples_ns /
        # samples_ts 逐重复原始值；进度行(--progress)在 stderr。
        parsed = None
        try:
            idx = (proc.stdout or "").index("[")
            parsed = json.loads(proc.stdout[idx:])
        except (ValueError, json.JSONDecodeError):
            parsed = None
        result["bench_json_summary"] = parsed
        result["stdout_nonempty"] = bool((proc.stdout or "").strip())
        result["success"] = (proc.returncode == 0 and parsed is not None)
        if proc.returncode != 0:
            result["error"] = f"llama-bench 退出码 {proc.returncode}; " \
                              f"stderr 前 500 字符: {(proc.stderr or '')[:500]}"
        log.info("llama-bench 退出码=%s, json摘要=%s", proc.returncode,
                 "已解析" if parsed else "未解析到")
        exit_code = 0 if result["success"] else 1
    except Exception as exc:  # noqa: BLE001
        log.exception("run_bench 失败")
        result["success"] = False
        result.setdefault("error", f"{type(exc).__name__}: {exc}")
    finally:
        result["finished_at_utc"] = ctl.utc_now_iso()
        result["orphan_servers_after"] = count_orphan_servers()
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("结果: %s", run_dir / "result.json")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
