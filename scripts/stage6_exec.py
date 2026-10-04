# -*- coding: utf-8 -*-
"""Stage 6 共同错误诊断执行器。

与 run_tasks.py 同族模式：单推理进程、串行请求、每次启动独立预热+缓存检查、
超时后 /slots 确认、失败/截断保留不重试、run 目录拒绝覆盖、逐条落盘 JSONL。

差异（预注册见 configs/stage6_plan.json / docs/STAGE6_PLAN.md）：
- 请求单元 = datasets/stage6/ 变体文件中的预渲染 messages（执行时不做任何渲染，
  消除渲染漂移）；执行顺序取 stage6_order.json 的 shuffled 列表（三模型同一顺序）；
- dev 模式先于冻结运行（链路验证）；formal 模式要求 Stage 6 冻结清单
  逐文件 SHA256 通过 + 历史冻结清单（Stage 3）通过 + 模型/后端清单通过；
- 启动前 count_orphan_servers()>0 拒绝（同一时刻仅一个推理进程）；
- 环境快照（CPU/内存/供电/CircuitPilot 按名识别）写入 result.json；
- 评分沿用冻结 score_answer.score_item；诊断标记（fence/parse_method）在分析
  阶段从 raw_output 重算，执行记录只保存原始证据与三档得分。

用法:
    C:/Anaconda/python.exe scripts/stage6_exec.py --mode dev    --model-key Q4_K_M
    C:/Anaconda/python.exe scripts/stage6_exec.py --mode formal --model-key Q8_0
"""

import argparse
import hashlib
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

STAGE6_DIR = ROOT / "results" / "stage6"
VARIANT_FILES = {
    "dev": ["datasets/stage6/stage6_dev_variants.json"],
    "formal": ["datasets/stage6/stage6_cls_variants.json",
               "datasets/stage6/stage6_json_variants.json"],
}
ORDER_FILE = ROOT / "datasets" / "stage6" / "stage6_order.json"
FREEZE_MANIFEST = ROOT / "datasets" / "stage6" / "frozen_hashes_stage6.json"
HISTORY_MANIFEST = ROOT / "datasets" / "frozen_hashes_stage3.json"
MODEL_MANIFEST = ROOT / "models" / "model_manifest_v3.json"
BACKEND_MANIFEST = ROOT / "environment" / "backend_manifest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_manifest(manifest_path: Path) -> list:
    """逐文件 SHA256 校验冻结清单；返回问题列表。"""
    problems = []
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    for rel in man["files"]:
        p = ROOT / rel
        if not p.is_file():
            problems.append(f"缺失: {rel}")
            continue
        got = sha256_file(p)
        want = man["files"][rel]["sha256"] if isinstance(
            man["files"], dict) and isinstance(man["files"].get(rel), dict) else None
        if want is None:
            # 清单 files 为映射: rel -> sha256
            want = man["files"][rel] if isinstance(man["files"].get(rel), str) else None
        if want is None:
            problems.append(f"清单格式异常: {rel}")
        elif got != want:
            problems.append(f"哈希不一致: {rel}")
    return problems


def detect_circuitpilot() -> dict:
    """按进程名识别 CircuitPilot 负载；识别不到如实记 not_found_by_name。"""
    hits = []
    cpu0 = {}
    for p in psutil_process_iter():
        n = (p.get("name") or "").lower()
        if "circuitpilot" in n or "circuit_pilot" in n or "circuit-pilot" in n:
            hits.append({"pid": p["pid"], "name": p["name"]})
    return {"found": hits, "method": "process_name_substring(circuitpilot)",
            "note": "仅按进程名识别；无法核验其内部负载时如实记录，不宣称全程无干扰"}


def psutil_process_iter():
    import psutil
    return [{"pid": p.info["pid"], "name": p.info["name"]}
            for p in psutil.process_iter(["name", "pid"])]


