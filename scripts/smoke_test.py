# -*- coding: utf-8 -*-
"""冒烟测试 v2（Stage 2 修正版）。

Stage 1 版本的问题与修正见 docs/stage2_corrections.md。本版要点：
- 三个独立判定，总体 success 由预先注册的验收条件组合得出：
    link_success      链路成功（启动就绪 + 请求 HTTP 200 + 输出非空）
    answer_correct    答案正确（output.strip() == "5"，预注册）
    cleanup_success   进程清理成功（主动终止后确认退出且无残留）
- server_startup_to_ready_s：Popen → /health 200 的耗时（含后端内置 warmup）；
  model_load_time_s 仅取后端日志差值统计，取不到则 null + 原因；
- 速度只使用后端 timings 自报值；timings 缺失 → null + 原因 +
  performance_record_valid=false，不编造兜底值；原始值不取整，展示才取整；
- 显式固定：--parallel 1、-t、-tb、-fa off、-ngl 0、--cache-reuse 0，
  并从服务端日志提取实际生效配置（n_slots、n_threads）；
- 预热请求与正式计时请求分开标记；另做"同一提示两次"缓存复用校验；
- 清理在 finally 路径：Windows terminate 如实记录为主动强制终止；
- 配置/清单缺失或损坏时仍写结构化失败记录（异常处理中不重读清单）。

结果写入 results/raw/<run_id>/，日志写入 logs/<run_id>/。
用法: python scripts/smoke_test.py
"""

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import llama_server_ctl as ctl
from llama_server_ctl import (LlamaServer, capture_backend_version,
                              count_orphan_servers, find_server_exe, new_run_id,
                              parse_load_time_from_log)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "configs" / "smoke_test_v2.json"
MANIFEST_PATH = ROOT / "models" / "model_manifest.json"


def load_json(path: Path):
    """返回 (data, error)。损坏/缺失时 error 说明原因，绝不二次读取。"""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"文件不存在: {path}"
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"文件损坏无法解析: {path} ({type(e).__name__}: {e})"


