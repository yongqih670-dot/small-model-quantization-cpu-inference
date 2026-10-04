# -*- coding: utf-8 -*-
"""Stage 6 事后追溯副本导出（零推理、只读 git，不回填原 run）。

从提交 f92012e（预注册提交=正式运行门禁实际核验的清单状态）导出：
- 执行时冻结的全部材料（以该提交中的冻结清单 files 键为准，动态获取）；
- 该清单本身；
- 冻结#1（f92012e）与冻结#2（当前工作区）的逐条目差异表。

写入 results/stage6/rev/traceability_f92012e/，MANIFEST 标明来源提交、
导出时间与用途；明确声明：这是事后追溯副本，不回填原 run，
不据此宣称正式运行当时已另行存档（当时门禁核验的是工作区状态）。

用法: C:/Anaconda/python.exe scripts/stage6_trace_export.py
"""

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")

COMMIT = "f92012e"
OUT = ROOT / "results" / "stage6" / "rev" / f"traceability_{COMMIT}"
CURRENT_MANIFEST = ROOT / "datasets" / "stage6" / "frozen_hashes_stage6.json"


def git(args: list) -> bytes:
    r = subprocess.run(["git"] + args, cwd=ROOT, capture_output=True,
                       check=True)
    return r.stdout


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    if OUT.exists():
        print(f"[ABORT] 追溯副本目录已存在（不可覆盖）: {OUT}")
        return 1
    # 冻结#1 清单（执行时门禁实际核验的清单）
    manifest_bytes = git(["show", f"{COMMIT}:datasets/stage6/"
                                  f"frozen_hashes_stage6.json"])
    manifest1 = json.loads(manifest_bytes.decode("utf-8"))
    files1: dict = manifest1["files"]

    OUT.mkdir(parents=True, exist_ok=True)
    exported = {}
    crlf_restored = []
    for rel in files1:
        blob = git(["show", f"{COMMIT}:{rel}"])
        want = files1[rel]
        if sha256_bytes(blob) == want:
            data, status, blob_sha = blob, "blob_identical", sha256_bytes(blob)
        else:
            # 该路径不在 .gitattributes -text 覆盖内：blob=LF，冻结清单记录
            # 的是门禁当时核验的工作区字节（CRLF）。LF→CRLF 复原后必须与
            # 清单哈希一致，否则中止（不导出不可复原的材料）。
            crlf = blob.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if sha256_bytes(crlf) != want:
                print(f"[ABORT] 无法复原门禁核验字节: {rel} "
                      f"(blob={sha256_bytes(blob)[:12]}, "
                      f"crlf={sha256_bytes(crlf)[:12]}, want={want[:12]})")
                return 1
            data, status, blob_sha = crlf, "crlf_restored", sha256_bytes(blob)
            crlf_restored.append(rel)
        dest = OUT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        exported[rel] = {"sha256": sha256_bytes(data), "size_bytes": len(data),
                         "git_blob_sha256": blob_sha, "restore_status": status}
    # 清单本身也导出
    (OUT / "frozen_hashes_stage6.json@f92012e").write_bytes(manifest_bytes)

    # 冻结#1 vs 冻结#2 差异
    manifest2 = json.loads(CURRENT_MANIFEST.read_text(encoding="utf-8"))
    files2: dict = manifest2["files"]
    diff = {"only_in_freeze1": sorted(set(files1) - set(files2)),
            "only_in_freeze2": sorted(set(files2) - set(files1)),
            "hash_changed": sorted(k for k in set(files1) & set(files2)
                                   if files1[k] != files2[k]),
            "identical_count": sum(1 for k in set(files1) & set(files2)
                                   if files1[k] == files2[k])}

    doc = {
        "purpose": ("事后追溯副本（retrospective copy）：导出正式运行门禁"
                    "实际核验的冻结材料，供事后核查；"
                    "不回填原 run，不据此宣称正式运行当时已另行存档"),
        "source_commit": COMMIT,
        "source_commit_subject": git(["log", "-1", "--format=%s", COMMIT]
                                     ).decode("utf-8").strip(),
        "exported_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "exported_by": "scripts/stage6_trace_export.py（零推理，只读 git）",
        "basis": ("f92012e 在冻结#1 之后、三个正式 run 之前提交且工作区干净，"
                  "故其 blob 即正式运行时工作区字节；门禁当时核验的是"
                  "工作区状态，本副本仅为事后可核查的导出物"),
        "files": exported,
        "crlf_restoration_finding": {
            "crlf_restored_paths": crlf_restored,
            "finding": ("这些路径不在 .gitattributes -text 覆盖内：git blob 为 LF，"
                        "冻结清单记录的是门禁当时核验的工作区字节（CRLF）；"
                        "LF→CRLF 复原后与清单哈希逐一相符（已断言，否则中止）。"
                        "门禁语义=工作区字节，故当时核验有效；"
                        "本副本按工作区口径复原，属审计发现，不据此重新冻结"),
            "gate_semantics": "冻结清单哈希口径=工作区字节（freeze/verify 均按此核验）"},
        "freeze1_manifest": {"frozen_at": manifest1.get("frozen_at"),
                             "file_count": len(files1)},
        "freeze1_vs_freeze2_diff": diff,
        "non_goals": ["不回填原 run 目录", "不重新冻结", "不补造当时快照",
                      "不据此宣称当时已存档"]}
    (OUT / "MANIFEST.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")
    print(json.dumps({"out": str(OUT.relative_to(ROOT)),
                      "files_exported": len(exported) + 1,
                      "freeze1_files": len(files1),
                      "freeze1_vs_freeze2": diff},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
