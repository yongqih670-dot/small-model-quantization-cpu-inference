# -*- coding: utf-8 -*-
"""Stage 2 离线验证套件（不调用模型；无 pytest 依赖，直接运行）。

覆盖（对应 docs/EXPERIMENT_PROTOCOL.md §验证）：
  A 评分器：错误答案不能判成功（含含"5"的错误字符串）；格式/内容/联合口径
  B extract_perf：缺失 timings 绝不伪造速度
  C 日志加载时间解析：格式正确时给出后端统计，异常时 null+原因
  D 守卫：正式测试集被硬拒绝
  E 损坏 JSON 数据集 → 结构化失败记录
  F 非法模型文件 → 服务提前退出被捕获、清理确认、无残留进程
  G 重复运行不覆盖历史证据（唯一 run_id + Stage 1 证据哈希不变）
  H 合成/真实分离不变量（results/ 中不允许出现 synthetic=true 的记录）

运行: .venv/Scripts/python.exe tests/run_tests.py
"""

import hashlib
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import llama_server_ctl as ctl  # noqa: E402
import preflight  # noqa: E402
from score_answer import (normalize_text, parse_json_output, score_exact,  # noqa: E402
                          score_item, score_json, score_label)
from llama_server_ctl import (LlamaServer, capture_backend_version,  # noqa: E402
                              extract_perf, find_server_exe, parse_load_time_from_log,
                              slots_idle)

PYTHON = sys.executable
STAGE1_EVIDENCE = ["results/raw/smoke_test.json", "logs/smoke_test.log",
                   "environment/system_info.json", "models/model_manifest.json"]

FAILURES = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_script(args: list, timeout_s: float = 240) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          cwd=str(ROOT), timeout=timeout_s)


def section(title: str):
    print(f"\n== {title} ==")


