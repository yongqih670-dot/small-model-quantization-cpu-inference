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
import math
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
    """从服务端日志解析后端模型初始化区间（Stage 3 改名并限定语义）。

    返回：
      backend_model_init_interval_s —— 日志时间戳差值
        (`load_model: loading model` → `llama_server: model loaded`)。
        该区间包含后端加载过程中的全部初始化开销，其准确边界未核实，
        **不称"纯权重加载耗时"**；
      model_load_time_s —— 恒为 None + 原因（没有独立可靠的
        "纯模型加载"统计前留空，不得用启动就绪时间替代）。
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
    reason = None
    if "loading" not in events or "loaded" not in events:
        return {"backend_model_init_interval_s": None,
                "model_load_time_s": None,
                "reason": f"日志缺少加载起止行, 仅找到: {sorted(events)}"}
    if events["loaded"] < events["loading"]:
        return {"backend_model_init_interval_s": None,
                "model_load_time_s": None,
                "reason": f"日志时间戳顺序异常: {events}"}
    return {"backend_model_init_interval_s":
            round(events["loaded"] - events["loading"], 6),
            "model_load_time_s": None,
            "reason": ("model_load_time_s 留空: loading→loaded 区间的准确边界"
                       "未核实(含后端初始化开销)，不称纯权重加载耗时；"
                       "区间值记入 backend_model_init_interval_s")}


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
                     "User-Agent": "quant-lab/3.0"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return {"ok": True, "http_status": resp.status, "body": body,
                        "client_e2e_s": time.perf_counter() - t0, "error": None,
                        "error_type": None}
        except urllib.error.HTTPError as e:
            # 尽可能保存状态码与错误响应体，不再一律 status=null
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except OSError:
                err_body = ""
            return {"ok": False, "http_status": e.code, "body": None,
                    "error_body": err_body[:2000],
                    "client_e2e_s": time.perf_counter() - t0,
                    "error": f"HTTPError {e.code}: {err_body[:200]}",
                    "error_type": "HTTPError"}
        except Exception as e:  # noqa: BLE001 —— 失败信息完整返回，由调用方记录
            return {"ok": False, "http_status": None, "body": None,
                    "client_e2e_s": time.perf_counter() - t0,
                    "error": f"{type(e).__name__}: {e}",
                    "error_type": type(e).__name__}

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
        """主动终止本对象启动的进程（Windows: TerminateProcess，非优雅退出）。

        只处理本对象 Popen 启动的 PID；不检查也不影响其他项目的同名进程
        （全局同名进程数仅作环境记录，不参与本运行清理判定）。
        """
        already_gone = self.proc is not None and self.proc.poll() is not None
        info = {"attempted": True,
                "method": ("no_action_process_already_exited" if already_gone
                           else "terminate()=Windows TerminateProcess(主动强制终止)"),
                "server_self_exited": bool(already_gone),
                "confirmed_exit": False,
                "exit_code": None}
        if self._sampler_thread:
            self._sampler_stop.set()
            self._sampler_thread.join(timeout=3)
        if self.proc is None:
            info["method"] = "no_process_started"
            info["attempted"] = False
            return info
        if not already_gone:
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
        self.log.info("llama-server 退出: exit=%s (%s, %s)", info["exit_code"],
                      "服务自行退出" if info["server_self_exited"] else "本程序主动终止",
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


def _finite_number(value) -> bool:
    """类型为数字、有限（非 NaN/Inf）且不为负，才能通过有效性检查。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value)) and float(value) >= 0