def main() -> int:
    run_id = new_run_id("smoke_v2")
    run_dir = ROOT / "results" / "raw" / run_id
    log_dir = ROOT / "logs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_fh = (log_dir / "smoke_test.log").open("w", encoding="utf-8", buffering=1)
    log = logging.getLogger("smoke_v2")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(log_fh), logging.StreamHandler(sys.stdout)):
        h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                                         "%H:%M:%S"))
        log.addHandler(h)

    result = {"run_id": run_id, "script": "scripts/smoke_test.py",
              "stage": "2-corrected-smoke", "is_real_inference": True,
              "synthetic": False, "started_at_utc": ctl.utc_now_iso()}

    server = None
    exit_code = 1
    try:
        # ---- 配置与清单（失败即结构化记录，不重读） ----
        cfg, cfg_err = load_json(CONFIG_PATH)
        manifest, man_err = load_json(MANIFEST_PATH)
        if cfg_err:
            result.update({"success": False, "link_success": False,
                           "answer_correct": None, "cleanup_success": None,
                           "failure_stage": "load_config", "error": cfg_err})
            raise RuntimeError(cfg_err)
        result["config_snapshot"] = cfg
        result["config_path"] = str(CONFIG_PATH.relative_to(ROOT))
        if man_err:
            # 清单损坏不阻断链路验证，但记录缺证事实
            result["manifest_error"] = man_err
            log.warning("清单问题: %s", man_err)
        else:
            result["model_manifest"] = {
                k: manifest.get(k) for k in
                ("filename", "quantization", "sha256", "size_bytes", "source")}

        model_path = ROOT / cfg["model_path"]
        if not model_path.is_file():
            raise RuntimeError(f"模型文件不存在: {model_path}")

        server_exe = find_server_exe()
        result["backend_version"] = capture_backend_version(server_exe)
        log.info("后端版本 exit=%s stderr首行=%s",
                 result["backend_version"]["exit_code"],
                 (result["backend_version"]["stderr"].splitlines() or [""])[0])

        argv = ["-m", str(model_path),
                "--parallel", str(cfg["parallel"]),
                "-c", str(cfg["context_length"]),
                "-t", str(cfg["threads"]),
                "-tb", str(cfg["threads_batch"]),
                "-fa", cfg["flash_attn"],
                "-ngl", str(cfg["gpu_offload_layers"]),
                "--cache-reuse", str(cfg["cache_reuse"])]
        result["server_argv"] = argv
        result["effective_intent"] = {
            "single_inference_process": True, "parallel_slots": cfg["parallel"],
            "threads_generation": cfg["threads"],
            "threads_prompt_batch": cfg["threads_batch"],
            "flash_attn": cfg["flash_attn"], "gpu_offload_layers":
                cfg["gpu_offload_layers"], "cache_reuse": cfg["cache_reuse"],
            "cache_prompt_request_body": False,
            "backend": "llama.cpp llama-server (官方预编译 CPU 构建)"}

        log_fh_server = (log_dir / "server.log").open("w", encoding="utf-8",
                                                      buffering=1)
        try:
            server = LlamaServer(server_exe, argv, log_fh_server, log,
                                 memory_sample_interval_s=cfg.get(
                                     "memory_sample_interval_s"))
            server.start()
            ready = server.wait_ready(
                timeout_s=cfg["load_timeout_s"],
                poll_interval_s=cfg["health_poll_interval_s"])
            result["server_startup"] = {
                "ok": ready["ok"], "error": ready["error"],
                "server_startup_to_ready_s": server.startup_to_ready_s,
                "health_poll_interval_s": cfg["health_poll_interval_s"],
                "warmup": "backend default enabled (未传 --no-warmup)",
                "includes": "进程拉起+服务初始化+模型加载+内置warmup; 不含清理/写文件/退出"}
            if not ready["ok"]:
                raise RuntimeError(f"服务未就绪: {ready['error']}")
            log.info("server_startup_to_ready_s = %.6f (未取整展示 %.3f)",
                     server.startup_to_ready_s,
                     round(server.startup_to_ready_s, 3))

            # ---- 预热请求（标记 warmup，不计入计时结论） ----
            payload = {"messages": [
                {"role": "system", "content": cfg["system_prompt"]},
                {"role": "user", "content": cfg["prompt"]}],
                "temperature": cfg["temperature"], "max_tokens":
                cfg["max_tokens"], "seed": cfg["seed"], "cache_prompt": False,
                "stream": False}
            warm = server.request("/v1/chat/completions", payload,
                                  cfg["request_timeout_s"])
            result["warmup_request"] = {"marked_as": "warmup",
                                        "ok": warm["ok"],
                                        "excluded_from_stats": True}
            if not warm["ok"]:
                raise RuntimeError(f"预热请求失败: {warm['error']}")

            # ---- 正式计时请求（唯一一次） ----
            t0 = time.perf_counter()
            resp = server.request("/v1/chat/completions", payload,
                                  cfg["request_timeout_s"])
            req_e2e = time.perf_counter() - t0
            (run_dir / "measured_request.json").write_text(
                json.dumps(resp, ensure_ascii=False, indent=2),
                encoding="utf-8")
            if not resp["ok"]:
                result["link_success"] = False
                raise RuntimeError(f"推理请求失败: {resp['error']}")
            body = resp["body"]
            choice = (body.get("choices") or [{}])[0]
            output = (choice.get("message") or {}).get("content") or ""
            usage = body.get("usage") or {}
            timings = body.get("timings") or {}
            log.info("输出=%r finish_reason=%s", output,
                     choice.get("finish_reason"))

            # ---- 缓存复用校验：同一提示再连发两次 ----
            cache_reqs = []
            for i in (1, 2):
                r = server.request("/v1/chat/completions", payload,
                                   cfg["request_timeout_s"])
                (run_dir / f"cache_check_request_{i}.json").write_text(
                    json.dumps(r, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                b = r.get("body") or {}
                cache_reqs.append({
                    "index": i, "ok": r["ok"], "error": r["error"],
                    "cache_n": (b.get("timings") or {}).get("cache_n"),
                    "prompt_n": (b.get("timings") or {}).get("prompt_n"),
                    "cached_tokens": ((b.get("usage") or {})
                                      .get("prompt_tokens_details", {})
                                      .get("cached_tokens"))})
            c1, c2 = cache_reqs
            cache_passed = bool(
                c1["ok"] and c2["ok"]
                and c1["cache_n"] == 0 and c2["cache_n"] == 0
                and c1["prompt_n"] == c2["prompt_n"] and (c1["prompt_n"] or 0) > 1)
            log.info("缓存校验: %s", "通过(两次均完整重算)" if cache_passed
                     else "未通过!")

            # ---- 指标（原始值不取整；速度仅用后端统计，缺失不伪造） ----
            perf = ctl.extract_perf(timings, usage)
            if not perf["performance_record_valid"]:
                result["performance_missing_reason"] = perf["missing_reason"]
            # model_load_time_s：无独立可靠统计则留空；区间值另记
            log_text = (log_dir / "server.log").read_text(
                encoding="utf-8", errors="replace")
            init_stat = parse_load_time_from_log(log_text)
            # 生效配置证据（从服务端日志提取）
            eff = {}
            for line in log_text.splitlines():
                if "n_slots = " in line and "n_ctx_slot" in line:
                    eff["n_slots_line"] = line.strip()[:160]
                if "system_info: n_threads" in line:
                    eff["threads_line"] = line.strip()[:160]
                if "warming up the model" in line:
                    eff["warmup_line"] = line.strip()[:160]
            result["effective_config_evidence"] = eff

            link_success = bool(ready["ok"] and resp["ok"] and output.strip())
            answer_correct = output.strip() == cfg["expected_answer"]

            result.update({
                "model": model_path.name,
                "prompt": cfg["prompt"],
                "output": output,
                "raw_output_unmodified": True,
                "finish_reason": choice.get("finish_reason"),
                "usage_raw": usage,
                "timings_raw": timings,
                "end_to_end_request_s": req_e2e,
                "end_to_end_includes": "单次HTTP请求(含模板组装/推理/JSON序列化); "
                                       "不含服务启动、清理与写结果文件",
                "performance": perf,
                "server_startup_to_ready_s": server.startup_to_ready_s,
                "backend_model_init_interval_s":
                    init_stat["backend_model_init_interval_s"],
                "model_load_time_s": init_stat["model_load_time_s"],
                "model_load_time_reason": init_stat["reason"],
                "cache_reuse_check": {"requests": cache_reqs,
                                      "passed": cache_passed,
                                      "rule": "两次 cache_n=0 且 prompt_n 相同"
                                              "且>1(完整重算)"},
                "memory": server.memory_stats(),
                "link_success": link_success,
                "answer_correct": answer_correct,
                "answer_rule": "output.strip() == '%s'" % cfg["expected_answer"],
            })
        finally:
            # ---- 清理（finally 覆盖正常/异常/中断路径） ----
            if server is not None:
                result["cleanup"] = server.stop()
            log_fh_server.close()
            result["orphan_servers_after"] = count_orphan_servers()
            result["cleanup_success"] = bool(
                result.get("cleanup", {}).get("confirmed_exit")
                and result["orphan_servers_after"] == 0)

        # ---- 预注册的总体验收条件 ----
        result["success"] = bool(
            result["link_success"] and result["answer_correct"]
            and result["cleanup_success"] and cache_passed)
        result["acceptance_rule"] = ("link_success AND answer_correct "
                                     "AND cleanup_success AND cache_reuse_check.passed")
        if not result["performance"]["performance_record_valid"]:
            result["success"] = False  # 性能记录无效时总体不判成功
            result["acceptance_rule"] += "; 且 performance.performance_record_valid"
        exit_code = 0 if result["success"] else 1

    except KeyboardInterrupt:
        result["success"] = False
        result["error"] = "用户中断 (KeyboardInterrupt); 清理已在 finally 执行"
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 —— 结构化失败记录，不隐藏
        log.exception("smoke v2 失败")
        result["success"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        result.setdefault("failure_stage", "runtime")
        result.setdefault("link_success", False)
        result.setdefault("answer_correct", None)
        result.setdefault("cleanup_success", None)
        for k in ("model", "prompt", "output", "server_startup_to_ready_s",
                  "model_load_time_s", "end_to_end_request_s",
                  "generation_tokens_per_second", "usage_raw", "timings_raw"):
            result.setdefault(k, None)
    finally:
        result["finished_at_utc"] = ctl.utc_now_iso()
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("结果: %s (success=%s)", run_dir / "result.json",
                 result.get("success"))
        log_fh.flush()
        log_fh.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