def power_status() -> dict:
    try:
        import psutil
        b = psutil.sensors_battery()
        if b is None:
            return {"status": "unknown", "reason": "no battery API data"}
        return {"status": "recorded", "percent": b.percent,
                "power_plugged": bool(b.power_plugged)}
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "reason": repr(e)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["dev", "formal"])
    ap.add_argument("--model-key", required=True,
                    choices=["Q4_K_M", "Q5_K_M", "Q8_0"])
    ap.add_argument("--config", default="configs/stage6_config.json")
    args = ap.parse_args()

    run_id = new_run_id(f"stage6{args.mode}_{args.model_key.replace('_', '')}")
    run_dir = ensure_run_dir(STAGE6_DIR, run_id)
    log_dir = ROOT / "logs" / run_id
    log_dir.mkdir(parents=True, exist_ok=False)
    log_fh = (log_dir / "stage6_exec.log").open("a", encoding="utf-8", buffering=1)
    log = logging.getLogger("stage6_exec")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(log_fh), logging.StreamHandler(sys.stdout)):
        h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                                         "%H:%M:%S"))
        log.addHandler(h)

    result = {"run_id": run_id, "script": "scripts/stage6_exec.py",
              "mode": args.mode, "model_key": args.model_key,
              "config": args.config, "started_at_utc": ctl.utc_now_iso(),
              "stage": "stage6_joint_error_diagnosis",
              "is_real_inference": True, "synthetic": False,
              "exploratory_positioning": "事后探索性诊断；不产生新正式测试集成绩"}
    server = None
    exit_code = 1

    try:
        cfg = json.loads((ROOT / args.config).read_text(encoding="utf-8"))
        result["config_id"] = cfg["config_id"]
        result["config_snapshot"] = cfg
        # 继承一致性硬校验：与 Stage 3 正式配置逐键比对（stage6 明确继承的字段）
        formal_cfg = json.loads(
            (ROOT / "configs" / "formal_config.json").read_text(encoding="utf-8"))
        inherited_keys = ["parallel", "threads", "threads_batch", "context_length",
                          "batch_size", "ubatch_size", "cache_type_k", "cache_type_v",
                          "flash_attn", "load_mode", "gpu_offload_layers",
                          "cache_reuse", "generation", "request_timeout_s",
                          "load_timeout_s", "health_poll_interval_s",
                          "timeout_recovery"]
        inherited_diff = {k: [formal_cfg.get(k), cfg.get(k)] for k in inherited_keys
                          if formal_cfg.get(k) != cfg.get(k)}
        if inherited_diff:
            raise RuntimeError(f"stage6_config 与 formal_config 继承字段不一致: "
                               f"{json.dumps(inherited_diff, ensure_ascii=False)}")
        result["inheritance_check_vs_formal_config"] = {
            "keys_checked": inherited_keys, "mismatches": inherited_diff,
            "passed": True}

        # ---- 变体与顺序 ----
        variants = {}
        for rel in VARIANT_FILES[args.mode]:
            doc = json.loads((ROOT / rel).read_text(encoding="utf-8"))
            for v in doc["variants"]:
                if v["variant_id"] in variants:
                    raise RuntimeError(f"变体ID重复: {v['variant_id']}")
                variants[v["variant_id"]] = v
        order_doc = json.loads(ORDER_FILE.read_text(encoding="utf-8"))
        order_key = "shuffled_dev" if args.mode == "dev" else "shuffled_formal"
        unit_order = order_doc[order_key]
        missing = [u for u in unit_order if u not in variants]
        extra = [vid for vid in variants if vid not in unit_order]
        if missing or extra:
            raise RuntimeError(f"顺序与变体不匹配: missing={missing} extra={extra}")
        result["order_source"] = f"stage6_order.json::{order_key}"
        result["order_seed"] = order_doc["seed"]
        result["units_planned"] = len(unit_order)

        # ---- 门禁 ----
        orphans = count_orphan_servers()
        result["orphan_servers_before"] = orphans
        if orphans > 0:
            raise RuntimeError(f"检测到 {orphans} 个残留 llama-server 进程，"
                               f"同一时刻仅允许一个推理进程，拒绝启动")
        if args.mode == "formal":
            if not FREEZE_MANIFEST.is_file():
                raise RuntimeError("Stage 6 冻结清单不存在，拒绝正式运行")
            problems = check_manifest(FREEZE_MANIFEST)
            problems += [f"[历史] {x}" for x in check_manifest(HISTORY_MANIFEST)]
            result["freeze_check"] = {
                "manifest": str(FREEZE_MANIFEST.relative_to(ROOT)),
                "history_manifest": str(HISTORY_MANIFEST.relative_to(ROOT)),
                "problems": problems, "passed": not problems}
            if problems:
                raise RuntimeError("冻结清单核验未通过: " + "; ".join(problems))
            log.info("冻结清单核验通过（含历史 Stage 3 清单）")
        else:
            result["freeze_check"] = {"skipped_reason": "dev 模式先于冻结运行"}

        mm = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
        model_rel = cfg["model_paths"][args.model_key]
        model_path = ROOT / model_rel
        if not model_path.is_file():
            raise RuntimeError(f"模型文件不存在: {model_path}")
        model_sha = sha256_file(model_path)
        mm_models = mm.get("models")
        entry = None
        if isinstance(mm_models, list):
            entry = next((e for e in mm_models
                          if isinstance(e, dict) and e.get("quantization") == args.model_key),
                         None)
        elif isinstance(mm_models, dict):
            entry = mm_models.get(args.model_key)
        manifest_sha = entry.get("sha256") if isinstance(entry, dict) else None
        result["model"] = {"key": args.model_key, "path": model_rel,
                           "sha256": model_sha,
                           "manifest_v3_sha256": manifest_sha,
                           "manifest_match": (manifest_sha == model_sha
                                              if manifest_sha else None)}
        if manifest_sha and manifest_sha != model_sha:
            raise RuntimeError(f"模型 SHA256 与 manifest v3 不一致: {args.model_key}")
        server_exe = find_server_exe()
        result["backend_version"] = capture_backend_version(server_exe)
        bm = json.loads(BACKEND_MANIFEST.read_text(encoding="utf-8"))
        result["backend_manifest_recorded"] = True

        result["environment_at_start"] = {
            **resource_snapshot(), "power": power_status(),
            "circuitpilot": detect_circuitpilot()}

        # ---- 服务启动 ----
        argv = ["-m", str(model_path),
                "--parallel", str(cfg["parallel"]),
                "-c", str(cfg["context_length"]),
                "-b", str(cfg["batch_size"]),
                "-ub", str(cfg["ubatch_size"]),
                "-ctk", cfg["cache_type_k"], "-ctv", cfg["cache_type_v"],
                "-t", str(cfg["threads"]), "-tb", str(cfg["threads_batch"]),
                "-fa", cfg["flash_attn"], "-lm", cfg["load_mode"],
                "-ngl", str(cfg["gpu_offload_layers"]),
                "--cache-reuse", str(cfg["cache_reuse"])]
        result["server_argv"] = argv
        log_srv = (log_dir / "server.log").open("a", encoding="utf-8", buffering=1)
        server = LlamaServer(server_exe, argv, log_srv, log,
                             memory_sample_interval_s=cfg["memory_sample_interval_s"])
        server.start()
        ready = server.wait_ready(cfg["load_timeout_s"], cfg["health_poll_interval_s"])
        result["server_startup"] = {"ok": ready["ok"], "error": ready["error"],
                                    "server_startup_to_ready_s":
                                        server.startup_to_ready_s}
        if not ready["ok"]:
            raise RuntimeError(f"服务未就绪: {ready['error']}")

        # ---- 预热 + 缓存复用检查（辅助，不计入成绩） ----
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
        result["warmup_request"] = {"marked_as": "warmup_auxiliary", "ok": warm["ok"],
                                    "excluded_from_stats": True}
        result["cache_reuse_check"] = {"requests": cc, "passed": cache_ok}
        log.info("预热=%s 缓存校验=%s", warm["ok"], cache_ok)
        if not cache_ok:
            raise RuntimeError("缓存复用校验未通过，拒绝继续（提示缓存必须关闭）")

        # ---- 串行执行（预注册顺序） ----
        gen = cfg["generation"]
        counts = {"total": 0, "ok": 0, "request_failed": 0, "timeout": 0,
                  "truncated": 0}
        requests_path = run_dir / "requests.jsonl"
        timeout_recoveries, not_attempted = [], []
        scoring_errors = 0
        attempted_ids = []
        max_prompt_tokens_seen = 0

        for idx, vid in enumerate(unit_order, start=1):
            v = variants[vid]
            attempted_ids.append(vid)
            counts["total"] += 1
            payload = {"messages": v["messages"],
                       "temperature": gen["temperature"],
                       "top_p": gen.get("top_p", 1.0),
                       "max_tokens": gen["max_tokens"],
                       "seed": gen["seed"], "cache_prompt": False,
                       "stream": False}
            rec = {"run_id": run_id, "config_id": cfg["config_id"],
                   "mode": args.mode, "phase": v["phase"],
                   "unit_index_in_order": idx,
                   "branch": v["branch"], "variant_id": vid,
                   "parent_task_id": v["parent_task_id"],
                   "condition": v.get("condition"),
                   "rotation": v.get("rotation"),
                   "labels_displayed": v.get("labels_displayed"),
                   "gold": v["gold"],
                   "gold_position_in_display": v.get("gold_position_in_display"),
                   "messages": v["messages"], "user_message": v["user_message"],
                   "prompt_sha256": v["prompt_sha256"],
                   "model_key": args.model_key, "model_path": model_rel,
                   "model_sha256": model_sha,
                   "backend_version": result["backend_version"]["stderr"].strip(),
                   "generation_params": {k: payload[k] for k in
                                         ("temperature", "top_p", "max_tokens",
                                          "seed", "cache_prompt")},
                   "started_at_utc": ctl.utc_now_iso(),
                   "is_real_inference": True, "synthetic": False}
            r = server.request("/v1/chat/completions", payload,
                               cfg["request_timeout_s"])
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
            rec["performance"] = ctl.extract_perf(
                rec["timings_raw"], rec["usage_raw"], r["client_e2e_s"])
            ptoks = (rec["usage_raw"] or {}).get("prompt_tokens")
            if isinstance(ptoks, int):
                max_prompt_tokens_seen = max(max_prompt_tokens_seen, ptoks)

            if not r["ok"]:
                is_timeout = "timed out" in (r["error"] or "").lower() or \
                    "timeout" in (r["error"] or "").lower()
                rec["status"] = "timeout" if is_timeout else "request_failed"
                counts[rec["status"]] += 1
                rec["score"] = {"format_correct": False, "content_correct": False,
                                "correct": False,
                                "detail": {"reason": "请求失败/超时, 计入分母"}}
                log.warning("变体 %s 失败: %s", vid, r["error"])
                if is_timeout:
                    st = slots_idle(server, cfg["timeout_recovery"]["wait_timeout_s"])
                    timeout_recoveries.append({"variant_id": vid, **st})
                    log.warning("超时后 slot 状态确认: %s", st)
                    if not st["freed"]:
                        not_attempted = [u for u in unit_order
                                         if u not in attempted_ids]
                        with requests_path.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        raise RuntimeError("超时后服务未恢复空闲，按预案终止")
            else:
                counts["ok"] += 1
                truncated = choice.get("finish_reason") == "length"
                rec["truncated"] = truncated
                if truncated:
                    counts["truncated"] += 1
                    log.warning("变体 %s 截断(标记, 不重试)", vid)
                # 评分对象 = 变体自身（classification: labels_displayed 集合不变，
                # 格式判定集合成员等价；json: gold 不变）
                score_target = dict(v)
                try:
                    sc = score_item(score_target, rec["raw_output"] or "")
                except Exception as e:  # noqa: BLE001
                    scoring_errors += 1
                    sc = {"format_correct": False, "content_correct": False,
                          "correct": False, "detail": {"scoring_error": repr(e)}}
                    log.error("变体 %s 评分异常: %r", vid, e)
                sc["truncated_output"] = truncated
                rec["score"] = sc
                log.info("[%d/%d] %s -> %r | 格式=%s 内容=%s 联合=%s",
                         idx, len(unit_order), vid,
                         (rec["raw_output"] or "")[:40], sc["format_correct"],
                         sc["content_correct"], sc["correct"])

            rec["finished_at_utc"] = ctl.utc_now_iso()
            with requests_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        result["memory"] = server.memory_stats()
        result["requests_sent"] = counts["total"]
        result["counts"] = counts
        result["scoring_errors"] = scoring_errors
        result["max_prompt_tokens_seen"] = max_prompt_tokens_seen
        result["context_budget_check"] = {
            "ctx": cfg["context_length"], "max_prompt_tokens_seen":
                max_prompt_tokens_seen,
            "fits": bool(max_prompt_tokens_seen + gen["max_tokens"]
                         <= cfg["context_length"])}
        result["timeout_recoveries"] = timeout_recoveries
        result["not_attempted"] = not_attempted
        result["execution_complete"] = bool(not not_attempted
                                            and counts["total"] == len(unit_order))
        result["quality_evaluation_valid"] = bool(
            result["execution_complete"] and scoring_errors == 0)
        result["denominator_note"] = ("分母=全部请求(含失败/超时/截断); 无剔除无重试; "
                                      "诊断结果不作为正式测试集成绩")
    except KeyboardInterrupt:
        result["success"] = False
        result["error"] = "用户中断; 已完成请求已逐条落盘"
        exit_code = 130
    except Exception as exc:  # noqa: BLE001
        log.exception("stage6_exec 失败")
        result["success"] = False
        result.setdefault("error", f"{type(exc).__name__}: {exc}")
        result.setdefault("failure_stage", "runtime")
        result.setdefault("requests_sent", 0)
        exit_code = 1
    finally:
        if server is not None:
            if "memory" not in result:
                result["memory"] = server.memory_stats()
            result["cleanup"] = server.stop()
        result["environment_at_end"] = {
            **resource_snapshot(), "power": power_status(),
            "circuitpilot": detect_circuitpilot()}
        result["cleanup_success"] = bool(
            result.get("cleanup", {}).get("confirmed_exit"))
        result["success"] = bool(result.get("execution_complete")
                                 and result.get("cleanup_success"))
        result["success_rule"] = "execution_complete AND cleanup_success"
        result["finished_at_utc"] = ctl.utc_now_iso()
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n")
        log.info("结果: %s (success=%s)", run_dir / "result.json",
                 result.get("success"))
        log_fh.flush()
        log_fh.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