def extract_perf(timings: dict | None, usage: dict | None,
                 client_e2e_s: float | None = None) -> dict:
    """统一性能解析与校验（Stage 3 预注册口径）。

    - 速度只取后端自报值（predicted_per_second / prompt_per_second）；
      绝不用 output_tokens/e2e 或猜测公式替代；
    - 字段须为有限非负数字：null/NaN/Infinity/负耗时无效；
    - 提示处理、生成、客户端端到端三者的有效性分开表示；
    - predicted_n<=1 时没有可测解码步，生成速度单独标注不可靠，不猜算；
    - predicted_n 与 completion_tokens 相等不代表分母语义已证实，
      仅作为交叉核对记录保留。
    """
    timings = timings if isinstance(timings, dict) else {}
    usage = usage if isinstance(usage, dict) else {}

    gen_tps = timings.get("predicted_per_second")
    prompt_tps = timings.get("prompt_per_second")

    gen_reason = None
    if not timings:
        gen_reason = "服务端响应缺少 timings 对象"
    elif not _finite_number(gen_tps):
        gen_reason = ("timings 缺少 predicted_per_second" if gen_tps is None
                      else f"predicted_per_second 非有限非负值: {gen_tps!r}")
    elif float(timings.get("predicted_n") or 0) <= 1:
        gen_reason = ("predicted_n<=1: 无可测解码步，后端吞吐不可靠，"
                      "单独标注且不用于速度结论")
    prompt_reason = None
    if not timings:
        prompt_reason = "服务端响应缺少 timings 对象"
    elif not _finite_number(prompt_tps):
        prompt_reason = ("timings 缺少 prompt_per_second" if prompt_tps is None
                         else f"prompt_per_second 非有限非负值: {prompt_tps!r}")
    elif float(timings.get("prompt_n") or 0) <= 0:
        prompt_reason = "prompt_n<=0: 无提示处理量"
    e2e_reason = None
    if not _finite_number(client_e2e_s) or float(client_e2e_s or 0) <= 0:
        e2e_reason = "客户端端到端时间缺失或非正"

    pred_n = timings.get("predicted_n")
    comp_n = usage.get("completion_tokens")
    return {
        "generation_valid": gen_reason is None,
        "generation_invalid_reason": gen_reason,
        "prompt_processing_valid": prompt_reason is None,
        "prompt_processing_invalid_reason": prompt_reason,
        "client_e2e_valid": e2e_reason is None,
        "client_e2e_invalid_reason": e2e_reason,
        "performance_record_valid": (gen_reason is None and prompt_reason is None
                                     and e2e_reason is None),
        "generation_tokens_per_second": gen_tps if gen_reason is None else None,
        "prompt_processing_tokens_per_second": (prompt_tps
                                                if prompt_reason is None else None),
        "generation_time_s": (timings["predicted_ms"] / 1000.0
                              if _finite_number(timings.get("predicted_ms")) else None),
        "prompt_eval_time_s": (timings["prompt_ms"] / 1000.0
                               if _finite_number(timings.get("prompt_ms")) else None),
        "generation_speed_source": "server_timings.predicted_per_second",
        "prompt_processing_source": "server_timings.prompt_per_second",
        "predicted_n": pred_n,
        "completion_tokens": comp_n,
        "denominator_crosscheck": {
            "predicted_n": pred_n,
            "completion_tokens": comp_n,
            "match": (pred_n == comp_n if pred_n is not None
                      and comp_n is not None else None),
            "note": ("两者相等仅作交叉核对记录；分母语义以 predicted_n 为准的"
                     "判断需另行核实，不据相等直接声明")},
    }


def slots_idle(server: "LlamaServer", timeout_s: float = 60.0,
               poll_s: float = 0.5) -> dict:
    """超时后确认服务端确已停止生成（请求超时≠服务端已停止）。

    通过 GET /slots 检查所有 slot 的 is_processing；在超时预算内未空闲
    则返回 freed=False，由调用方决定是否终止服务。
    """
    deadline = time.perf_counter() + timeout_s
    last = None
    while time.perf_counter() < deadline:
        r = server.request("/slots", None, timeout_s=5)
        if r["ok"]:
            body = r.get("body")
            if isinstance(body, dict):
                slots = body.get("slots") or []
            elif isinstance(body, list):
                slots = body
            else:
                slots = []
            busy = any(s.get("is_processing") for s in slots)
            last = {"slots": len(slots), "busy": busy}
            if not busy:
                return {"freed": True, "state": last, "action": "confirmed_idle"}
        time.sleep(poll_s)
    return {"freed": False, "state": last, "action": "not_confirmed"}


def resource_snapshot() -> dict:
    """简要只读资源状态（CPU 总占用低≠完全没有干扰，仅作记录）。"""
    return {
        "time": utc_now_iso(),
        "cpu_percent_sample": psutil.cpu_percent(interval=0.3),
        "free_ram_gib": round(psutil.virtual_memory().available / 1024 ** 3, 2),
        "llama_server_process_count_global": count_orphan_servers(),
        "note": "仅记录本机负载；不据此筛选数据",
    }


def ensure_run_dir(base: Path, run_id: str) -> Path:
    """创建 run 目录；已存在则拒绝（防覆盖历史证据）。"""
    path = base / run_id
    path.mkdir(parents=True, exist_ok=False)
    return path


def count_orphan_servers() -> int:
    """统计残留的 llama-server.exe 进程数（本工具清理校验用）。"""
    return sum(1 for p in psutil.process_iter(["name"])
               if (p.info["name"] or "").lower() == "llama-server.exe")
