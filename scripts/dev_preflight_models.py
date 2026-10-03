# -*- coding: utf-8 -*-
"""Stage 3 三模型开发预检（有限预检，非正式结果）。

对 Q4_K_M/Q5_K_M/Q8_0 各执行一次有限预检：
- 以正式公共配置启动服务（不调整各模型专属参数）；
- 核对三版本内嵌聊天模板一致（/props.chat_template SHA256）；
- 各跑 1 条开发题（dev-001）确认可加载、可推理、输出非空；
- 预热+缓存检查通过后才发预检请求。

用法: python scripts/dev_preflight_models.py
"""

import hashlib
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

import llama_server_ctl as ctl
from llama_server_ctl import (LlamaServer, capture_backend_version,
                              ensure_run_dir, find_server_exe, new_run_id)
from run_tasks import build_user_message, server_argv_from_config

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    log = logging.getLogger("dev_preflight")

    cfg = json.loads((ROOT / "configs/formal_config.json").read_text(encoding="utf-8"))
    dev = json.loads((ROOT / "datasets/dev/dev_tasks.json").read_text(encoding="utf-8"))
    tpl = json.loads((ROOT / "datasets/prompt_template.json").read_text(encoding="utf-8"))
    task = next(t for t in dev["tasks"] if t["id"] == "dev-001")

    run_id = new_run_id("devpreflight3")
    run_dir = ensure_run_dir(ROOT / "results" / "raw", run_id)
    log_dir = ROOT / "logs" / run_id
    log_dir.mkdir(parents=True, exist_ok=False)

    result = {"run_id": run_id, "purpose": "三模型开发预检(非正式结果)",
              "is_real_inference": True, "synthetic": False,
              "config_id": cfg["config_id"], "models": [],
              "started_at_utc": ctl.utc_now_iso()}
    server = None
    exit_code = 1
    try:
        server_exe = find_server_exe()
        result["backend_version"] = capture_backend_version(server_exe)
        template_hashes = {}
        for model in ["Q4_K_M", "Q5_K_M", "Q8_0"]:
            log.info("=== 预检 %s ===", model)
            argv = server_argv_from_config(cfg, ROOT / cfg["model_paths"][model])
            log_srv = (log_dir / f"server_{model}.log").open(
                "w", encoding="utf-8", buffering=1)
            entry = {"model": model, "argv": argv}
            try:
                server = LlamaServer(server_exe, argv, log_srv, log,
                                     memory_sample_interval_s=cfg.get(
                                         "memory_sample_interval_s"))
                server.start()
                ready = server.wait_ready(cfg["load_timeout_s"],
                                          cfg["health_poll_interval_s"])
                entry["server_startup_to_ready_s"] = server.startup_to_ready_s
                if not ready["ok"]:
                    raise RuntimeError(f"服务未就绪: {ready['error']}")
                props = server.request("/props", None, timeout_s=10)
                tpl_text = (props.get("body") or {}).get("chat_template") or ""
                template_hashes[model] = hashlib.sha256(
                    tpl_text.encode("utf-8")).hexdigest()
                # 预热 + 缓存检查
                warm_payload = {"messages": [{"role": "user", "content": "1"}],
                                "max_tokens": 1, "temperature": 0.0,
                                "cache_prompt": False, "stream": False}
                warm = server.request("/v1/chat/completions", warm_payload,
                                      cfg["request_timeout_s"])
                cc = []
                for _ in (1, 2):
                    r = server.request("/v1/chat/completions", warm_payload,
                                       cfg["request_timeout_s"])
                    b = r.get("body") or {}
                    cc.append({"cache_n": (b.get("timings") or {}).get("cache_n"),
                               "prompt_n": (b.get("timings") or {}).get("prompt_n")})
                cache_ok = bool(warm["ok"] and cc[0]["cache_n"] == 0
                                and cc[1]["cache_n"] == 0
                                and cc[0]["prompt_n"] == cc[1]["prompt_n"]
                                and (cc[0]["prompt_n"] or 0) > 1)
                entry["cache_check_passed"] = cache_ok
                if not cache_ok:
                    raise RuntimeError("缓存校验失败")
                # 1 条开发题
                payload = {"messages": [
                    {"role": "system", "content": tpl["system_prompt"]},
                    {"role": "user", "content": build_user_message(task, tpl)}],
                    "temperature": cfg["generation"]["temperature"],
                    "top_p": cfg["generation"]["top_p"],
                    "max_tokens": cfg["generation"]["max_tokens"],
                    "seed": cfg["generation"]["seed"],
                    "cache_prompt": False, "stream": False}
                r = server.request("/v1/chat/completions", payload,
                                   cfg["request_timeout_s"])
                body = r.get("body") or {}
                out = ((body.get("choices") or [{}])[0].get("message") or {})
                entry["preflight_task"] = {
                    "task_id": task["id"], "ok": r["ok"],
                    "output": out.get("content"),
                    "finish_reason": (body.get("choices") or [{}])[0]
                    .get("finish_reason"),
                    "e2e_s": r["client_e2e_s"]}
                entry["loadable_and_infers"] = bool(
                    r["ok"] and (out.get("content") or "").strip())
                entry["memory"] = server.memory_stats()
                (run_dir / f"preflight_{model}.json").write_text(
                    json.dumps({"model": model, "entry": entry},
                               ensure_ascii=False, indent=2), encoding="utf-8")
                log.info("%s: 加载%.2fs 输出=%r 缓存校验=%s", model,
                         entry["server_startup_to_ready_s"],
                         (out.get("content") or "")[:20], cache_ok)
            finally:
                if server is not None:
                    entry["cleanup"] = server.stop()
                    server = None
                log_srv.close()
            result["models"].append(entry)

        hashes = set(template_hashes.values())
        result["chat_template_identical"] = len(hashes) == 1
        result["chat_template_sha256"] = (hashes.pop() if len(hashes) == 1
                                          else template_hashes)
        result["all_models_ready"] = all(
            m.get("loadable_and_infers") and m.get("cache_check_passed")
            for m in result["models"])
        result["success"] = bool(result["all_models_ready"]
                                 and result["chat_template_identical"]
                                 and all(m.get("cleanup", {}).get("confirmed_exit")
                                         for m in result["models"]))
        exit_code = 0 if result["success"] else 1
    except Exception as exc:  # noqa: BLE001
        log.exception("开发预检失败")
        result["success"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["finished_at_utc"] = ctl.utc_now_iso()
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("预检结果: success=%s 模板一致=%s", result.get("success"),
                 result.get("chat_template_identical"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
