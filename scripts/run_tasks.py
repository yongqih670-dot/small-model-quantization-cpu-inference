# -*- coding: utf-8 -*-
"""任务路径 runner：用 llama-server 执行评测任务集并逐条评分落盘（Stage 2）。

守卫与规则：
- 硬拒绝执行 datasets/test/ 下的正式测试集（本阶段只允许开发集）；
- 不做任何重试；失败/超时/截断照常记录并计入分母；
- 预热请求单独标记，不计入任何统计；
- 每条请求完成立即追加写入 requests.jsonl（中断也不丢已完成记录）；
- 速度/耗时使用后端 timings 原始值，未取整；缺失时记 null + 原因；
- 清理在 finally：Windows terminate 记录为主动强制终止。

用法:
    python scripts/run_tasks.py --dataset datasets/dev/dev_tasks.json \
        --config configs/dev_config.json --run-label dev10
"""

import argparse
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
                              count_orphan_servers, find_server_exe, new_run_id)
from score_answer import score_item

ROOT = Path(__file__).resolve().parent.parent


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"文件不存在: {path}"
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"文件损坏: {path} ({type(e).__name__}: {e})"


def build_user_message(task: dict, template: dict) -> str:
    tpl = template["per_type"][task["task_type"]]
    fields = {
        "input": task["input"],
        "field": task.get("field", "{field}"),
        "fields": ", ".join(task.get("fields", [])),
        "labels": "、".join(task.get("labels", [])),
        "rule": task.get("rule", "{rule}"),
    }
    return tpl.format(**fields)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--config", default="configs/dev_config.json")
    ap.add_argument("--template", default="datasets/prompt_template.json")
    ap.add_argument("--run-label", default="tasks")
    args = ap.parse_args()

    dataset_path = (ROOT / args.dataset).resolve()
    # ---- 守卫：正式测试集硬拒绝 ----
    if "datasets" in dataset_path.parts and "test" in dataset_path.parts:
        print(f"[GUARD] 拒绝执行正式测试集: {dataset_path.name}\n"
              "        本阶段(Stage 2)不运行正式 50 题。正式集运行属于后续阶段，"
              "需按协议另行确认。", file=sys.stderr)
        return 2

    run_id = new_run_id(args.run_label)
    run_dir = ROOT / "results" / "raw" / run_id
    log_dir = ROOT / "logs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_fh = (log_dir / "run_tasks.log").open("w", encoding="utf-8", buffering=1)
    log = logging.getLogger("run_tasks")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(log_fh), logging.StreamHandler(sys.stdout)):
        h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                                         "%H:%M:%S"))
        log.addHandler(h)

    result = {"run_id": run_id, "script": "scripts/run_tasks.py",
              "dataset": str(dataset_path.relative_to(ROOT)),
              "is_real_inference": True, "synthetic": False,
              "started_at_utc": ctl.utc_now_iso()}
    requests_path = run_dir / "requests.jsonl"
    server = None
    exit_code = 1

    try:
        cfg, e1 = load_json(ROOT / args.config)
        tpl, e2 = load_json(ROOT / args.template)
        data, e3 = load_json(dataset_path)
        errs = [x for x in (e1, e2, e3) if x]
        if errs:
            result.update({"success": False, "failure_stage": "load_inputs",
                           "error": "; ".join(errs)})
            raise RuntimeError(result["error"])

        cfg_path = (ROOT / args.config).resolve()
        result["config_id"] = cfg.get("config_id", cfg_path.stem)
        result["config_snapshot"] = cfg
        result["prompt_template"] = {k: tpl[k] for k in
                                     ("template_id", "system_prompt", "per_type")}
        tasks = data["tasks"] if isinstance(data, dict) else data
        result["task_count_requested"] = len(tasks)
        log.info("数据集 %s 共 %d 条 (config_id=%s)", dataset_path.name,
                 len(tasks), result["config_id"])

        model_path = ROOT / cfg["model_path"]
        if not model_path.is_file():
            raise RuntimeError(f"模型文件不存在: {model_path}")
        server_exe = find_server_exe()
        result["backend_version"] = capture_backend_version(server_exe)

        argv = ["-m", str(model_path),
                "--parallel", str(cfg["parallel"]),
                "-c", str(cfg["context_length"]),
                "-t", str(cfg["threads"]),
                "-tb", str(cfg["threads_batch"]),
                "-fa", cfg["flash_attn"],
                "-ngl", str(cfg["gpu_offload_layers"]),
                "--cache-reuse", str(cfg["cache_reuse"])]
        result["server_argv"] = argv

        log_srv = (log_dir / "server.log").open("w", encoding="utf-8", buffering=1)
        try:
            server = LlamaServer(server_exe, argv, log_srv, log,
                                 memory_sample_interval_s=cfg.get(
                                     "memory_sample_interval_s"))
            server.start()
            ready = server.wait_ready(cfg["load_timeout_s"],
                                      cfg["health_poll_interval_s"])
            result["server_startup_to_ready_s"] = server.startup_to_ready_s
            if not ready["ok"]:
                raise RuntimeError(f"服务未就绪: {ready['error']}")

            # ---- 预热请求（标记 warmup，排除统计） ----
            warm_payload = {"messages": [{"role": "user", "content": "1"}],
                            "max_tokens": 1, "temperature": 0.0,
                            "cache_prompt": False, "stream": False}
            warm = server.request("/v1/chat/completions", warm_payload,
                                  cfg["request_timeout_s"])
            result["warmup_request"] = {"marked_as": "warmup", "ok": warm["ok"],
                                        "excluded_from_stats": True}
            log.info("预热请求: ok=%s", warm["ok"])

            gen = cfg["generation"]
            counts = {"total": 0, "ok": 0, "request_failed": 0,
                      "timeout": 0, "truncated": 0,
                      "format_correct": 0, "content_correct": 0,
                      "correct": 0}
            per_type = {}
            for task in tasks:
                counts["total"] += 1
                user_msg = build_user_message(task, tpl)
                payload = {"messages": [
                    {"role": "system", "content": tpl["system_prompt"]},
                    {"role": "user", "content": user_msg}],
                    "temperature": gen["temperature"],
                    "top_p": gen.get("top_p", 1.0),
                    "max_tokens": task.get("max_tokens", gen["max_tokens"]),
                    "seed": gen["seed"], "cache_prompt": False,
                    "stream": False}
                rec = {"run_id": run_id, "config_id": result["config_id"],
                       "dataset": result["dataset"], "task_id": task["id"],
                       "task_type": task["task_type"],
                       "input": task["input"], "user_message": user_msg,
                       "generation_params": {k: payload[k] for k in
                                             ("temperature", "top_p", "max_tokens",
                                              "seed", "cache_prompt")},
                       "started_at_utc": ctl.utc_now_iso()}
                t0 = time.perf_counter()
                r = server.request("/v1/chat/completions", payload,
                                   cfg["request_timeout_s"])
                rec["end_to_end_client_s"] = r["client_e2e_s"]
                rec["http_status"] = r["http_status"]
                rec["error"] = r["error"]
                body = r.get("body") or {}
                choice = (body.get("choices") or [{}])[0]
                rec["raw_output"] = (choice.get("message") or {}).get("content")
                rec["finish_reason"] = choice.get("finish_reason")
                rec["usage_raw"] = body.get("usage")
                rec["timings_raw"] = body.get("timings")
                rec["timings_source"] = "server_timings" if body.get(
                    "timings") else "missing"
                rec["is_real_inference"] = True
                rec["synthetic"] = False

                if not r["ok"]:
                    rec["status"] = ("timeout" if "timed out" in (r["error"] or "")
                                     .lower() or "timeout" in (r["error"] or "").lower()
                                     else "request_failed")
                    counts[rec["status"]] += 1
                    rec["score"] = {"format_correct": False,
                                    "content_correct": False, "correct": False,
                                    "detail": {"reason": "请求失败/超时, 计入分母"}}
                    log.warning("任务 %s 失败: %s", task["id"], r["error"])
                else:
                    counts["ok"] += 1
                    truncated = choice.get("finish_reason") == "length"
                    rec["truncated"] = truncated
                    if truncated:
                        counts["truncated"] += 1
                        log.warning("任务 %s 达到生成上限被截断(标记, 不重试)",
                                    task["id"])
                    sc = score_item(task, rec["raw_output"] or "")
                    sc["truncated_output"] = truncated
                    rec["score"] = sc
                    counts["format_correct"] += int(sc["format_correct"])
                    counts["content_correct"] += int(sc["content_correct"])
                    counts["correct"] += int(sc["correct"])
                    pt = per_type.setdefault(task["task_type"], {
                        "total": 0, "correct": 0, "format_correct": 0,
                        "content_correct": 0})
                    pt["total"] += 1
                    pt["correct"] += int(sc["correct"])
                    pt["format_correct"] += int(sc["format_correct"])
                    pt["content_correct"] += int(sc["content_correct"])
                    log.info("[%d/%d] %s -> %r | 格式=%s 内容=%s 联合=%s",
                             counts["total"], len(tasks), task["id"],
                             (rec["raw_output"] or "")[:40],
                             sc["format_correct"], sc["content_correct"],
                             sc["correct"])

                rec["finished_at_utc"] = ctl.utc_now_iso()
                with requests_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            result["counts"] = counts
            result["per_task_type"] = per_type
            result["denominator_note"] = ("全部请求计入分母(含失败/超时/截断/格式错误); "
                                          "无剔除无重试")
            result["requests_jsonl"] = str(requests_path.relative_to(ROOT))
            result["success"] = counts["ok"] > 0 and counts["ok"] + \
                counts["request_failed"] + counts["timeout"] == counts["total"]
        finally:
            if server is not None:
                result["cleanup"] = server.stop()
            log_srv.close()
            result["orphan_servers_after"] = count_orphan_servers()
            result["cleanup_success"] = bool(
                result.get("cleanup", {}).get("confirmed_exit")
                and result["orphan_servers_after"] == 0)
        exit_code = 0 if result.get("success") else 1

    except KeyboardInterrupt:
        result["success"] = False
        result["error"] = "用户中断; 已完成条目已逐条落盘"
        exit_code = 130
    except Exception as exc:  # noqa: BLE001
        log.exception("run_tasks 失败")
        result["success"] = False
        result.setdefault("error", f"{type(exc).__name__}: {exc}")
        result.setdefault("failure_stage", "runtime")
        exit_code = 1
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
