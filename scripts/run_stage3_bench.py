# -*- coding: utf-8 -*-
"""Stage 3 矩阵 A：固定工作量 llama-bench 对照（Q4_K_M/Q5_K_M/Q8_0）。

预注册要点（configs/stage3_plan.json）：
- 5 轮 × 3 模型 = 15 次 bench 进程启动；每次 -r 1，同时产出 pp256/tg64/
  pg256+64 三个测试条目 → 45 个工作负载测量值；
- 每轮模型顺序由种子 20261003 预生成并冻结于计划，驱动脚本只校验不重排；
- pg 是组合工作负载指标，不称生成速度；
- 每次调用前记录资源快照；全部原始 stdout/stderr/argv/samples 落盘。

用法: python scripts/run_stage3_bench.py
"""

import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import preflight
import llama_server_ctl as ctl
from llama_server_ctl import (ROOT, ensure_run_dir, new_run_id,
                              resource_snapshot)

TOOLS = ROOT / "tools" / "llama.cpp"


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    log = logging.getLogger("stage3A")

    plan = json.loads((ROOT / "configs/stage3_plan.json").read_text(encoding="utf-8"))
    cfg = json.loads((ROOT / "configs/formal_config.json").read_text(encoding="utf-8"))

    print("=== Stage 3 矩阵 A preflight ===")
    pf = preflight.run_checks()
    if not pf["passed"]:
        log.error("preflight 未通过，拒绝开始正式采集")
        return 1

    run_id = new_run_id("stage3benchA")
    run_dir = ensure_run_dir(ROOT / "results" / "raw", run_id)
    log_dir = ROOT / "logs" / run_id
    log_dir.mkdir(parents=True, exist_ok=False)

    result = {"run_id": run_id, "matrix": "A", "plan_id": plan["plan_id"],
              "is_real_inference": True, "synthetic": False,
              "config_id": cfg["config_id"],
              "invocations": [], "started_at_utc": ctl.utc_now_iso()}
    exit_code = 1
    try:
        bench = sorted(TOOLS.rglob("llama-bench.exe"))[0]
        result["bench_executable"] = str(bench)
        mpaths = cfg["model_paths"]

        inv_no = 0
        for round_def in plan["round_orders"]:
            rnd = round_def["round"]
            for model in round_def["order"]:
                inv_no += 1
                snap = resource_snapshot()
                argv = [str(bench), "-m", str(ROOT / mpaths[model]),
                        "-ngl", str(cfg["gpu_offload_layers"]),
                        "-t", str(cfg["threads"]),
                        "-fa", cfg["flash_attn"],
                        "-p", str(plan["matrix_A_fixed_workload_bench"]["workloads"]["pp"]["n_prompt"]),
                        "-n", str(plan["matrix_A_fixed_workload_bench"]["workloads"]["tg"]["n_gen"]),
                        "-pg", f"{plan['matrix_A_fixed_workload_bench']['workloads']['pg']['pp']},"
                               f"{plan['matrix_A_fixed_workload_bench']['workloads']['pg']['tg']}",
                        "-d", str(plan["matrix_A_fixed_workload_bench"]["depth"]),
                        "-r", str(plan["matrix_A_fixed_workload_bench"]["llama_bench_repetitions"]),
                        "-o", "json", "-oe", "json", "--progress"]
                log.info("[%d/15] 轮%d %s: %s", inv_no, rnd, model, " ".join(argv))
                proc = subprocess.run(argv, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      timeout=1800)
                inv = {"invocation": inv_no, "round": rnd, "model": model,
                       "argv": argv, "exit_code": proc.returncode,
                       "resource_before": snap,
                       "resource_after": resource_snapshot(),
                       "stdout_file": f"inv_{inv_no:02d}_{model}_stdout.txt",
                       "stderr_file": f"inv_{inv_no:02d}_{model}_stderr.txt"}
                (run_dir / inv["stdout_file"]).write_text(
                    proc.stdout or "", encoding="utf-8")
                (run_dir / inv["stderr_file"]).write_text(
                    proc.stderr or "", encoding="utf-8")
                parsed = None
                try:
                    parsed = json.loads(proc.stdout[proc.stdout.index("["):])
                except (ValueError, json.JSONDecodeError):
                    pass
                inv["bench_json"] = parsed
                inv["tests_parsed"] = len(parsed) if isinstance(parsed, list) else 0
                inv["ok"] = proc.returncode == 0 and inv["tests_parsed"] == 3
                result["invocations"].append(inv)
                if not inv["ok"]:
                    log.error("调用失败: 轮%d %s exit=%s tests=%s",
                              rnd, model, proc.returncode, inv["tests_parsed"])
                else:
                    for t in parsed:
                        log.info("  %s n_prompt=%s n_gen=%s avg_ts=%.2f "
                                 "stddev_ts=%.2f samples=%s",
                                 model, t.get("n_prompt"), t.get("n_gen"),
                                 t.get("avg_ts") or -1, t.get("stddev_ts") or -1,
                                 t.get("samples_ts"))
        result["invocation_count"] = len(result["invocations"])
        result["invocations_ok"] = sum(1 for i in result["invocations"] if i["ok"])
        # 45 个测量值校验
        result["workload_measurements"] = sum(
            i["tests_parsed"] for i in result["invocations"])
        result["execution_complete"] = (result["invocation_count"] == 15
                                        and result["invocations_ok"] == 15
                                        and result["workload_measurements"] == 45)
        result["performance_record_valid"] = result["execution_complete"]
        result["success"] = result["execution_complete"]
        result["cleanup_success"] = True  # 无常驻服务进程
        result["orphan_servers_global_after"] = ctl.count_orphan_servers()
        exit_code = 0 if result["success"] else 1
    except Exception as exc:  # noqa: BLE001
        log.exception("矩阵 A 失败")
        result["success"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["finished_at_utc"] = ctl.utc_now_iso()
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("结果: %s (success=%s, 测量值=%s)", run_dir / "result.json",
                 result.get("success"), result.get("workload_measurements"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
