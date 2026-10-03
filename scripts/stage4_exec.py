# -*- coding: utf-8 -*-
"""Stage 4 执行器：Q4_K_M 线程数实验（矩阵 A bench / 矩阵 B HTTP）。

保障机制（docs/STAGE4_PLAN.md §执行与恢复保障）：
- 单元级验证：预热/缓存检查、预期数量、HTTP 状态、输出结构、性能有效性、
  清理确认分别记录；不无条件写 unit.ok / cleanup_success；
- 严格 /slots：空列表、结构异常、解析失败一律不算"确认空闲"；
- bench 超时/中断保留已有 stdout/stderr；
- 真 --resume：恢复前核对冻结清单/模型/依赖指纹；只跳过 marker 存在且
  execution_complete+metrics_valid+cleanup_success 全真的单元；不完整单元
  保留 *_partial.json 证据后整单元重跑；汇总从落盘记录重建，不用 0 填充；
- 每个 run 保存计划/配置/冻结清单快照与哈希、实际 argv、工具实时版本、
  模型 SHA256。

用法:
    python scripts/stage4_exec.py --matrix bench
    python scripts/stage4_exec.py --matrix http
    python scripts/stage4_exec.py --matrix http --resume <run_id>
"""

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import llama_server_ctl as ctl
from llama_server_ctl import (LlamaServer, capture_backend_version,
                              ensure_run_dir, find_server_exe, new_run_id,
                              resource_snapshot)
from run_tasks import build_user_message

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools" / "llama.cpp"
FROZEN_MANIFEST = ROOT / "datasets" / "frozen_hashes_stage4.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def strict_slots_idle(server: LlamaServer, timeout_s: float,
                      poll_s: float = 0.5) -> dict:
    """Stage 4 严格版：空列表/结构异常/解析失败一律不算确认空闲。

    只有拿到非空的 slot 列表、每个元素含 is_processing 键且全部为 False，
    才返回 freed=True。
    """
    deadline = time.perf_counter() + timeout_s
    last = None
    while time.perf_counter() < deadline:
        r = server.request("/slots", None, timeout_s=5)
        body = r.get("body")
        if r.get("ok") and isinstance(body, list) and body:
            if all(isinstance(s, dict) and "is_processing" in s for s in body):
                busy = any(s.get("is_processing") for s in body)
                last = {"slots": len(body), "busy": busy, "wellformed": True}
                if not busy:
                    return {"freed": True, "state": last,
                            "action": "confirmed_idle"}
            else:
                last = {"wellformed": False, "shape": "bad_elements"}
        else:
            last = {"wellformed": False,
                    "shape": ("empty_list" if body == [] else
                              "not_a_list" if not r.get("ok") else "empty_body"),
                    "http_status": r.get("http_status")}
        time.sleep(poll_s)
    return {"freed": False, "state": last, "action": "not_confirmed"}


def verify_fingerprints(verbose: bool = False) -> dict:
    """恢复前指纹核对：冻结清单文件 + 后端实现文件 + 模型。"""
    manifest = json.loads(FROZEN_MANIFEST.read_text(encoding="utf-8"))
    problems = []
    for rel, expected in manifest.get("files", {}).items():
        p = ROOT / rel
        if not p.is_file() or sha256(p) != expected:
            problems.append(rel)
    for rel, meta in manifest.get("tools_inventory", {}).items():
        p = TOOLS / rel
        if not p.is_file() or sha256(p) != meta["sha256"]:
            problems.append(f"tools/{rel}")
    man3 = json.loads((ROOT / "models/model_manifest_v3.json")
                      .read_text(encoding="utf-8"))
    q4 = next(m for m in man3["models"] if m["quantization"] == "Q4_K_M")
    if sha256(ROOT / q4["path"]) != q4["sha256"]:
        problems.append(q4["path"])
    return {"ok": not problems, "problems": problems}


