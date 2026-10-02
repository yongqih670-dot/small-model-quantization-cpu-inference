# -*- coding: utf-8 -*-
"""Stage 1 最小 CPU 推理冒烟测试（smoke test）。

目的：验证「Q4 GGUF 模型 -> llama.cpp llama-server -> CPU 推理」链路可用，
确认模型加载、CPU 执行、文本输出、正常退出、基础计时五个环节，不追求性能。

流程：
  1. 读取 configs/smoke_test.json 配置与 models/model_manifest.json 模型信息；
  2. 启动 llama-server（单实例、CPU-only、-ngl 0、小上下文）；
  3. 轮询 /health 直到就绪，计时首次加载时间（含服务启动与内置 warmup）；
  4. POST /v1/chat/completions 发送一条简单数学题（temperature=0，贪心解码）；
  5. 记录输出文本、token 数、服务端 timings、客户端端到端耗时、进程峰值内存；
  6. 结果写入 results/raw/smoke_test.json，全部过程写入 logs/smoke_test.log。

用法：
    python scripts/smoke_test.py            # 用 .venv 或任意已装 psutil 的 Python
"""

import json
import logging
import math
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "configs" / "smoke_test.json"
MANIFEST_PATH = ROOT / "models" / "model_manifest.json"
RESULT_PATH = ROOT / "results" / "raw" / "smoke_test.json"
LOG_PATH = ROOT / "logs" / "smoke_test.log"
TOOLS_DIR = ROOT / "tools" / "llama.cpp"

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def find_server_exe():
    """在 tools/llama.cpp 下查找 llama-server.exe。"""
    if not TOOLS_DIR.is_dir():
        raise FileNotFoundError(
            f"未找到 {TOOLS_DIR}。请先按 README 安装 llama.cpp 预编译版。")
    hits = sorted(TOOLS_DIR.rglob("llama-server.exe"))
    if not hits:
        raise FileNotFoundError(
            f"{TOOLS_DIR} 下没有 llama-server.exe，请检查解压结果。")
    return hits[0]


