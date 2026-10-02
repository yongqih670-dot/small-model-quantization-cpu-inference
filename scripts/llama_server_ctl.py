# -*- coding: utf-8 -*-
"""llama-server 进程生命周期控制（Stage 2 修正版核心库）。

设计要点（对应 docs/stage2_corrections.md）：
- 启动→/health 就绪时间命名为 server_startup_to_ready_s（含进程拉起与后端内置
  warmup，不含任何清理/写文件/退出耗时）；
- model_load_time_s 只在后端日志提供可靠加载统计时填写（由日志时间戳差值解析），
  否则为 null 并记录原因；
- 退出使用 Windows terminate（= TerminateProcess），如实记录为“主动强制终止”，
  不称为优雅退出；
- 清理位于 finally 路径，只清理本对象启动的进程，结束后 wait 并确认退出；
- 内存记录：peak_wset 是进程生命周期峰值，不能当作单请求峰值；另提供可选的
  定时采样（interval_s 记录在结果中，称为 sampled_peak）。
"""

from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools" / "llama.cpp"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id(label: str) -> str:
    """唯一 run_id：本地时间 + 短随机后缀，避免同秒覆盖。"""
    import uuid
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"run_{stamp}_{label}_{uuid.uuid4().hex[:6]}"


def find_server_exe() -> Path:
    if not TOOLS_DIR.is_dir():
        raise FileNotFoundError(f"未找到 {TOOLS_DIR}，请先安装 llama.cpp 预编译版")
    hits = sorted(TOOLS_DIR.rglob("llama-server.exe"))
    if not hits:
        raise FileNotFoundError(f"{TOOLS_DIR} 下没有 llama-server.exe")
    return hits[0]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def capture_backend_version(server_exe: Path) -> dict:
    """版本信息同时保留 stdout 与 stderr，并检查退出状态。"""
    proc = subprocess.run([str(server_exe), "--version"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return {
        "command": f"{server_exe.name} --version",
        "exit_code": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


# llama.cpp 日志时间戳格式：M.SS.mmm.uuu（分.秒.毫秒.微秒，进程启动为原点）
_TS_RE = re.compile(
    r"^\s*(\d+)\.(\d{2})\.(\d{3})\.(\d{3})\s+([IWE])\s")


def ts_to_seconds(match) -> float:
    minutes, seconds, millis, micros = (int(g) for g in match.groups()[:4])
    return minutes * 60 + seconds + millis / 1000 + micros / 1e6


def parse_load_time_from_log(log_text: str) -> dict:
    """从服务端日志解析模型加载耗时（后端自己的时间统计）。

    返回 {"model_load_time_s": float|None, "source": str, "reason": str|None}。
    时间戳格式若与预期不符则返回 null 并说明原因，不做猜测性换算。
    """
    events = {}
    for line in log_text.splitlines():
        m = _TS_RE.match(line)
        if not m:
            continue
        t = ts_to_seconds(m)
        if "load_model: loading model" in line:
            events.setdefault("loading", t)
        elif "llama_server: model loaded" in line:
            events.setdefault("loaded", t)
        elif "llama_server: listening on" in line:
            events.setdefault("listening", t)
    # 一致性检查：加载起止时间先后顺序必须合理
    if "loading" in events and "loaded" in events:
        if events["loaded"] < events["loading"]:
            return {"model_load_time_s": None, "source": "server_log",
                    "reason": f"日志时间戳顺序异常: {events}"}
        return {"model_load_time_s": round(events["loaded"] - events["loading"], 6),
                "source": "server_log_delta(loading_model->model_loaded)",
                "reason": None}
    return {"model_load_time_s": None, "source": "server_log",
            "reason": f"日志缺少加载起止行, 仅找到: {sorted(events)}"}


class LlamaServer:
    """单实例 llama-server 控制器。"""

    def __init__(self, server_exe: Path, argv: list, log_fh, logger,
                 memory_sample_interval_s: float | None = None):
        self.server_exe = server_exe
        self.argv = argv
        self.log_fh = log_fh          # 服务原始输出写入的文件句柄
        self.log = logger
        self.memory_sample_interval_s = memory_sample_interval_s
        self.proc: subprocess.Popen | None = None
        self.ps_proc: psutil.Process | None = None
        self.port: int | None = None
        self.startup_to_ready_s: float | None = None
        self.sampled_peak_rss_mib: float | None = None
        self._sampler_stop = threading.Event()
        self._sampler_thread: threading.Thread | None = None
        self.shutdown = {"attempted": False, "method": None,
                         "confirmed_exit": False, "exit_code": None}

    def start(self) -> None:
        self.port = free_port()
        full_argv = [str(self.server_exe)] + self.argv + [
            "--host", "127.0.0.1", "--port", str(self.port)]
        self.full_argv = full_argv
        self.log.info("启动 llama-server: %s", " ".join(full_argv))
        self.t_start_wall = time.perf_counter()
        self.proc = subprocess.Popen(full_argv, stdout=self.log_fh,
                                     stderr=subprocess.STDOUT)
        self.ps_proc = psutil.Process(self.proc.pid)
        if self.memory_sample_interval_s:
            self._sampler_thread = threading.Thread(
                target=self._sampler_loop, daemon=True)
            self._sampler_thread.start()

    def _sampler_loop(self) -> None:
        peak = 0.0
        while not self._sampler_stop.is_set():
            try:
                rss = self.ps_proc.memory_info().rss / 1048576
                peak = max(peak, rss)
                self.sampled_peak_rss_mib = round(peak, 3)  # 运行中持续写回
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            self._sampler_stop.wait(self.memory_sample_interval_s)
        if peak > 0:
            self.sampled_peak_rss_mib = round(peak, 3)

    def wait_ready(self, timeout_s: float, poll_interval_s: float) -> dict:
        """轮询 /health；返回 {"ok": bool, "error": str|None}。"""
        base = self.base_url
        deadline = time.perf_counter() + timeout_s
        last_err = None
        while time.perf_counter() < deadline:
            if self.proc.poll() is not None:
                return {"ok": False,
                        "error": f"llama-server 提前退出 (exit={self.proc.returncode})"}
            try:
                import urllib.request
                with urllib.request.urlopen(f"{base}/health", timeout=3) as resp:
                    if resp.status == 200:
                        body = json.loads(resp.read().decode("utf-8"))
                        if body.get("status") == "ok":
                            self.startup_to_ready_s = round(
                                time.perf_counter() - self.t_start_wall, 6)
                            return {"ok": True, "error": None}
            except Exception as e:  # noqa: BLE001
                last_err = repr(e)
            time.sleep(poll_interval_s)
        return {"ok": False,
                "error": f"等待 /health 就绪超时({timeout_s}s), 最后错误: {last_err}"}

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(self, path: str, payload: dict | None, timeout_s: float) -> dict:
        import urllib.error
        import urllib.request
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method="POST" if payload is not None else "GET",
            headers={"Content-Type": "application/json",
                     "User-Agent": "quant-lab/2.0"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return {"ok": True, "http_status": resp.status, "body": body,
                        "client_e2e_s": time.perf_counter() - t0, "error": None}
        except Exception as e:  # noqa: BLE001 —— 失败信息完整返回，由调用方记录
            return {"ok": False, "http_status": None, "body": None,
                    "client_e2e_s": time.perf_counter() - t0,
                    "error": f"{type(e).__name__}: {e}"}

    def memory_stats(self) -> dict:
        """进程级内存：lifetime peak（peak_wset）+ 采样峰值（若开启）。"""
        stats = {"peak_wset_mib": None, "sampled_peak_rss_mib": self.sampled_peak_rss_mib,
                 "note": "peak_wset 为进程生命周期峰值, 非单请求峰值; "
                         "sampled_peak 为间隔采样值"}
        if self.memory_sample_interval_s:
            stats["sample_interval_s"] = self.memory_sample_interval_s
        try:
            if self.ps_proc and self.ps_proc.is_running():
                stats["peak_wset_mib"] = round(
                    self.ps_proc.memory_info().peak_wset / 1048576, 3)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return stats

    def stop(self) -> dict:
        """主动终止本对象启动的进程（Windows: TerminateProcess，非优雅退出）。"""
        info = {"attempted": True, "method": "terminate()=Windows TerminateProcess(主动强制终止)",
                "confirmed_exit": False, "exit_code": None}
        if self._sampler_thread:
            self._sampler_stop.set()
            self._sampler_thread.join(timeout=3)
        if self.proc is None:
            info["method"] = "no_process_started"
            return info
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError as e:
                self.log.warning("terminate 失败: %r, 改用 kill", e)
                try:
                    self.proc.kill()
                except OSError:
                    pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.log.warning("terminate 后 15s 未退出, 执行 kill")
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.log.error("kill 后仍未退出, 进程可能残留")
        info["exit_code"] = self.proc.returncode
        info["confirmed_exit"] = self.proc.poll() is not None
        self.log.info("llama-server 退出: exit=%s (%s)", info["exit_code"],
                      "已确认" if info["confirmed_exit"] else "未确认!")
        return info

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def extract_perf(timings: dict | None, usage: dict | None) -> dict:
    """从服务端 timings/usage 提取性能指标（Stage 2 预注册口径）。

    - 速度只取后端自报值（predicted_per_second / prompt_per_second）；
    - 绝不从 predicted_ms 或 output_tokens/end_to_end_s 猜算速度；
    - timings 缺失或缺关键子段时速度为 None，performance_record_valid=False
      并给出 missing_reason。
    """
    timings = timings or {}
    usage = usage or {}
    reason = None
    if not timings:
        reason = "服务端响应缺少 timings 对象"
    elif "predicted_per_second" not in timings:
        reason = "timings 缺少 predicted_per_second"
    valid = reason is None
    return {
        "performance_record_valid": valid,
        "missing_reason": reason,
        "generation_tokens_per_second": timings.get("predicted_per_second")
        if valid else None,
        "prompt_processing_tokens_per_second": timings.get("prompt_per_second")
        if valid else None,
        "generation_time_s": (timings["predicted_ms"] / 1000.0
                              if valid and "predicted_ms" in timings else None),
        "prompt_eval_time_s": (timings["prompt_ms"] / 1000.0
                               if valid and "prompt_ms" in timings else None),
        "generation_speed_source": "server_timings.predicted_per_second" if valid else None,
        "predicted_n": timings.get("predicted_n"),
        "completion_tokens": usage.get("completion_tokens"),
        "denominator_crosscheck": {
            "predicted_n": timings.get("predicted_n"),
            "completion_tokens": usage.get("completion_tokens"),
            "match": (timings.get("predicted_n") == usage.get("completion_tokens")
                      if timings.get("predicted_n") is not None
                      and usage.get("completion_tokens") is not None else None),
            "note": "分母语义以 predicted_n 为准(后端计时对象); 与 OAI "
                    "completion_tokens 交叉核对, 不一致时须在报告中说明"},
    }


def count_orphan_servers() -> int:
    """统计残留的 llama-server.exe 进程数（本工具清理校验用）。"""
    return sum(1 for p in psutil.process_iter(["name"])
               if (p.info["name"] or "").lower() == "llama-server.exe")
