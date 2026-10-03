# -*- coding: utf-8 -*-
"""Stage 3 矩阵 B：固定真实请求的性能实验。

预注册要点（configs/stage3_plan.json）：
- 任务固定顺序 dev-001, 003, 004, 007, 009（来自冻结开发集）；
- 3 模型 × 5 轮 × 5 题 = 75 条计时请求；每模型每轮独立启动服务，
  预热 1 条 + 缓存检查 2 条另计（校验通过才计时）；
- 记录启动/初始化区间/进程峰值内存/逐请求端到端/后端 timings；
- 各模型输出长度可能不同 → 这是实际任务响应比较，吞吐结论以矩阵 A 为准；
- 本矩阵输出不作为质量样本（quality_sample=false）；
- 断点续跑：已完成 (model, round) 启动单元跳过，不重复计时。

用法: python scripts/run_stage3_requestperf.py
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import preflight
import llama_server_ctl as ctl
from llama_server_ctl import (LlamaServer, capture_backend_version,
                              ensure_run_dir, find_server_exe, new_run_id,
                              resource_snapshot)
from run_tasks import build_user_message, server_argv_from_config

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    log = logging.getLogger("stage3B")

    plan = json.loads((ROOT / "configs/stage3_plan.json").read_text(encoding="utf-8"))
    cfg = json.loads((ROOT / "configs/formal_config.json").read_text(encoding="utf-8"))
    dev = json.loads((ROOT / "datasets/dev/dev_tasks.json").read_text(encoding="utf-8"))
    tpl = json.loads((ROOT / "datasets/prompt_template.json").read_text(encoding="utf-8"))

    print("=== Stage 3 矩阵 B preflight ===")
    pf = preflight.run_checks()
    if not pf["passed"]:
        log.error("preflight 未通过，拒绝开始正式采集")
        return 1

    task_order = plan["matrix_B_fixed_request_perf"]["task_order_fixed"]
    tasks_by_id = {t["id"]: t for t in dev["tasks"]}
    for tid in task_order:
        if tid not in tasks_by_id:
            log.error("计划中的任务不在开发集: %s", tid)
            return 1

    run_id = new_run_id("stage3perfB")
    run_dir = ensure_run_dir(ROOT / "results" / "raw", run_id)
    log_dir = ROOT / "logs" / run_id
    log_dir.mkdir(parents=True, exist_ok=False)
    requests_path = run_dir / "requests_perf.jsonl"

    result = {"run_id": run_id, "matrix": "B", "plan_id": plan["plan_id"],
              "is_real_inference": True, "synthetic": False,
              "config_id": cfg["config_id"],
              "quality_sample": False,
              "task_order_fixed": task_order,
              "round_orders": plan["round_orders"],
              "startups": [], "started_at_utc": ctl.utc_now_iso()}
    exit_code = 1
    server = None
    try:
        server_exe = find_server_exe()
        result["backend_version"] = capture_backend_version(server_exe)
        gen = cfg["generation"]
        timed_total = 0
        unit_no = 0
        try:
            for round_def in plan["round_orders"]:
                rnd = round_def["round"]
                for model in round_def["order"]:
                    unit_no += 1
                    marker = run_dir / f"startup_{unit_no:02d}_{model}_r{rnd}.json"
                    if marker.is_file():
                        log.info("[跳过已完成启动单元 %02d: %s 轮%d]",
                                 unit_no, model, rnd)
                        continue
                    snap = resource_snapshot()
                    argv = server_argv_from_config(cfg, ROOT / cfg["model_paths"][model])
                    log_srv = (log_dir / f"server_{unit_no:02d}_{model}_r{rnd}.log") \
                        .open("a", encoding="utf-8", buffering=1)
                    unit = {"unit": unit_no, "round": rnd, "model": model,
                            "argv": argv, "resource_before": snap,
                            "warmup_and_cache": {}, "timed_requests": 0}
                    try:
                        server = LlamaServer(server_exe, argv, log_srv, log,
                                             memory_sample_interval_s=cfg.get(
                                                 "memory_sample_interval_s"))
                        server.start()
                        ready = server.wait_ready(cfg["load_timeout_s"],
                                                  cfg["health_poll_interval_s"])
                        log_text = Path(log_srv.name).read_text(
                            encoding="utf-8", errors="replace")
                        init_stat = ctl.parse_load_time_from_log(log_text)
                        unit["server_startup"] = {
                            "ok": ready["ok"], "error": ready["error"],
                            "server_startup_to_ready_s": server.startup_to_ready_s,
                            "backend_model_init_interval_s":
                                init_stat["backend_model_init_interval_s"],
                            "model_load_time_s": init_stat["model_load_time_s"],
                            "model_load_time_reason": init_stat["reason"]}
                        if not ready["ok"]:
                            raise RuntimeError(f"服务未就绪: {ready['error']}")
                        # 预热 + 缓存检查（另计，不计入 75 条）
                        warm_payload = {"messages": [{"role": "user",
                                                      "content": "1"}],
                                        "max_tokens": 1, "temperature": 0.0,
                                        "cache_prompt": False, "stream": False}
                        warm = server.request("/v1/chat/completions", warm_payload,
                                              cfg["request_timeout_s"])
                        cc = []
                        for _ in (1, 2):
                            r = server.request("/v1/chat/completions",
                                               warm_payload,
                                               cfg["request_timeout_s"])
                            b = r.get("body") or {}
                            cc.append({"cache_n": (b.get("timings") or {})
                                       .get("cache_n"),
                                       "prompt_n": (b.get("timings") or {})
                                       .get("prompt_n")})
                        cache_ok = bool(warm["ok"] and cc[0]["cache_n"] == 0
                                        and cc[1]["cache_n"] == 0
                                        and cc[0]["prompt_n"] == cc[1]["prompt_n"]
                                        and (cc[0]["prompt_n"] or 0) > 1)
                        unit["warmup_and_cache"] = {
                            "warmup_ok": warm["ok"], "cache_check": cc,
                            "cache_ok": cache_ok,
                            "excluded_from_timed_total": True}
                        if not (warm["ok"] and cache_ok):
                            raise RuntimeError("预热/缓存校验失败，本轮不计入正式测量")

                        for tid in task_order:
                            task = tasks_by_id[tid]
                            payload = {"messages": [
                                {"role": "system",
                                 "content": tpl["system_prompt"]},
                                {"role": "user",
                                 "content": build_user_message(task, tpl)}],
                                "temperature": gen["temperature"],
                                "top_p": gen["top_p"],
                                "max_tokens": gen["max_tokens"],
                                "seed": gen["seed"], "cache_prompt": False,
                                "stream": False}
                            rec = {"run_id": run_id, "matrix": "B",
                                   "config_id": cfg["config_id"],
                                   "round": rnd, "model": model,
                                   "task_id": tid, "task_type": task["task_type"],
                                   "input": task["input"],
                                   "generation_params": {
                                       k: payload[k] for k in
                                       ("temperature", "top_p", "max_tokens",
                                        "seed", "cache_prompt")},
                                   "started_at_utc": ctl.utc_now_iso(),
                                   "is_real_inference": True,
                                   "synthetic": False,
                                   "quality_sample": False}
                            r = server.request("/v1/chat/completions", payload,
                                               cfg["request_timeout_s"])
                            body = r.get("body") or {}
                            rec.update({
                                "http_status": r["http_status"],
                                "error": r["error"],
                                "error_type": r.get("error_type"),
                                "end_to_end_client_s": r["client_e2e_s"],
                                "raw_output": ((body.get("choices") or [{}])[0]
                                               .get("message", {})
                                               .get("content")),
                                "finish_reason": ((body.get("choices") or [{}])[0]
                                                  .get("finish_reason")),
                                "usage_raw": body.get("usage"),
                                "timings_raw": body.get("timings"),
                                "response_body": body if r["ok"] else None})
                            rec["performance"] = ctl.extract_perf(
                                rec["timings_raw"], rec["usage_raw"],
                                r["client_e2e_s"])
                            with requests_path.open("a", encoding="utf-8") as f:
                                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                            unit["timed_requests"] += 1
                            timed_total += 1
                            log.info("  [%s 轮%d] %s e2e=%.3fs 输出=%r",
                                     model, rnd, tid, r["client_e2e_s"],
                                     (rec["raw_output"] or "")[:24])
                        unit["memory"] = server.memory_stats()
                        unit["ok"] = True
                    finally:
                        if server is not None:
                            if "memory" not in unit:
                                unit["memory"] = server.memory_stats()
                            unit["cleanup"] = server.stop()
                            server = None
                        log_srv.close()
                        unit["resource_after"] = resource_snapshot()
                    marker.write_text(json.dumps(unit, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
                    result["startups"].append(
                        {"unit": unit_no, "model": model, "round": rnd,
                         "ok": unit.get("ok", False),
                         "timed_requests": unit["timed_requests"],
                         "marker": marker.name})
        except KeyboardInterrupt:
            result["error"] = "用户中断; 已完成单元已落盘, 可重跑本脚本续做"
            result["interrupted"] = True

        result["timed_requests_total"] = timed_total
        result["expected_timed_requests"] = 75
        result["startup_units_expected"] = 15
        result["startup_units_completed"] = len(
            [s for s in result["startups"] if s["ok"]])
        result["execution_complete"] = (timed_total == 75
                                        and result["startup_units_completed"] == 15)
        result["success"] = bool(result["execution_complete"]
                                 and not result.get("interrupted"))
        exit_code = 0 if result["success"] else 1
    except Exception as exc:  # noqa: BLE001
        log.exception("矩阵 B 失败")
        result["success"] = False
        result.setdefault("error", f"{type(exc).__name__}: {exc}")
    finally:
        result["finished_at_utc"] = ctl.utc_now_iso()
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("结果: %s (success=%s, 计时请求=%s)", run_dir / "result.json",
                 result.get("success"), result.get("timed_requests_total"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
