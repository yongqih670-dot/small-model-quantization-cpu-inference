"""Official revision, download evidence, SHA256 gate and GGUF tokenizer checks."""
import hashlib
import json
import os
import re
import struct
import sys
import time
from datetime import datetime

from dataset import HERE, MODELS, ROOT, read

REPOSITORY = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
REVISION = "9217f5db79a29953eb74d5343926648285ec7e67"
FP16_FILE = "qwen2.5-0.5b-instruct-fp16.gguf"
FP16_SHA = "8e0ae26000627ed62de0e78e41860af70094558b9d2913385c842a6aa06cf3fc"
API = f"https://huggingface.co/api/models/{REPOSITORY}/tree/{REVISION}"
DOWNLOAD = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{FP16_FILE}"
META_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory():
    original = read(ROOT / "models/model_manifest_v3.json")
    if original["revision_sha"] != REVISION:
        raise ValueError("Original model revision changed")
    by_name = {
        rec["quantization"]: dict(path=rec["path"], sha256=rec["sha256"], size_bytes=rec["size_bytes"],
                                  revision=REVISION,
                                  url=f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{rec['filename']}")
        for rec in original["models"]
    }
    by_name["FP16"] = dict(path=f"models/{FP16_FILE}", sha256=FP16_SHA, revision=REVISION, url=DOWNLOAD)
    if tuple(by_name) != MODELS:
        raise ValueError("Unexpected model inventory")
    return by_name


def skip_value(stream, kind, size):
    if kind in META_SIZES:
        stream.seek(META_SIZES[kind], 1)
    elif kind == 8:
        stream.seek(struct.unpack("<Q", stream.read(8))[0], 1)
    elif kind == 9:
        item, count = struct.unpack("<IQ", stream.read(12))
        if count > 10_000_000:
            raise ValueError("Invalid GGUF array length")
        if item in META_SIZES:
            stream.seek(count * META_SIZES[item], 1)
        else:
            for _ in range(count):
                skip_value(stream, item, size)
    else:
        raise ValueError(f"Unknown GGUF type {kind}")
    if stream.tell() > size:
        raise ValueError("Truncated GGUF metadata")


def metadata(path):
    size = path.stat().st_size
    result = {}
    with path.open("rb") as stream:
        if stream.read(4) != b"GGUF":
            raise ValueError("GGUF magic mismatch")
        version, tensor_count, pairs = struct.unpack("<IQQ", stream.read(20))
        if version not in (2, 3) or not tensor_count or pairs > 4096:
            raise ValueError("Unexpected GGUF header")
        file_type = None
        for _ in range(pairs):
            length, = struct.unpack("<Q", stream.read(8))
            if length > 4096:
                raise ValueError("Unexpected metadata key length")
            key = stream.read(length).decode("utf-8")
            kind, = struct.unpack("<I", stream.read(4))
            start = stream.tell()
            skip_value(stream, kind, size)
            end = stream.tell()
            if key.startswith("tokenizer."):
                stream.seek(start)
                value = stream.read(end - start)
                result[key] = hashlib.sha256(struct.pack("<I", kind) + value).hexdigest()
            if key == "general.file_type":
                if kind != 4:
                    raise ValueError("Unexpected GGUF file type field")
                stream.seek(start)
                file_type, = struct.unpack("<I", stream.read(4))
    if not {"tokenizer.chat_template", "tokenizer.ggml.tokens", "tokenizer.ggml.model"}.issubset(result):
        raise ValueError("Incomplete tokenizer metadata")
    return dict(gguf_version=version, tensor_count=tensor_count, file_type=file_type, tokenizer_keys=result)


def verify():
    evidence = {}
    for model, spec in inventory().items():
        path = ROOT / spec["path"]
        if not path.is_file():
            raise FileNotFoundError(f"Missing required official weight: {path}")
        actual = digest(path)
        if actual != spec["sha256"] or path.stat().st_size != spec.get("size_bytes", path.stat().st_size):
            raise ValueError(f"GGUF hash or size mismatch: {model}")
        details = metadata(path)
        if model == "FP16" and details["file_type"] != 1:
            raise ValueError("Official baseline does not report GGUF mostly F16")
        evidence[model] = dict(sha256=actual, size_bytes=path.stat().st_size, **details)
    if any(value["tokenizer_keys"] != evidence["FP16"]["tokenizer_keys"] for value in evidence.values()):
        raise ValueError("Token vocabulary or chat-template metadata differs")
    return evidence


def download():
    import requests

    target = ROOT / "models" / FP16_FILE
    partial = target.with_name("qwen2.5-0.5b-instruct-fp16.partial.gguf")
    journal = HERE / "download_attempts.jsonl"

    def record(**entry):
        entry["at"] = datetime.now().astimezone().isoformat()
        with journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if target.exists():
        if digest(target) != FP16_SHA:
            raise ValueError("Existing FP16 has the wrong SHA256 and has been preserved")
        print("Pinned FP16 already present and valid", flush=True)
        return

    expected_size = None
    try:
        api = requests.get(API, timeout=(10, 25))
        api.raise_for_status()
        matched = next(item for item in api.json() if item["path"] == FP16_FILE)
        if matched["lfs"]["oid"] != FP16_SHA:
            record(event="official_sha_conflict", http=api.status_code, pinned_sha=FP16_SHA,
                   actual_sha=matched["lfs"]["oid"])
            raise ValueError("Official LFS metadata contradicts the pinned SHA")
        expected_size = matched["lfs"]["size"]
        record(event="official_metadata", revision=REVISION, sha256=FP16_SHA, size=expected_size)
    except (requests.RequestException, StopIteration) as error:
        record(event="official_metadata_unavailable", reason=type(error).__name__,
               http=getattr(getattr(error, "response", None), "status_code", None))

    for attempt in range(1, 5):
        offset = partial.stat().st_size if partial.exists() else 0
        try:
            with requests.get(DOWNLOAD, params={"download": "true", "stage3_resume": f"{offset}-{attempt}"},
                              headers={"Range": f"bytes={offset}-"} if offset else {},
                              stream=True, timeout=(15, 45)) as response:
                response.raise_for_status()
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match or int(match[1]) != offset:
                        raise ValueError("Unsafe response range for partial download")
                    total = int(match[3])
                else:
                    if offset:
                        raise ValueError("Server ignored range; retained existing partial")
                    total = int(response.headers["Content-Length"]) if "Content-Length" in response.headers else expected_size
                if expected_size and total and expected_size != total:
                    raise ValueError("Official size and response size disagree")
                if response.headers.get("Content-Type", "").split(";")[0] in ("text/html", "application/json"):
                    raise ValueError("Model route returned a document instead of a weight")
                record(event="download_response", attempt=attempt, offset=offset, status=response.status_code, total=total)
                with partial.open("ab" if offset else "wb") as f:
                    for data in response.iter_content(2**20):
                        f.write(data)
            length = partial.stat().st_size
            if total and length != total:
                record(event="partial_retained", attempt=attempt, bytes=length)
                continue
            if digest(partial) != FP16_SHA:
                record(event="sha256_mismatch", bytes=length)
                raise ValueError("Downloaded bytes fail the pinned SHA256")
            partial.rename(target)
            record(event="verified", bytes=length, sha256=FP16_SHA, revision=REVISION)
            print(f"Verified pinned FP16 ({length} bytes)", flush=True)
            return
        except requests.RequestException as error:
            record(event="download_failure", attempt=attempt, offset=offset, reason=type(error).__name__,
                   http=getattr(getattr(error, "response", None), "status_code", None))
            print(f"FP16 attempt {attempt} failed: {type(error).__name__}", flush=True)
            time.sleep(attempt * 2)
    raise RuntimeError("Official FP16 download incomplete; preserved all download evidence")


if __name__ == "__main__":
    if sys.argv[1:] == ["download"]:
        download()
    elif sys.argv[1:] == ["verify"]:
        print(json.dumps(verify(), indent=2))
    else:
        raise SystemExit("Specify download or verify")
