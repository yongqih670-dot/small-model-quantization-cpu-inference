# -*- coding: utf-8 -*-
"""探针运行：在正式写死 runner 逻辑前，实证以下行为假设（Stage 2 证据）。

1. --parallel 1 是否使 n_slots = 1；
2. 服务端日志时间戳格式 M.SS.mmm.uuu 是否成立、能否解析加载耗时；
3. 请求体 cache_prompt=false 是否禁用跨请求前缀复用（同一提示连发两次，
   两次的 prompt_n 应相同且 cache_n 均为 0）；
4. /props 是否暴露聊天模板信息；
5. 退出后无残留进程。

本探针输出为环境行为证据，不是性能实验结果。
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import llama_server_ctl as ctl
from llama_server_ctl import (LlamaServer, capture_backend_version,
                              count_orphan_servers, find_server_exe, new_run_id,
                              parse_load_time_from_log)

ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("probe")


def main():
    run_id = new_run_id("probe")
    run_dir = ROOT / "results" / "raw" / run_id
    log_dir = ROOT / "logs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    server_exe = find_server_exe()
    ver = capture_backend_version(server_exe)
    model = ROOT / "models" / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    assert model.is_file(), f"模型不存在: {model}"

    argv = ["-m", str(model), "--parallel", "1", "-c", "1024",
            "-t", "4", "-tb", "4", "-fa", "off", "-ngl", "0",
            "--cache-reuse", "0", "-lv", "5"]
    # 预热与正式计时分开标记：探针本身即环境验证，无正式计时请求
    result = {"run_id": run_id, "purpose": "environment_behavior_probe",
              "is_real_inference": True,
              "not_a_performance_result": True,
              "backend_version": ver, "server_argv": argv,
              "config_snapshot": {"parallel": 1, "ctx": 1024, "threads": 4,
                                  "threads_batch": 4, "fa": "off", "ngl": 0,
                                  "cache_reuse": 0, "verbosity": 5}}

    log_fh = (log_dir / "server.log").open("w", encoding="utf-8", buffering=1)
    server = None
    try:
        server = LlamaServer(server_exe, argv, log_fh, log,
                             memory_sample_interval_s=0.05)
        server.start()
        ready = server.wait_ready(timeout_s=300, poll_interval_s=0.25)
        result["startup"] = {"ok": ready["ok"], "error": ready["error"],
                             "server_startup_to_ready_s": server.startup_to_ready_s,
                             "health_poll_interval_s": 0.25,
                             "warmup": "backend default (enabled), 未显式传 --no-warmup"}
        if not ready["ok"]:
            raise RuntimeError(ready["error"])

        props = server.request("/props", None, timeout_s=10)
        body = props.get("body") or {}
        result["props"] = {
            "keys": sorted(body.keys()),
            "chat_template_present": "chat_template" in body,
            "chat_template_first120": (body.get("chat_template") or "")[:120],
            "model_path_field": body.get("model_path"),
            "default_generation_settings_keys": sorted(
                (body.get("default_generation_settings") or {}).keys()),
        }

        # ---- 同一提示连续两次，cache_prompt=false ----
        payload = {"messages": [{"role": "user",
                                 "content": "请只回答一个数字：2 + 3 = ?"}],
                   "temperature": 0.0, "max_tokens": 16, "seed": 42,
                   "cache_prompt": False, "stream": False}
        checks = []
        for i in (1, 2):
            r = server.request("/v1/chat/completions", payload, timeout_s=120)
            b = r.get("body") or {}
            t = b.get("timings") or {}
            u = b.get("usage") or {}
            checks.append({
                "request_index": i, "ok": r["ok"], "error": r["error"],
                "raw_response_saved": True,
                "usage": u, "timings": t,
                "cache_n": t.get("cache_n"),
                "prompt_n": t.get("prompt_n"),
                "output": (b.get("choices") or [{}])[0].get("message", {}).get("content"),
                "finish_reason": (b.get("choices") or [{}])[0].get("finish_reason"),
            })
            (run_dir / f"probe_request_{i}.json").write_text(
                json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        c1, c2 = checks
        cache_ok = (c1["cache_n"] == 0 and c2["cache_n"] == 0
                    and c1["prompt_n"] == c2["prompt_n"]
                    and (c1["prompt_n"] or 0) > 1)
        result["cache_reuse_check"] = {
            "requests": checks, "passed": bool(cache_ok),
            "rule": "两次 cache_n 均为 0 且第二次 prompt_n 与第一次相同(完整重算)"}

        mem = server.memory_stats()
        result["memory"] = mem
        result["cleanup"] = {"planned": "finally 中 stop()"}
    except Exception as e:  # noqa: BLE001
        log.exception("探针失败: %r", e)
        result["probe_error"] = f"{type(e).__name__}: {e}"
        result["success"] = False
    finally:
        if server is not None:
            result["cleanup"] = server.stop()
        log_fh.flush()
        log_fh.close()

    # ---- 日志分析 ----
    log_text = (log_dir / "server.log").read_text(encoding="utf-8", errors="replace")
    parsed = parse_load_time_from_log(log_text)
    result["load_time_parse"] = parsed
    interesting = [ln for ln in log_text.splitlines()
                   if any(k in ln for k in ("n_slots", "model loaded",
                                            "listening on", "backend",
                                            "warmup", "warm", "devices",
                                            "n_threads", "loading model"))]
    result["log_evidence_lines"] = interesting[:30]
    result["orphan_servers_after"] = count_orphan_servers()
    result["success"] = (result.get("probe_error") is None
                         and result.get("cache_reuse_check", {}).get("passed") is True
                         and result.get("cleanup", {}).get("confirmed_exit") is True
                         and result["orphan_servers_after"] == 0)

    (run_dir / "probe.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("=== 探针结果 success=%s, run_id=%s ===", result["success"], run_id)
    log.info("startup_to_ready=%s", result.get("startup", {}).get("server_startup_to_ready_s"))
    log.info("load_time_parse=%s", json.dumps(parsed, ensure_ascii=False))
    log.info("cache_check_passed=%s", result.get("cache_reuse_check", {}).get("passed"))
    log.info("orphan_servers_after=%s", result["orphan_servers_after"])
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
