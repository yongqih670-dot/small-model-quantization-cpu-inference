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
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import llama_server_ctl as ctl  # noqa: E402
from score_answer import (normalize_text, parse_json_output, score_exact,  # noqa: E402
                          score_item, score_json, score_label)
from llama_server_ctl import extract_perf, parse_load_time_from_log  # noqa: E402

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
                         "prompt_ms": 36.614},
                        {"completion_tokens": 2})
    check("完整 timings: valid 且速度取后端值",
          perf["performance_record_valid"]
          and perf["generation_tokens_per_second"] == 116.171
          and perf["generation_speed_source"] == "server_timings.predicted_per_second")
    check("分母交叉核对 predicted_n==completion_tokens",
          perf["denominator_crosscheck"]["match"] is True)
    perf = extract_perf(None, None)
    check("timings 缺失: valid=False, 速度为 None, 有原因",
          not perf["performance_record_valid"]
          and perf["generation_tokens_per_second"] is None
          and perf["missing_reason"])
    perf = extract_perf({"prompt_ms": 1.0}, {})
    check("timings 缺 predicted_per_second: 不猜算", not perf["performance_record_valid"]
          and perf["generation_tokens_per_second"] is None)

    # ---------------- C. 日志加载时间解析 ----------------
    section("C. 服务日志加载时间解析")
    sample_log = ("\n".join([
        "0.00.040.237 I srv    load_model: loading model 'models/x.gguf'",
        "0.00.224.442 I load_tensors: loading model tensors...",
        "0.00.576.178 I srv  llama_server: model loaded",
        "0.00.576.180 I srv  llama_server: listening on http://127.0.0.1:1"]))
    parsed = parse_load_time_from_log(sample_log)
    check("时间戳格式 M.SS.mmm.uuu 解析出后端加载耗时",
          parsed["model_load_time_s"] == round(0.576178 - 0.040237, 6)
          and parsed["reason"] is None, str(parsed))
    parsed = parse_load_time_from_log("garbage log without timestamps")
    check("无关日志: null + 原因, 不编造", parsed["model_load_time_s"] is None
          and parsed["reason"], str(parsed))

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
    check("清理方式如实记录为主动强制终止",
          "主动强制终止" in rec.get("cleanup", {}).get("method", ""))
    check("无残留服务进程", rec.get("orphan_servers_after") == 0)
    check("没有产出任何请求记录(未发生推理)",
          not (dirs[-1] / "requests.jsonl").exists())

    # ---------------- G. 历史证据不变 ----------------
    section("G. 重复运行不覆盖历史证据")
    all_dirs = sorted(p_.name for p_ in (ROOT / "results/raw").iterdir())
    check("results/raw 下存在多个独立 run 目录", len(all_dirs) >= 3, str(all_dirs))
    for rel, h0 in baseline_hashes.items():
        check(f"Stage 1 证据未被修改: {rel}", sha256(ROOT / rel) == h0)

    # ---------------- H. 真实/合成分离不变量 ----------------
    section("H. 合成记录分离不变量")
    synthetic_hits = []
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