def free_port():
    """向系统申请一个空闲 TCP 端口，避免写死端口造成冲突。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(url, timeout=5):
    req = urllib.request.Request(url, headers={"User-Agent": "smoke-test/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def http_post_json(url, payload, timeout=300):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "smoke-test/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main():
    # ---------------- 日志：文件 + 终端双写 ----------------
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_PATH, "w", encoding="utf-8", buffering=1)
    log = logging.getLogger("smoke_test")
    log.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(log_fh),
                    logging.StreamHandler(sys.stdout)):
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S"))
        log.addHandler(handler)

    result = {"success": False, "error": None}
    server = None

    def finish(code):
        for h in log.handlers:
            h.flush()
        log_fh.flush()
        sys.exit(code)

    try:
        # ---------------- 读配置与模型清单 ----------------
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        model_path = ROOT / cfg["model_path"]
        if not model_path.is_file():
            raise FileNotFoundError(f"模型文件不存在: {model_path}")

        threads = int(cfg["threads"])
        ctx = int(cfg["context_length"])
        max_tokens = int(cfg["max_tokens"])
        temperature = float(cfg["temperature"])
        seed = int(cfg["seed"])
        ngl = int(cfg["gpu_offload_layers"])
        prompt = cfg["prompt"]
        expected_hint = cfg.get("expected_hint")

        log.info("=== Stage 1 smoke test 开始 ===")
        log.info("模型: %s (量化 %s)", model_path.name, manifest["quantization"])
        log.info("模型 SHA256: %s", manifest["sha256"])
        log.info("参数: threads=%d ctx=%d max_tokens=%d temperature=%s "
                 "seed=%d ngl=%d (CPU only)", threads, ctx, max_tokens,
                 temperature, seed, ngl)

        server_exe = find_server_exe()
        ver_status = subprocess.run([str(server_exe), "--version"],
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
        backend_version = (ver_status.stdout or ver_status.stderr).strip()
        log.info("后端: %s", backend_version.splitlines()[0] if backend_version
                 else server_exe.name)
        result["backend"] = "llama.cpp llama-server (official prebuilt CPU binaries)"
        result["backend_version"] = backend_version
        result["backend_executable"] = str(server_exe)

        # ---------------- 启动 llama-server（单实例） ----------------
        port = free_port()
        cmd = [str(server_exe),
               "-m", str(model_path),
               "--host", "127.0.0.1",
               "--port", str(port),
               "-c", str(ctx),
               "-t", str(threads),
               "-ngl", str(ngl)]
        log.info("启动命令: %s", " ".join(cmd))
        result["server_command"] = " ".join(cmd)
        result["server_port"] = port

        t0 = time.perf_counter()
        server = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT)
        result["server_pid"] = server.pid
        proc = psutil.Process(server.pid)

        # ---------------- 轮询 /health，计时模型加载 ----------------
        base = f"http://127.0.0.1:{port}"
        health_ok, last_err = False, None
        while time.perf_counter() - t0 < float(cfg["load_timeout_s"]):
            if server.poll() is not None:
                raise RuntimeError(
                    f"llama-server 提前退出 (exit={server.returncode})，"
                    f"请查看日志 {LOG_PATH}")
            try:
                status, body = http_get(f"{base}/health", timeout=3)
                if status == 200 and body.get("status") == "ok":
                    health_ok = True
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = repr(e)
            time.sleep(float(cfg["health_poll_interval_s"]))
        if not health_ok:
            raise RuntimeError(f"等待 /health 就绪超时（{cfg['load_timeout_s']}s）"
                               f"，最后错误: {last_err}")
        model_load_time_s = time.perf_counter() - t0
        log.info("模型加载完成（含服务启动与内置 warmup）: %.3f s",
                 model_load_time_s)
        result["model_load_time_s"] = round(model_load_time_s, 3)

        # ---------------- 发送推理请求 ----------------
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "seed": seed,
        }
        log.info("发送 Prompt: %r", prompt)
        t_req = time.perf_counter()
        status, resp = http_post_json(f"{base}/v1/chat/completions", payload)
        e2e_s = time.perf_counter() - t_req
        if status != 200:
            raise RuntimeError(f"推理请求返回 HTTP {status}: {str(resp)[:500]}")

        output = resp["choices"][0]["message"]["content"]
        usage = resp.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        generated_tokens = int(usage.get("completion_tokens", 0))
        log.info("模型输出: %r", output)
        log.info("端到端请求耗时: %.3f s | prompt_tokens=%d "
                 "generated_tokens=%d", e2e_s, prompt_tokens, generated_tokens)

        # 服务端 timings（llama.cpp 非标准扩展字段；缺失时退化为客户端估算）
        timings = resp.get("timings", {})
        tps_source = "none"
        gen_tps = None
        prompt_tps = None
        prompt_eval_s = None
        gen_time_s = None
        if timings:
            log.info("服务端 timings: %s", json.dumps(timings, ensure_ascii=False))
            tps_source = "server_timings"
            prompt_ms = float(timings.get("prompt_ms", 0) or 0)
            predicted_ms = float(timings.get("predicted_ms", 0) or 0)
            prompt_eval_s = round(prompt_ms / 1000, 4) if prompt_ms else None
            gen_time_s = round(predicted_ms / 1000, 4) if predicted_ms else None
            prompt_tps = (float(timings["prompt_per_second"])
                          if timings.get("prompt_per_second") else None)
            gen_tps = (float(timings["predicted_per_second"])
                       if timings.get("predicted_per_second") else None)
        if gen_tps is None and gen_time_s and gen_time_s > 0:
            gen_tps = generated_tokens / gen_time_s
            tps_source = "derived_from_predicted_ms"
        if gen_tps is None and e2e_s > 0 and timings:
            # 最后的兜底：把整段请求耗时都算进生成（会低估速度）
            gen_tps = generated_tokens / e2e_s
            tps_source = "client_fallback_e2e"
        log.info("生成速度: %s tok/s（来源: %s）",
                 f"{gen_tps:.2f}" if gen_tps else "不可用", tps_source)

        # ---------------- 进程峰值内存（Windows: peak_wset） ----------------
        peak_mem_mib = None
        try:
            if proc.is_running():
                mem = proc.memory_info()
                peak_mem_mib = round(mem.peak_wset / 1048576, 2)
                log.info("llama-server 进程峰值内存: %.2f MiB", peak_mem_mib)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            log.warning("读取进程内存失败: %r", e)

        # ---------------- 组装结果 ----------------
        ok_text = bool(output and output.strip())
        hint_hit = bool(expected_hint and expected_hint in output)
        log.info("输出非空: %s | 包含期望答案 %r: %s",
                 ok_text, expected_hint, hint_hit)

        result.update({
            "model": model_path.name,
            "quantization": manifest["quantization"],
            "model_sha256": manifest["sha256"],
            "threads": threads,
            "context_length": ctx,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "seed": seed,
            "gpu_offload_layers": ngl,
            "prompt": prompt,
            "output": output,
            "end_to_end_time_s": round(e2e_s, 3),
            "total_elapsed_time_s": round(time.perf_counter() - t0, 3),
            "prompt_tokens": prompt_tokens,
            "generated_tokens": generated_tokens,
            "generation_tokens_per_second": round(gen_tps, 2) if gen_tps else None,
            "generation_time_s": gen_time_s,
            "generation_tps_source": tps_source,
            "prompt_eval_time_s": prompt_eval_s,
            "prompt_processing_tokens_per_second":
                round(prompt_tps, 2) if prompt_tps else None,
            "peak_process_memory_mib": peak_mem_mib,
            "server_timings_raw": timings,
            "output_nonempty": ok_text,
            "output_contains_expected": hint_hit,
            "success": bool(health_ok and ok_text and server.poll() is None),
            "error": None,
        })

        # ---------------- 收尾：优雅关闭服务 ----------------
        log.info("关闭 llama-server ...")
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("terminate 未退出，改用 kill")
            server.kill()
            server.wait(timeout=10)
        log.info("llama-server 已退出 (exit=%s)", server.returncode)
        result["server_exit_code"] = server.returncode

        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("结果已写入 %s", RESULT_PATH.relative_to(ROOT))
        log.info("=== smoke test 成功 ===")
        finish(0)

    except Exception as exc:  # noqa: BLE001 —— 失败也要完整落盘，不隐藏错误
        log.exception("smoke test 失败: %r", exc)
        result["success"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        result.setdefault("model", json.loads(
            MANIFEST_PATH.read_text(encoding="utf-8"))["filename"])
        result.setdefault("quantization", json.loads(
            MANIFEST_PATH.read_text(encoding="utf-8"))["quantization"])
        for key, default in (("threads", None), ("context_length", None),
                             ("max_tokens", None), ("prompt", None),
                             ("output", None), ("model_load_time_s", None),
                             ("end_to_end_time_s", None),
                             ("prompt_tokens", None), ("generated_tokens", None),
                             ("generation_tokens_per_second", None)):
            result.setdefault(key, default)
        try:
            RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESULT_PATH.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8")
            log.info("失败结果已如实写入 %s", RESULT_PATH.relative_to(ROOT))
        except OSError:
            pass
        if server is not None and server.poll() is None:
            server.kill()
        finish(1)


if __name__ == "__main__":
    main()
