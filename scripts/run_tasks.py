# -*- coding: utf-8 -*-
"""任务路径 runner（Stage 3 修正版）。

Stage 2 → 3 的修正（详见 docs/stage3_corrections.md）：
- 每条请求统一走 extract_perf（有限性/必要字段/合理范围），并把
  提示处理、生成、客户端端到端的有效性分开记录；
- HTTPError 保存状态码与错误响应体，不再全部 status=null；
- 四态判定分离：execution_complete / quality_evaluation_valid /
  performance_record_valid / cleanup_success；success ≠ "至少一个请求成功"；
- 正式模式（--formal）需 preflight 全部通过才能解锁 datasets/test/，
  未启用时仍然硬拒绝；
- 请求超时 ≠ 服务端已停止：超时后经 GET /slots 确认空闲才继续；
  确认失败则终止本程序启动的服务并把剩余任务标记 not_attempted；
- 内存（生命周期峰值 + 采样峰值 + 间隔）在服务退出前采集；
  日志初始化区间记为 backend_model_init_interval_s（不称纯权重加载）；
- 断点续跑 --resume <run_id>：核对已落盘请求，跳过已完成 task_id，
  不重复执行已完成正式题；
- run 目录创建拒绝覆盖。

用法:
    python scripts/run_tasks.py --dataset datasets/dev/dev_tasks.json --run-label dev
    python scripts/run_tasks.py --dataset datasets/test/test_tasks.json \
        --config configs/formal_config.json --formal --run-label formal_q4
    python scripts/run_tasks.py --dataset datasets/test/test_tasks.json \
        --config configs/formal_config.json --formal --resume <run_id>
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
                              count_orphan_servers, ensure_run_dir,
                              find_server_exe, new_run_id, resource_snapshot,
                              slots_idle)
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


def server_argv_from_config(cfg: dict, model_path: Path) -> list:
    """由配置生成 server argv（字段与实际参数一一对应）。

    batch/ubatch/KV 类型/加载模式/FA 缺省时取 llama.cpp 对应默认值，
    以兼容 Stage 2 的开发配置；正式配置（formal_config.json）显式固定全部值。
    """
    return ["-m", str(model_path),
            "--parallel", str(cfg["parallel"]),
            "-c", str(cfg["context_length"]),
            "-b", str(cfg.get("batch_size", 2048)),
            "-ub", str(cfg.get("ubatch_size", 512)),
            "-ctk", cfg.get("cache_type_k", "f16"),
            "-ctv", cfg.get("cache_type_v", "f16"),
            "-t", str(cfg["threads"]),
            "-tb", str(cfg["threads_batch"]),
            "-fa", cfg.get("flash_attn", "off"),
            "-lm", cfg.get("load_mode", "mmap"),
            "-ngl", str(cfg["gpu_offload_layers"]),
            "--cache-reuse", str(cfg["cache_reuse"])]


def compose_run_success(execution_complete: bool, cleanup_success: bool,
                        formal: bool = False, preflight_passed: bool = True) -> bool:
    """预注册的总体成功组合：执行完整 ∧ 清理成功（正式模式还需 preflight）。

    质量有效性与性能有效性单列，不参与该组合；缺性能不抹去质量答案，
    答错不构成执行失败，清理失败不能被总体成功掩盖。
    """
    ok = bool(execution_complete and cleanup_success)
    if formal:
        ok = ok and bool(preflight_passed)
    return ok


def is_formal_dataset(dataset_path: Path) -> bool:
    parts = dataset_path.parts
    return "datasets" in parts and "test" in parts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--config", default="configs/dev_config.json")
    ap.add_argument("--template", default="datasets/prompt_template.json")
    ap.add_argument("--run-label", default="tasks")
    ap.add_argument("--formal", action="store_true",
                    help="正式模式：需 preflight 通过；解锁正式测试集")
    ap.add_argument("--resume", default=None,
                    help="续跑指定 run_id（不重复执行已落盘 task_id）")
    ap.add_argument("--model-key", default=None,
                    help="正式配置含 model_paths 字典时，选择本次运行的模型")
    args = ap.parse_args()

    dataset_path = (ROOT / args.dataset).resolve()
    formal_set = is_formal_dataset(dataset_path)
    # ---- 守卫：正式测试集仅在显式 --formal 且 preflight 通过时解锁 ----
    if formal_set and not args.formal:
        print("[GUARD] 拒绝执行正式测试集：未显式启用 --formal。\n"
              "        正式模式还要求 preflight 全部通过（冻结哈希/模型/后端）。",
              file=sys.stderr)
        return 2

    run_id = args.resume or new_run_id(args.run_label)
    if args.resume and not args.formal:
        print("[GUARD] --resume 仅支持正式运行。", file=sys.stderr)
        return 2
    run_dir = ROOT / "results" / "raw" / run_id
    log_dir = ROOT / "logs" / run_id
    if args.resume:
        if not (run_dir / "result.json").is_file():
            print(f"[GUARD] 续跑目标不存在: {run_dir}", file=sys.stderr)
            return 2
        run_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
    else:
        ensure_run_dir(ROOT / "results" / "raw", run_id)   # 已存在则拒绝
        log_dir.mkdir(parents=True, exist_ok=False)

    log_fh = (log_dir / "run_tasks.log").open("a", encoding="utf-8", buffering=1)
    log = logging.getLogger("run_tasks")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(log_fh), logging.StreamHandler(sys.stdout)):
        h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                                         "%H:%M:%S"))
        log.addHandler(h)

    requests_path = run_dir / "requests.jsonl"
    result = {"run_id": run_id, "script": "scripts/run_tasks.py",
              "dataset": str(dataset_path.relative_to(ROOT)),
              "synthetic": False, "formal_mode": bool(args.formal),
              "resumed_from_record": bool(args.resume),
              "started_at_utc": ctl.utc_now_iso()}
    server = None
    attempted_ids: list = []
    exit_code = 1

    try:
        cfg, e1 = load_json(ROOT / args.config)
        tpl, e2 = load_json(ROOT / args.template)
        data, e3 = load_json(dataset_path)
        errs = [x for x in (e1, e2, e3) if x]
        if errs:
            result.update({"success": False, "failure_stage": "load_inputs",
                           "is_real_inference": False, "requests_sent": 0,
                           "error": "; ".join(errs)})
            raise RuntimeError(result["error"])

        result["config_id"] = cfg.get("config_id", (ROOT / args.config).stem)
        result["config_snapshot"] = cfg
        result["prompt_template"] = {k: tpl[k] for k in
                                     ("template_id", "system_prompt", "per_type")}
        tasks = data["tasks"] if isinstance(data, dict) else data
        result["task_count_requested"] = len(tasks)
        result["dataset_id"] = data.get("dataset_id") if isinstance(data, dict) else None
        all_task_ids = [t["id"] for t in tasks]
        result["all_task_ids"] = all_task_ids
        log.info("数据集 %s 共 %d 条 (config_id=%s, formal=%s)", dataset_path.name,
                 len(tasks), result["config_id"], args.formal)

        # ---- 正式模式：preflight 门禁 ----
        if args.formal:
            import preflight
            pf = preflight.run_checks(verbose=True)
            result["preflight_passed"] = pf["passed"]
            if not pf["passed"]:
                result.update({"success": False,
                               "failure_stage": "preflight",
                               "error": "preflight 未通过，拒绝进入正式模式",
                               "is_real_inference": False, "requests_sent": 0})
                raise RuntimeError(result["error"])
            log.info("preflight 通过，进入正式模式")

        # ---- 计数器（续跑时由已完成记录重建） ----
        counts = {"total": 0, "ok": 0, "request_failed": 0, "timeout": 0,
                  "truncated": 0, "format_correct": 0, "content_correct": 0,
                  "correct": 0}
        per_type = {}

        # ---- 续跑：读取已落盘记录并重建计数（避免汇总遗漏已完成部分） ----
        done_ids, completed_records = [], []
        if args.resume and requests_path.is_file():
            for line in requests_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                completed_records.append(rec)
                if rec.get("task_id"):
                    done_ids.append(rec["task_id"])
            for rec in completed_records:
                counts0 = {"total": 1, "ok": 0, "request_failed": 0,
                           "timeout": 0, "truncated": 0,
                           "format_correct": 0, "content_correct": 0,
                           "correct": 0}
                st = rec.get("status")
                if st in ("request_failed", "timeout"):
                    counts0[st] = 1
                else:
                    counts0["ok"] = 1
                    sc = rec.get("score") or {}
                    counts0["format_correct"] = int(bool(sc.get("format_correct")))
                    counts0["content_correct"] = int(bool(sc.get("content_correct")))
                    counts0["correct"] = int(bool(sc.get("correct")))
                    if rec.get("truncated"):
                        counts0["truncated"] = 1
                for k, v in counts0.items():
                    counts[k] += v
                pt = per_type.setdefault(rec.get("task_type", "unknown"), {
                    "total": 0, "correct": 0, "format_correct": 0,
                    "content_correct": 0, "failed": 0})
                pt["total"] += 1
                sc = rec.get("score") or {}
                pt["correct"] += int(bool(sc.get("correct")))
                pt["format_correct"] += int(bool(sc.get("format_correct")))
                pt["content_correct"] += int(bool(sc.get("content_correct")))
            log.info("续跑: 已有 %d 条记录, 跳过 task_id=%s",
                     len(completed_records), sorted(set(done_ids)))

        model_key = args.model_key or cfg.get("model_key")
        if "model_paths" in cfg:
            if not model_key or model_key not in cfg["model_paths"]:
                raise RuntimeError(
                    f"需要在 --model-key 中指定 {sorted(cfg['model_paths'])} 之一")
            model_rel = cfg["model_paths"][model_key]
        else:
            model_rel = cfg["model_path"]
        model_path = ROOT / model_rel
        result["model_path"] = model_rel
        result["model_key"] = model_key
        if not model_path.is_file():
            raise RuntimeError(f"模型文件不存在: {model_path}")
        server_exe = find_server_exe()
        result["backend_version"] = capture_backend_version(server_exe)

        argv = server_argv_from_config(cfg, model_path)
        result["server_argv"] = argv

        log_srv = (log_dir / "server.log").open("a", encoding="utf-8", buffering=1)
        try:
            server = LlamaServer(server_exe, argv, log_srv, log,
                                 memory_sample_interval_s=cfg.get(
                                     "memory_sample_interval_s"))
            server.start()
            ready = server.wait_ready(cfg["load_timeout_s"],
                                      cfg["health_poll_interval_s"])
            log_text_now = log_srv.name and Path(log_srv.name).read_text(
                encoding="utf-8", errors="replace")
            init_stat = ctl.parse_load_time_from_log(log_text_now)
            result["server_startup"] = {
                "ok": ready["ok"], "error": ready["error"],
                "server_startup_to_ready_s": server.startup_to_ready_s,
                "health_poll_interval_s": cfg["health_poll_interval_s"],
                "warmup": "backend default enabled (未传 --no-warmup)",
                "backend_model_init_interval_s":
                    init_stat["backend_model_init_interval_s"],
                "model_load_time_s": init_stat["model_load_time_s"],
                "model_load_time_reason": init_stat["reason"]}
            if not ready["ok"]:
                raise RuntimeError(f"服务未就绪: {ready['error']}")

            # ---- 每次启动后的预热与缓存检查（不计入正式统计） ----
            warm_payload = {"messages": [{"role": "user", "content": "1"}],
                            "max_tokens": 1, "temperature": 0.0,
                            "cache_prompt": False, "stream": False}
            warm = server.request("/v1/chat/completions", warm_payload,
                                  cfg["request_timeout_s"])
            cc = []
            for i in (1, 2):
                r = server.request("/v1/chat/completions", warm_payload,
                                   cfg["request_timeout_s"])
                b = r.get("body") or {}
                cc.append({"cache_n": (b.get("timings") or {}).get("cache_n"),
                           "prompt_n": (b.get("timings") or {}).get("prompt_n")})
            cache_ok = bool(warm["ok"] and len(cc) == 2
                            and cc[0]["cache_n"] == 0 and cc[1]["cache_n"] == 0
                            and cc[0]["prompt_n"] == cc[1]["prompt_n"]
                            and (cc[0]["prompt_n"] or 0) > 1)
            result["warmup_request"] = {"marked_as": "warmup", "ok": warm["ok"],
                                        "excluded_from_stats": True}
            result["cache_reuse_check"] = {"requests": cc, "passed": cache_ok}
            log.info("预热=%s 缓存校验=%s", warm["ok"], cache_ok)
            if formal_set and not cache_ok:
                raise RuntimeError("正式模式缓存复用校验未通过，拒绝继续")
            warmup_failed = not warm["ok"]

            gen = cfg["generation"]
            requests_sent = len(completed_records)
            perf_all_valid = True
            scoring_errors = 0
            timeout_recoveries = []
            not_attempted = []

            for task in tasks:
                if task["id"] in done_ids:
                    continue
                user_msg = build_user_message(task, tpl)  # 可能因题目缺陷抛错
                attempted_ids.append(task["id"])  # 实际发送请求前才计为已尝试
                counts["total"] += 1
                payload = {"messages": [
                    {"role": "system", "content": tpl["system_prompt"]},
                    {"role": "user", "content": user_msg}],
                    "temperature": gen["temperature"],
                    "top_p": gen.get("top_p", 1.0),
                    "max_tokens": task.get("max_tokens", gen["max_tokens"]),
                    "seed": gen["seed"], "cache_prompt": False,
                    "stream": False}
                rec = {"run_id": run_id, "config_id": result["config_id"],
                       "dataset": result["dataset"],
                       "dataset_id": result.get("dataset_id"),
                       "task_id": task["id"], "task_type": task["task_type"],
                       "input": task["input"], "user_message": user_msg,
                       "generation_params": {k: payload[k] for k in
                                             ("temperature", "top_p", "max_tokens",
                                              "seed", "cache_prompt")},
                       "started_at_utc": ctl.utc_now_iso(),
                       "is_real_inference": True, "synthetic": False}
                r = server.request("/v1/chat/completions", payload,
                                   cfg["request_timeout_s"])
                requests_sent += 1
                rec["end_to_end_client_s"] = r["client_e2e_s"]
                rec["http_status"] = r["http_status"]
                rec["error"] = r["error"]
                rec["error_type"] = r.get("error_type")
                if not r["ok"] and r.get("error_type") == "HTTPError":
                    rec["error_body"] = r.get("error_body")
                body = r.get("body") or {}
                rec["response_body"] = body if r["ok"] else None
                choice = (body.get("choices") or [{}])[0]
                rec["raw_output"] = (choice.get("message") or {}).get("content")
                rec["finish_reason"] = choice.get("finish_reason")
                rec["usage_raw"] = body.get("usage")
                rec["timings_raw"] = body.get("timings")
                # 统一性能解析（Stage 3：有限性/范围/分类有效性）
                rec["performance"] = ctl.extract_perf(
                    rec["timings_raw"], rec["usage_raw"], r["client_e2e_s"])
                if not rec["performance"]["performance_record_valid"]:
                    perf_all_valid = False

                if not r["ok"]:
                    is_timeout = "timed out" in (r["error"] or "").lower() or \
                        "timeout" in (r["error"] or "").lower()
                    rec["status"] = "timeout" if is_timeout else "request_failed"
                    counts[rec["status"]] += 1
                    rec["score"] = {"format_correct": False,
                                    "content_correct": False, "correct": False,
                                    "detail": {"reason": "请求失败/超时, 计入分母"}}
                    log.warning("任务 %s 失败: %s", task["id"], r["error"])
                    if is_timeout:
                        # 超时≠服务端已停止生成：确认 slot 空闲才继续
                        st = slots_idle(server, cfg["timeout_recovery"]["wait_timeout_s"])
                        timeout_recoveries.append({"task_id": task["id"],
                                                   **st})
                        log.warning("超时后 slot 状态确认: %s", st)
                        if not st["freed"]:
                            not_attempted = [t2["id"] for t2 in tasks
                                             if t2["id"] not in attempted_ids
                                             and t2["id"] not in done_ids]
                            log.error("slot 未空闲, 终止服务, 剩余 %d 题标记 "
                                      "not_attempted", len(not_attempted))
                            with requests_path.open("a", encoding="utf-8") as f:
                                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                            raise RuntimeError(
                                "超时后服务未恢复空闲，按预案终止本次运行")
                else:
                    counts["ok"] += 1
                    truncated = choice.get("finish_reason") == "length"
                    rec["truncated"] = truncated
                    if truncated:
                        counts["truncated"] += 1
                        log.warning("任务 %s 截断(标记, 不重试)", task["id"])
                    try:
                        sc = score_item(task, rec["raw_output"] or "")
                    except Exception as e:  # noqa: BLE001 —— 评分异常按失败计
                        scoring_errors += 1
                        sc = {"format_correct": False, "content_correct": False,
                              "correct": False,
                              "detail": {"scoring_error": repr(e)}}
                        log.error("任务 %s 评分异常: %r", task["id"], e)
                    sc["truncated_output"] = truncated
                    rec["score"] = sc
                    counts["format_correct"] += int(sc["format_correct"])
                    counts["content_correct"] += int(sc["content_correct"])
                    counts["correct"] += int(sc["correct"])
                    pt = per_type.setdefault(task["task_type"], {
                        "total": 0, "correct": 0, "format_correct": 0,
                        "content_correct": 0, "failed": 0})
                    pt["total"] += 1
                    pt["correct"] += int(sc["correct"])
                    pt["format_correct"] += int(sc["format_correct"])
                    pt["content_correct"] += int(sc["content_correct"])
                    if not r["ok"]:
                        pt["failed"] += 1
                    log.info("[%d/%d] %s -> %r | 格式=%s 内容=%s 联合=%s",
                             counts["total"], len(tasks), task["id"],
                             (rec["raw_output"] or "")[:40],
                             sc["format_correct"], sc["content_correct"],
                             sc["correct"])

                rec["finished_at_utc"] = ctl.utc_now_iso()
                with requests_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            # ---- 退出前采集内存 ----
            result["memory"] = server.memory_stats()
            result["requests_sent"] = requests_sent
            result["counts"] = counts
            result["per_task_type"] = per_type
            result["timeout_recoveries"] = timeout_recoveries
            attempted_all = (counts["total"] + len(done_ids)) == len(tasks)
            result["not_attempted"] = not_attempted if not_attempted else []
            result["execution_complete"] = bool(attempted_all and not not_attempted)
            # 四态判定（quality/performance 与执行成功分开）
            result["quality_evaluation_valid"] = bool(
                result["execution_complete"] and scoring_errors == 0
                and not warmup_failed)
            result["performance_record_valid"] = bool(perf_all_valid)
            result["denominator_note"] = ("分母=全部请求(含失败/超时/截断/格式错误); "
                                          "无剔除无重试; 未完成整套任务时不得宣布"
                                          "完整正式正确率")
        finally:
            if server is not None:
                if "memory" not in result:
                    result["memory"] = server.memory_stats()  # 退出前尽力采集
                result["cleanup"] = server.stop()
            log_srv.close()
            # 清理只针对本程序启动的 PID；全局同名进程数仅作环境记录
            result["orphan_servers_global_after"] = count_orphan_servers()
            result["cleanup_success"] = bool(
                result.get("cleanup", {}).get("confirmed_exit"))
        # 总体 success：预注册组合（执行完整 ∧ 清理成功；正式模式还需 preflight）
        result["success"] = compose_run_success(
            result.get("execution_complete", False),
            result.get("cleanup_success", False),
            formal=formal_set,
            preflight_passed=result.get("preflight_passed", False))
        result["success_rule"] = ("execution_complete AND cleanup_success; "
                                  "quality_evaluation_valid 与 "
                                  "performance_record_valid 单列报告")
        exit_code = 0 if result.get("success") else 1

    except KeyboardInterrupt:
        result["success"] = False
        result["error"] = "用户中断; 已完成请求已逐条落盘"
        result["is_real_inference"] = result.get("requests_sent", 0) > 0
        exit_code = 130
    except Exception as exc:  # noqa: BLE001
        log.exception("run_tasks 失败")
        result["success"] = False
        result.setdefault("error", f"{type(exc).__name__}: {exc}")
        result.setdefault("failure_stage", "runtime")
        # 中断/失败时：已完成请求保留，剩余任务标记 not_attempted
        all_ids = result.get("all_task_ids") or []
        result["not_attempted"] = [t for t in all_ids
                                   if t not in attempted_ids]
        result["execution_complete"] = False  # 失败/中断路径一律不完整
        # 尚未发生任何真实请求的失败不是"已发生真实推理"
        result.setdefault("is_real_inference",
                          result.get("requests_sent", 0) > 0)
        result.setdefault("requests_sent", 0)
        exit_code = 1
    finally:
        result["finished_at_utc"] = ctl.utc_now_iso()
        result.setdefault("is_real_inference",
                          result.get("requests_sent", 0) > 0)
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("结果: %s (success=%s)", run_dir / "result.json",
                 result.get("success"))
        log_fh.flush()
        log_fh.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