def main() -> int:
    orphan_baseline = ctl.count_orphan_servers()
    baseline_hashes = {rel: sha256(ROOT / rel) for rel in STAGE1_EVIDENCE}
    print("Stage 1 证据基线哈希已记录; 残留服务进程基线:", orphan_baseline)
    check("测试前无残留 llama-server 进程", orphan_baseline == 0,
          f"baseline={orphan_baseline}")

    # ---------------- A. 评分器 ----------------
    section("A. 评分器口径")
    for out, expect_correct in [("5", True), (" 5\n", True),
                                ("15", False), ("25", False), ("5.0", False),
                                ("-5", False), ("五", False), ("答案：5", False)]:
        r = score_exact(out, "5")
        check(f"exact '5' vs {out!r} -> correct={expect_correct}",
              r["correct"] == expect_correct,
              f"got {r}")
    r = score_exact("5。", "5")
    check("exact '5。' 内容对但格式错(联合判错)",
          r["content_correct"] and not r["format_correct"] and not r["correct"],
          f"got {r}")
    for wrong in ("52", "答对5个"):
        r = score_exact(wrong, "5")
        check(f"包含'5'的错误字符串 {wrong!r} 不判对", not r["correct"])

    r = score_label("正面", "正面", ["正面", "负面"])
    check("label 命中", r["correct"])
    r = score_label("正面的", "正面", ["正面", "负面"])
    check("label 超出标签集判错", not r["correct"])
    r = score_label("负面", "正面", ["正面", "负面"])
    check("label 错误标签不判对", r["format_correct"] and not r["correct"])

    parsed = parse_json_output('{"a": 1}')
    check("JSON 直接解析 parse_method=direct", parsed["parse_method"] == "direct")
    parsed = parse_json_output('```json\n{"a": 1}\n```')
    check("围栏解析为 fenced+prose", parsed["parse_method"] == "fenced"
          and parsed["surrounding_prose"])
    parsed = parse_json_output('答案如下：{"a": 1} 以上。')
    check("额外说明下按 brace_slice 解析", parsed["parse_method"] == "brace_slice")
    parsed = parse_json_output('这不是JSON')
    check("不可解析输出不编造对象", parsed["obj"] is None
          and parsed["parse_method"] == "unparseable")

    gold = {"姓名": "张三", "年龄": 20, "备注": None}
    r = score_json('{"姓名": "张三", "年龄": 20, "备注": null}', gold)
    check("JSON 全对: format/content/correct 均真",
          r["format_correct"] and r["content_correct"] and r["correct"], str(r))
    r = score_json('```json\n{"姓名": "张三", "年龄": 20, "备注": null}\n```', gold)
    check("JSON 加围栏: 内容对但格式错", r["content_correct"]
          and not r["format_correct"] and not r["correct"])
    r = score_json('{"姓名": "张三", "年龄": 20, "备注": null, "多余": 1}', gold)
    check("JSON 多余字段: 内容对(宽松)但格式错, extra 被记录",
          r["content_correct"] and not r["format_correct"]
          and r["detail"]["extra_fields"] == ["多余"])
    r = score_json('{"姓名": "张三", "年龄": "20"}', {"姓名": "张三", "年龄": 20})
    check("JSON 类型不符(数字写成字符串): 内容判错", not r["content_correct"])
    r = score_json('{"姓名": "张三"}', {"姓名": "张三", "年龄": 20})
    check("JSON 缺字段: 内容判错", not r["content_correct"])
    r = score_json('{"姓名": "张三", "年龄": 20, "备注": null}',
                   {"姓名": "张三", "年龄": 20, "备注": None})
    check("JSON null 字段正确输出 null 判对", r["content_correct"])
    r = score_json('{"课程名称": "《机器学习导论》"}', {"课程名称": "机器学习导论"})
    check("JSON 字段值外层书名号对称剥离后判对",
          r["content_correct"] and r["correct"], str(r["detail"]["fields"]))
    r = score_json('{"姓名": "“张三”"}', {"姓名": "张三"})
    check("JSON 字段值外层弯引号剥离后判对", r["content_correct"])
    r = score_json('{"姓名": "张三》"}', {"姓名": "张三"})
    check("不成对引号不剥离(仍判错)", not r["content_correct"])
    r = score_json('{"班级": "高二（3）班"}', {"班级": "高二（3）班"})
    check("圆括号内容不被剥离", r["content_correct"])

    item = {"scoring": "exact", "gold": "周雨桐"}
    r = score_item(item, "周雨桐")
    check("score_item 调度 exact", r["correct"])
    item = {"scoring": "unknown_mode", "gold": "x"}
    try:
        score_item(item, "x")
        check("未知评分方式必须抛错", False, "未抛出")
    except ValueError:
        check("未知评分方式必须抛错", True)

    # ---------------- B. extract_perf ----------------
    section("B. 缺失 timings 不伪造速度")
    perf = extract_perf({"predicted_per_second": 116.171,
                         "prompt_per_second": 1119.790,
                         "predicted_n": 2, "predicted_ms": 8.608,
                         "prompt_ms": 36.614, "prompt_n": 41},
                        {"completion_tokens": 2}, client_e2e_s=0.064)
    check("完整 timings: valid 且速度取后端值",
          perf["performance_record_valid"]
          and perf["generation_tokens_per_second"] == 116.171
          and perf["generation_speed_source"] == "server_timings.predicted_per_second")
    check("分母交叉核对 predicted_n==completion_tokens",
          perf["denominator_crosscheck"]["match"] is True)
    perf = extract_perf(None, None, client_e2e_s=None)
    check("timings 缺失: 无效+速度为 None+有原因",
          not perf["performance_record_valid"]
          and perf["generation_tokens_per_second"] is None
          and perf["generation_invalid_reason"], str(perf))
    perf = extract_perf({"prompt_ms": 1.0, "prompt_n": 1}, {}, client_e2e_s=0.1)
    check("timings 缺 predicted_per_second: 不猜算",
          not perf["generation_valid"]
          and perf["generation_tokens_per_second"] is None)

    # ---------------- C. 日志加载时间解析（Stage 3 改名语义） ----------------
    section("C. 服务日志初始化区间解析")
    sample_log = ("\n".join([
        "0.00.040.237 I srv    load_model: loading model 'models/x.gguf'",
        "0.00.224.442 I load_tensors: loading model tensors...",
        "0.00.576.178 I srv  llama_server: model loaded",
        "0.00.576.180 I srv  llama_server: listening on http://127.0.0.1:1"]))
    parsed = parse_load_time_from_log(sample_log)
    check("解析出 backend_model_init_interval_s",
          parsed["backend_model_init_interval_s"]
          == round(0.576178 - 0.040237, 6), str(parsed))
    check("model_load_time_s 留空并说明原因(边界未核实)",
          parsed["model_load_time_s"] is None and parsed["reason"], str(parsed))
    parsed = parse_load_time_from_log("garbage log without timestamps")
    check("无关日志: 区间 null + 原因, 不编造",
          parsed["backend_model_init_interval_s"] is None
          and parsed["model_load_time_s"] is None and parsed["reason"], str(parsed))

    # ---------------- C2. extract_perf 有限性与分类有效性 ----------------
    section("C2. 性能字段有限性与分类有效性")
    perf = extract_perf({"predicted_per_second": float("nan"),
                         "prompt_per_second": 1119.0,
                         "predicted_n": 5, "predicted_ms": 8.0,
                         "prompt_ms": 1.0, "prompt_n": 41},
                        {"completion_tokens": 5}, client_e2e_s=0.5)
    check("NaN 生成速度: generation_valid=False", not perf["generation_valid"])
    check("NaN 情形下提示处理仍有效", perf["prompt_processing_valid"])
    check("客户端端到端有效分开表示", perf["client_e2e_valid"])
    check("整体 performance_record_valid=False", not perf["performance_record_valid"])
    perf = extract_perf({"predicted_per_second": float("inf"),
                         "prompt_per_second": 1.0, "predicted_n": 5,
                         "prompt_n": 1},
                        {}, client_e2e_s=0.5)
    check("Infinity 速度无效", not perf["generation_valid"])
    perf = extract_perf({"predicted_per_second": -5.0, "prompt_per_second": 1.0,
                         "predicted_n": 5, "prompt_n": 1}, {}, client_e2e_s=0.5)
    check("负速度无效", not perf["generation_valid"])
    perf = extract_perf({"predicted_per_second": 100.0, "prompt_per_second": 1.0,
                         "predicted_n": 1, "prompt_n": 41,
                         "predicted_ms": 8.0, "prompt_ms": 1.0},
                        {"completion_tokens": 1}, client_e2e_s=0.5)
    check("单 token 解码: 生成速度标注不可靠且不输出",
          not perf["generation_valid"]
          and perf["generation_tokens_per_second"] is None
          and "无可测解码步" in perf["generation_invalid_reason"])
    perf = extract_perf(None, None, client_e2e_s=None)
    check("全缺失: 三类有效性均 False", not perf["generation_valid"]
          and not perf["prompt_processing_valid"]
          and not perf["client_e2e_valid"])
    perf = extract_perf({"predicted_per_second": 116.171,
                         "prompt_per_second": 1119.79, "predicted_n": 2,
                         "predicted_ms": 8.608, "prompt_ms": 36.614,
                         "prompt_n": 41},
                        {"completion_tokens": 2}, client_e2e_s=0.064)
    check("完整有效情形: 三类有效 + 后端速度保留",
          perf["generation_valid"] and perf["prompt_processing_valid"]
          and perf["client_e2e_valid"]
          and perf["generation_tokens_per_second"] == 116.171)

    # ---------------- C3. 总体成功组合 ----------------
    section("C3. success 组合规则")
    from run_tasks import compose_run_success
    check("执行完整+清理失败 => 不成功",
          not compose_run_success(True, False))
    check("执行不完整+清理成功 => 不成功",
          not compose_run_success(False, True))
    check("两者齐备 => 成功", compose_run_success(True, True))
    check("正式模式 preflight 未过 => 不成功",
          not compose_run_success(True, True, formal=True, preflight_passed=False))

    # ---------------- C4. 冻结哈希校验函数 ----------------
    section("C4. 哈希不匹配检测")
    import tempfile
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        fake_manifest = Path(td) / "m.json"
        fake_manifest.write_text(json.dumps({"files": {
            "README.md": "0" * 64}}), encoding="utf-8")
        probs = preflight.check_frozen_hashes(fake_manifest, base=ROOT)
        check("篡改哈希被检出", any("README.md" in p and "不匹配" in p
                                    for p in probs), str(probs))
        fake_manifest.write_text(json.dumps({"files": {
            "no_such_file.txt": "0" * 64}}), encoding="utf-8")
        probs = preflight.check_frozen_hashes(fake_manifest, base=ROOT)
        check("缺失文件被检出", any("缺失" in p for p in probs), str(probs))

    # ---------------- C5. test-012 渲染与金标一致 ----------------
    section("C5. test-012 渲染要求与金标一致")
    ds = json.loads((ROOT / "datasets/test/test_tasks.json")
                    .read_text(encoding="utf-8"))
    tpl = json.loads((ROOT / "datasets/prompt_template.json")
                     .read_text(encoding="utf-8"))
    t12 = next(t for t in ds["tasks"] if t["id"] == "test-012")
    f = {"input": t12["input"], "field": t12["field"], "fields": "", "labels": "",
         "rule": ""}
    rendered = tpl["per_type"][t12["task_type"]].format(**f)
    check("test-012 金标为 '38.5'", t12["gold"] == "38.5")
    check("渲染提示含无单位规则", "只输出数字本身" in rendered)
    check("渲染提示保留答案内部字符的说明", "小数点" in rendered)
    check("字段名含单位标注", "（元）" in t12["field"])

    # ---------------- C6. JSON 重复键与 NaN 口径 ----------------
    section("C6. JSON 重复键/非标准数值预注册口径")
    parsed = parse_json_output('{"a": 1, "a": 2}')
    check("重复键按 Python 语义后者覆盖", parsed["obj"] == {"a": 2})
    parsed = parse_json_output('{"a": NaN}')
    check("NaN 可解析为 float(预注册口径)",
          parsed["obj"] is not None and parsed["obj"]["a"] != parsed["obj"]["a"])

    # ---------------- D. 守卫 ----------------
    section("D. 正式测试集硬拒绝")
    run_dirs_before = set(p.name for p in (ROOT / "results/raw").iterdir())
    p = run_script(["scripts/run_tasks.py", "--dataset",
                    "datasets/test/test_tasks.json", "--run-label", "guard"])
    check("运行正式集返回码=2", p.returncode == 2, f"rc={p.returncode}")
    check("守卫信息写入 stderr", "GUARD" in (p.stderr or ""))
    run_dirs_after = set(p_.name for p_ in (ROOT / "results/raw").iterdir())
    check("守卫路径未创建任何 run 目录", run_dirs_before == run_dirs_after)

    # ---------------- E. 损坏数据集 ----------------
    section("E. 损坏 JSON 数据集留结构化失败记录")
    p = run_script(["scripts/run_tasks.py", "--dataset",
                    "tests/fixtures/corrupt_tasks.json", "--run-label",
                    "testcorrupt"])
    check("损坏数据集退出码=1", p.returncode == 1, f"rc={p.returncode}")
    dirs = sorted((ROOT / "results/raw").glob("run_*_testcorrupt_*"))
    check("失败运行有独立 run 目录", len(dirs) >= 1)
    rec = json.loads((dirs[-1] / "result.json").read_text(encoding="utf-8"))
    check("失败记录: success=false 且 failure_stage=load_inputs",
          rec["success"] is False and rec.get("failure_stage") == "load_inputs",
          str(rec.get("failure_stage")))
    check("失败记录含错误信息", bool(rec.get("error")))
    check("损坏数据集未启动服务(无 cleanup 字段或未启动)",
          rec.get("server_startup_to_ready_s") is None)
    check("未启动模型前失败不标为真实推理", rec.get("is_real_inference") is False,
          str(rec.get("is_real_inference")))

    # ---------------- E2. 运行中断: 保留已完成 + 标记未执行 ----------------
    section("E2. 中断路径保留已完成请求并标记 not_attempted")
    p = run_script(["scripts/run_tasks.py", "--dataset",
                    "tests/fixtures/broken_second_task.json",
                    "--run-label", "testbroken"])
    check("中途异常退出码=1", p.returncode == 1, f"rc={p.returncode}")
    dirs = sorted((ROOT / "results/raw").glob("run_*_testbroken_*"))
    check("失败运行有独立 run 目录", len(dirs) >= 1)
    rec = json.loads((dirs[-1] / "result.json").read_text(encoding="utf-8"))
    check("not_attempted 正确标记剩余任务",
          rec.get("not_attempted") == ["fix-002"], str(rec.get("not_attempted")))
    check("已完成请求保留在 requests.jsonl",
          (dirs[-1] / "requests.jsonl").is_file()
          and len((dirs[-1] / "requests.jsonl")
                  .read_text(encoding="utf-8").strip().splitlines()) == 1)
    check("execution_complete=False", rec.get("execution_complete") is False)
    check("不宣布成功", rec.get("success") is False)

    # ---------------- F. 非法模型 → 服务提前退出 ----------------
    section("F. 服务提前退出的失败记录与清理")
    p = run_script(["scripts/run_tasks.py", "--dataset",
                    "datasets/dev/dev_tasks.json", "--config",
                    "tests/fixtures/badmodel_config.json",
                    "--run-label", "testbadmodel"], timeout_s=300)
    check("非法模型运行退出码=1", p.returncode == 1, f"rc={p.returncode}")
    dirs = sorted((ROOT / "results/raw").glob("run_*_testbadmodel_*"))
    check("失败运行有独立 run 目录", len(dirs) >= 1)
    rec = json.loads((dirs[-1] / "result.json").read_text(encoding="utf-8"))
    check("失败记录 success=false 且错误含'提前退出'",
          rec["success"] is False and "提前退出" in (rec.get("error") or ""),
          str(rec.get("error")))
    check("清理已确认退出", rec.get("cleanup", {}).get("confirmed_exit") is True)
    check("清理方式如实区分(服务自行退出 vs 主动强制终止)",
          ("主动强制终止" in rec.get("cleanup", {}).get("method", "")
           or rec.get("cleanup", {}).get("server_self_exited") is True
           and "already_exited" in rec.get("cleanup", {}).get("method", "")),
          str(rec.get("cleanup", {}).get("method")))
    check("无残留服务进程(全局计数)", rec.get("orphan_servers_global_after") == 0)
    check("没有产出任何请求记录(未发生推理)",
          not (dirs[-1] / "requests.jsonl").exists())
    check("服务启动但零请求: 不标为真实推理",
          rec.get("is_real_inference") is False, str(rec.get("is_real_inference")))

    # ---------------- F2. 真实服务: HTTPError/槽位确认/内存/清理 ----------------
    section("F2. 真实服务行为: HTTPError 捕获、slots 确认、退出前内存")
    model_path = ROOT / "models/qwen2.5-0.5b-instruct-q4_k_m.gguf"
    if model_path.is_file():
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            srv_log = (tdp / "server.log").open("w", encoding="utf-8", buffering=1)
            server = None
            try:
                server = LlamaServer(find_server_exe(),
                                     ["-m", str(model_path), "--parallel", "1",
                                      "-c", "512", "-t", "2", "-ngl", "0"],
                                     srv_log, logging.getLogger("t_f2"),
                                     memory_sample_interval_s=0.05)
                server.start()
                ready = server.wait_ready(timeout_s=120, poll_interval_s=0.25)
                check("F2: 服务就绪", ready["ok"], str(ready))
                bad = server.request("/definitely_not_here", None, timeout_s=10)
                check("F2: HTTPError 保存状态码",
                      not bad["ok"] and bad["error_type"] == "HTTPError"
                      and bad["http_status"] is not None, str(bad["http_status"]))
                check("F2: HTTPError 保存错误体", bool(bad.get("error_body")))
                st = slots_idle(server, timeout_s=20)
                check("F2: 空闲时 slots 确认返回 freed",
                      st["freed"], str(st))
                mem = server.memory_stats()
                check("F2: 服务运行中即可取得生命周期峰值内存",
                      mem["peak_wset_mib"] is not None and mem["peak_wset_mib"] > 0,
                      str(mem))
                check("F2: 采样峰值已写回(间隔0.05s)",
                      mem.get("sampled_peak_rss_mib") is not None
                      and mem.get("sample_interval_s") == 0.05, str(mem))
            finally:
                if server is not None:
                    stopinfo = server.stop()
                srv_log.close()
            check("F2: 主动终止并确认退出",
                  stopinfo["confirmed_exit"]
                  and "主动强制终止" in stopinfo["method"], str(stopinfo))
    else:
        print("  [SKIP] F2: 模型文件不存在")

    # ---------------- G. 历史证据不变 ----------------
    section("G. 重复运行不覆盖历史证据")
    all_dirs = sorted(p_.name for p_ in (ROOT / "results/raw").iterdir())
    check("results/raw 下存在多个独立 run 目录", len(all_dirs) >= 3, str(all_dirs))
    for rel, h0 in baseline_hashes.items():
        check(f"Stage 1 证据未被修改: {rel}", sha256(ROOT / rel) == h0)

    # ---------------- H. 真实/合成分离不变量 ----------------
    section("H. 合成记录分离不变量")
    synthetic_hits = []
    real_output_missing_flag = []
    for f in (ROOT / "results/raw").rglob("*"):
        if f.is_file() and f.suffix in (".json", ".jsonl"):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            if '"synthetic": true' in text:
                synthetic_hits.append(str(f.relative_to(ROOT)))
    check("results/ 中不存在 synthetic=true 的记录", not synthetic_hits,
          str(synthetic_hits))
    # 真实性不能只靠"没有 synthetic 标签"：凡带非空模型输出的记录必须显式
    # 标记 is_real_inference=true（单行 JSONL 逐行检查）
    for jf in (ROOT / "results/raw").rglob("requests*.jsonl"):
        for line in jf.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("raw_output") and not r.get("is_real_inference"):
                real_output_missing_flag.append(str(jf.relative_to(ROOT)))
    check("带模型输出的记录均显式标记真实推理",
          not real_output_missing_flag, str(real_output_missing_flag))

    # ---------------- 收尾 ----------------
    orphans_after = ctl.count_orphan_servers()
    check("全部测试结束后无残留 llama-server 进程", orphans_after == 0,
          f"after={orphans_after}")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"测试未全部通过: {len(FAILURES)} 项失败")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("全部离线测试通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