def snapshot_files(run_dir: Path) -> dict:
    """把计划/配置/冻结清单快照进 run 目录并记录哈希。"""
    snaps = {}
    for rel in ("configs/stage4_plan.json", "configs/stage4_config.json",
                "datasets/frozen_hashes_stage4.json",
                "models/model_manifest_v3.json"):
        src = ROOT / rel
        dst = run_dir / ("snapshot_" + Path(rel).name)
        shutil.copyfile(src, dst)
        snaps[rel] = {"snapshot": dst.name, "sha256": sha256(src)}
    return snaps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", choices=["bench", "http"], required=True)
    ap.add_argument("--resume", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    log = logging.getLogger(f"stage4_{args.matrix}")

    # ---- Stage 4 preflight（深度核验） ----
    pf = subprocess.run([sys.executable, str(ROOT / "scripts/stage4_preflight.py")],
                        capture_output=True, text=True, encoding="utf-8")
    print(pf.stdout[-1500:] if pf.stdout else pf.stderr[-800:])
    if pf.returncode != 0:
        log.error("stage4 preflight 未通过，拒绝执行")
        return 1

    plan = json.loads((ROOT / "configs/stage4_plan.json").read_text(encoding="utf-8"))
    cfg = json.loads((ROOT / "configs/stage4_config.json").read_text(encoding="utf-8"))

    if args.resume:
        run_id = args.resume
        run_dir = ROOT / "results" / "raw" / run_id
        if not (run_dir / "result.json").is_file():
            log.error("恢复目标不存在: %s", run_dir)
            return 1
        log_dir = ROOT / "logs" / run_id
        log_dir.mkdir(parents=True, exist_ok=True)
        fp = verify_fingerprints()
        if not fp["ok"]:
            log.error("指纹核对失败，拒绝恢复: %s", fp["problems"])
            return 1
        log.info("恢复 %s：指纹核对通过（模型/配置/计划/依赖）", run_id)
    else:
        run_id = new_run_id(f"stage4{args.matrix}")
        run_dir = ensure_run_dir(ROOT / "results" / "raw", run_id)
        log_dir = ROOT / "logs" / run_id
        log_dir.mkdir(parents=True, exist_ok=False)
        fp = {"ok": True, "problems": []}

    result = {"run_id": run_id, "matrix": args.matrix,
              "plan_id": plan["plan_id"], "config_id": cfg["config_id"],
              "is_real_inference": True, "synthetic": False,
              "resumed": bool(args.resume),
              "fingerprint_check": fp,
              "snapshots": snapshot_files(run_dir),
              "units": [], "started_at_utc": ctl.utc_now_iso()}
    requests_path = run_dir / "requests_threads.jsonl"
    exit_code = 1
    server = None

    try:
        model_path = ROOT / cfg["model_path"]
        result["model_sha256"] = sha256(model_path)
        result["tool_versions"] = {
            "llama-server": capture_backend_version(TOOLS / "llama-server.exe"),
            "llama-bench": capture_backend_version(TOOLS / "llama-bench.exe")}
        done_units = set()
        if args.resume:
            for m in run_dir.glob("unit_*.json"):
                if "_partial" in m.name:
                    continue
                d = json.loads(m.read_text(encoding="utf-8"))
                if d.get("execution_complete") and d.get("metrics_valid") \
                        and d.get("cleanup_success"):
                    done_units.add(d["unit_key"])
            log.info("已完成单元 %d 个: %s", len(done_units), sorted(done_units))

        unit_no = 0
        expected_units = 30
        expected_per = {"bench": 3, "http": 5}[args.matrix]
        aux_total = 0
        timed_total = 0
        try:
            for round_def in plan["round_orders"]:
                rnd = round_def["round"]
                for tier in round_def["tier_order"]:
                    unit_no += 1
                    unit_key = f"r{rnd}_t{tier}"
                    marker = run_dir / f"unit_{unit_no:02d}_{unit_key}.json"
                    if unit_key in done_units:
                        log.info("[跳过已完成单元 %02d: 轮%d T=%d]",
                                 unit_no, rnd, tier)
                        result["units"].append({"unit_key": unit_key,
                                                "skipped_resumed": True})
                        continue
                    snap = resource_snapshot()
                    unit = {"unit": unit_no, "unit_key": unit_key,
                            "round": rnd, "tier": tier,
                            "resource_before": snap}
                    log.info("[%d/30] 轮%d T=%d", unit_no, rnd, tier)

                    if args.matrix == "bench":
                        bench = sorted(TOOLS.rglob("llama-bench.exe"))[0]
                        bf = cfg["bench_flags"]
                        argv = [str(bench), "-m", str(model_path),
                                "-ngl", str(bf["ngl"]), "-t", str(tier),
                                "-fa", bf["fa"],
                                "-p", str(bf["p"]), "-n", str(bf["n"]),
                                "-pg", f"{bf['pg'][0]},{bf['pg'][1]}",
                                "-d", str(bf["d"]), "-r", str(bf["r"]),
                                "-o", "json", "-oe", "json", "--progress"]
                        unit["argv"] = argv
                        stdout_f = run_dir / f"bench_{unit_no:02d}_{unit_key}_stdout.txt"
                        stderr_f = run_dir / f"bench_{unit_no:02d}_{unit_key}_stderr.txt"
                        try:
                            proc = subprocess.run(
                                argv, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=1800)
                            stdout_f.write_text(proc.stdout or "", encoding="utf-8")
                            stderr_f.write_text(proc.stderr or "", encoding="utf-8")
                            unit["exit_code"] = proc.returncode
                            unit["stdout_file"] = stdout_f.name
                            unit["stderr_file"] = stderr_f.name
                            tests = []
                            try:
                                parsed = json.loads(
                                    proc.stdout[proc.stdout.index("["):])
                                for t in parsed:
                                    if t.get("n_prompt") and t.get("n_gen"):
                                        wl = "pg256+64"
                                    elif t.get("n_prompt"):
                                        wl = "pp256"
                                    else:
                                        wl = "tg64"
                                    v = t.get("samples_ts", [None])[0]
                                    valid = (isinstance(v, (int, float))
                                             and v == v and 0 < v < float("inf"))
                                    tests.append({"workload": wl, "tok_s": v,
                                                  "valid": valid,
                                                  "n_prompt": t.get("n_prompt"),
                                                  "n_gen": t.get("n_gen")})
                            except (ValueError, json.JSONDecodeError):
                                parsed = None
                            unit["tests"] = tests
                            unit["tests_valid_count"] = sum(
                                1 for t in tests if t["valid"])
                            unit["execution_complete"] = (
                                proc.returncode == 0
                                and unit["tests_valid_count"] == expected_per)
                            unit["metrics_valid"] = unit["execution_complete"]
                            unit["cleanup_success"] = True  # 无常驻进程，bench 已退出
                            if proc.returncode != 0:
                                unit["error"] = (
                                    f"bench 退出码 {proc.returncode}; "
                                    f"stderr: {(proc.stderr or '')[:300]}")
                        except subprocess.TimeoutExpired as e:
                            # bench 超时/中断：保留已有输出
                            (stdout_f.write_text((e.stdout or b"").decode(
                                "utf-8", "replace")) if e.stdout else None)
                            (stderr_f.write_text((e.stderr or b"").decode(
                                "utf-8", "replace")) if e.stderr else None)
                            unit["error"] = "bench 超时(1800s), 已保留输出文件"
                            unit["execution_complete"] = False
                            unit["metrics_valid"] = False
                            unit["cleanup_success"] = True
                    else:
                        log_srv = (log_dir / f"server_{unit_no:02d}_{unit_key}.log") \
                            .open("a", encoding="utf-8", buffering=1)
                        try:
                            argv = ["-m", str(model_path),
                                    "--parallel", "1",
                                    "-c", str(cfg["context_length"]),
                                    "-b", str(cfg["batch_size"]),
                                    "-ub", str(cfg["ubatch_size"]),
                                    "-ctk", cfg["cache_type_k"],
                                    "-ctv", cfg["cache_type_v"],
                                    "-t", str(tier), "-tb", str(tier),
                                    "-fa", cfg["flash_attn"],
                                    "-lm", cfg["load_mode"],
                                    "-ngl", str(cfg["gpu_offload_layers"]),
                                    "--cache-reuse", str(cfg["cache_reuse"])]
                            unit["argv"] = argv
                            server = LlamaServer(
                                find_server_exe(), argv, log_srv, log,
                                memory_sample_interval_s=cfg.get(
                                    "memory_sample_interval_s"))
                            server.start()
                            ready = server.wait_ready(
                                cfg["load_timeout_s"],
                                cfg["health_poll_interval_s"])
                            log_text = Path(log_srv.name).read_text(
                                encoding="utf-8", errors="replace")
                            init_stat = ctl.parse_load_time_from_log(log_text)
                            unit["server_startup"] = {
                                "ok": ready["ok"], "error": ready["error"],
                                "server_startup_to_ready_s":
                                    server.startup_to_ready_s,
                                "backend_model_init_interval_s":
                                    init_stat["backend_model_init_interval_s"],
                                "model_load_time_s":
                                    init_stat["model_load_time_s"]}
                            if not ready["ok"]:
                                raise RuntimeError(f"服务未就绪: {ready['error']}")
                            # 预热 + 缓存检查（另计）
                            warm_payload = {"messages": [
                                {"role": "user", "content": "1"}],
                                "max_tokens": 1, "temperature": 0.0,
                                "cache_prompt": False, "stream": False}
                            warm = server.request("/v1/chat/completions",
                                                  warm_payload,
                                                  cfg["request_timeout_s"])
                            cc = []
                            for _ in (1, 2):
                                rr = server.request("/v1/chat/completions",
                                                    warm_payload,
                                                    cfg["request_timeout_s"])
                                b = rr.get("body") or {}
                                cc.append({
                                    "cache_n": (b.get("timings") or {})
                                    .get("cache_n"),
                                    "prompt_n": (b.get("timings") or {})
                                    .get("prompt_n")})
                            cache_ok = bool(
                                warm["ok"] and cc[0]["cache_n"] == 0
                                and cc[1]["cache_n"] == 0
                                and cc[0]["prompt_n"] == cc[1]["prompt_n"]
                                and (cc[0]["prompt_n"] or 0) > 1)
                            aux_total += 3
                            unit["warmup_and_cache"] = {
                                "warmup_ok": warm["ok"], "cache_check": cc,
                                "cache_ok": cache_ok,
                                "excluded_from_timed_total": True}
                            if not (warm["ok"] and cache_ok):
                                raise RuntimeError("预热/缓存校验失败")

                            dev = json.loads((ROOT / "datasets/dev/dev_tasks.json")
                                             .read_text(encoding="utf-8"))
                            tpl = json.loads(
                                (ROOT / "datasets/prompt_template.json")
                                .read_text(encoding="utf-8"))
                            tasks = [t for t in dev["tasks"]
                                     if t["id"] in plan["matrix_B_http"]
                                     ["task_order_fixed"]]
                            gen = cfg["generation"]
                            sent = 0
                            all_valid = True
                            not_attempted = []
                            for task in tasks:
                                payload = {"messages": [
                                    {"role": "system",
                                     "content": tpl["system_prompt"]},
                                    {"role": "user",
                                     "content": build_user_message(task, tpl)}],
                                    "temperature": gen["temperature"],
                                    "top_p": gen["top_p"],
                                    "max_tokens": gen["max_tokens"],
                                    "seed": gen["seed"],
                                    "cache_prompt": False, "stream": False}
                                rec = {"run_id": run_id, "matrix": "B_stage4",
                                       "config_id": cfg["config_id"],
                                       "unit_key": unit_key, "round": rnd,
                                       "tier": tier, "model": "Q4_K_M",
                                       "task_id": task["id"],
                                       "task_type": task["task_type"],
                                       "input": task["input"],
                                       "started_at_utc": ctl.utc_now_iso(),
                                       "is_real_inference": True,
                                       "synthetic": False,
                                       "quality_sample": False}
                                r = server.request("/v1/chat/completions",
                                                   payload,
                                                   cfg["request_timeout_s"])
                                sent += 1
                                timed_total += 1
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
                                if not rec["performance"][
                                        "performance_record_valid"]:
                                    all_valid = False
                                with requests_path.open("a", encoding="utf-8") as f:
                                    f.write(json.dumps(rec, ensure_ascii=False)
                                            + "\n")
                                if not r["ok"] and "timeout" in (
                                        r.get("error") or "").lower():
                                    st = strict_slots_idle(
                                        server,
                                        cfg["request_timeout_s"])
                                    log.warning("超时后严格槽位确认: %s", st)
                                    unit["timeout_recovery"] = {
                                        "task_id": task["id"], **st}
                                    if not st["freed"]:
                                        not_attempted = [t2["id"] for t2 in tasks
                                                         if t2["id"] not in
                                                         {x["id"] for x in
                                                          tasks[:tasks.index(task)+1]}]
                                        log.error("无法确认空闲，终止单元")
                                        break
                            unit["timed_requests_sent"] = sent
                            unit["not_attempted_within_unit"] = not_attempted
                            unit["memory"] = server.memory_stats()
                            unit["execution_complete"] = (
                                sent == expected_per and not not_attempted)
                            unit["metrics_valid"] = bool(
                                unit["execution_complete"] and all_valid
                                and cache_ok)
                        finally:
                            if server is not None:
                                if "memory" not in unit:
                                    unit["memory"] = server.memory_stats()
                                unit["cleanup"] = server.stop()
                                server = None
                            log_srv.close()
                            unit["cleanup_success"] = bool(
                                unit.get("cleanup", {}).get("confirmed_exit"))
                            unit["resource_after"] = resource_snapshot()
                    # 单元收尾：ok 仅当三态全真
                    unit["ok"] = bool(unit.get("execution_complete")
                                      and unit.get("metrics_valid")
                                      and unit.get("cleanup_success", True))
                    if unit["ok"]:
                        marker.write_text(json.dumps(unit, ensure_ascii=False,
                                                     indent=2), encoding="utf-8")
                    else:
                        (run_dir / f"unit_{unit_no:02d}_{unit_key}_partial.json") \
                            .write_text(json.dumps(unit, ensure_ascii=False,
                                                   indent=2), encoding="utf-8")
                        log.error("单元未完整: ok=%s exec=%s metrics=%s cleanup=%s",
                                  unit["ok"], unit.get("execution_complete"),
                                  unit.get("metrics_valid"),
                                  unit.get("cleanup_success"))
                    result["units"].append({
                        "unit_key": unit_key, "round": rnd, "tier": tier,
                        "ok": unit["ok"],
                        "execution_complete": unit.get("execution_complete"),
                        "metrics_valid": unit.get("metrics_valid"),
                        "cleanup_success": unit.get("cleanup_success")})
        except KeyboardInterrupt:
            result["interrupted"] = True
            result["error"] = "用户中断；已完成单元已落盘，可用 --resume 续做"

        # 汇总从落盘记录重建（marker 文件为准），不依赖内存态、不用 0 填充
        marker_units = []
        for m in sorted(run_dir.glob("unit_*.json")):
            if "_partial" in m.name:
                continue
            d = json.loads(m.read_text(encoding="utf-8"))
            if d.get("execution_complete") and d.get("metrics_valid")                     and d.get("cleanup_success"):
                marker_units.append(d)
        result["units_completed"] = len(marker_units)
        result["units_expected"] = expected_units
        http_reqs = 0
        req_file = run_dir / "requests_threads.jsonl"
        if req_file.is_file():
            http_reqs = sum(1 for l in req_file.read_text(encoding="utf-8")
                            .splitlines() if l.strip())
        result["timed_requests_total"] = http_reqs
        result["aux_requests_total"] = sum(
            3 for u in marker_units if u.get("warmup_and_cache"))
        if args.matrix == "bench":
            result["measurements_total"] = sum(
                u.get("tests_valid_count", 0) for u in marker_units)
            result["measurements_expected"] = 90
            result["execution_complete"] = (
                result["units_completed"] == 30
                and result["measurements_total"] == 90)
        else:
            result["timed_requests_expected"] = 150
            result["aux_expected"] = 90
            result["execution_complete"] = (
                result["units_completed"] == 30
                and http_reqs == 150 and result["aux_requests_total"] == 90)
        result["success"] = bool(result["execution_complete"]
                                 and not result.get("interrupted"))
        exit_code = 0 if result["success"] else 1
    except Exception as exc:  # noqa: BLE001
        log.exception("stage4 执行失败")
        result["success"] = False
        result.setdefault("error", f"{type(exc).__name__}: {exc}")
    finally:
        result["finished_at_utc"] = ctl.utc_now_iso()
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("结果: %s (success=%s, 完成单元=%s/%s, 计时请求=%s)",
                 run_dir / "result.json", result.get("success"),
                 result.get("units_completed"), result.get("units_expected"),
                 result.get("timed_requests_total"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
