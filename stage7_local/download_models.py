"""Resume the three pinned models using verified byte ranges and final SHA256."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
MANIFEST = json.loads((ROOT / "model_manifest.json").read_text(encoding="utf-8"))
CURL = Path(os.environ["SystemRoot"]) / "System32" / "curl.exe"
CHUNK = 8 * 1024 * 1024

def valid(path, model):
    if not path.exists() or path.stat().st_size != model["size_bytes"]:
        return False
    with path.open("rb") as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest() == model["sha256"]

def fetch(model):
    name = model["filename"]
    final = ASSETS / name
    if valid(final, model):
        print(name, "already verified", flush=True)
        return
    size = model["size_bytes"]
    parts = ASSETS / ("parts-" + model["quantization"])
    parts.mkdir(exist_ok=True)
    prefix = ASSETS / (name + ".part")
    begin = prefix.stat().st_size if prefix.exists() else 0
    if begin >= size:
        begin = 0
    mirror = model["source_download_url"].replace("/main/", "/" + model["revision_sha"] + "/")
    alternate = "https://www.modelscope.cn/models/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/master/" + name

    def block(start):
        end = min(start + CHUNK, size) - 1
        path = parts / f"{start}-{end}"
        if path.exists() and path.stat().st_size == end - start + 1:
            return path
        partial = path.with_suffix(".resume")
        piece = path.with_suffix(".piece")
        old_temp = path.with_suffix(".tmp")
        failures = 0
        attempt = 0
        while failures < 8:
            have = partial.stat().st_size if partial.exists() else 0
            if have == end - start + 1:
                partial.replace(path)
                old_temp.unlink(missing_ok=True)
                return path
            offset = start + have
            origin = [alternate, mirror][attempt % 2]
            attempt += 1
            url = origin + f"?stage7_range={offset}-{end}&attempt={attempt}"
            p = subprocess.run([str(CURL), "--fail", "--location", "--silent", "--show-error",
                                "--connect-timeout", "12", "--max-time", "100", "--max-filesize", str(end-offset+1),
                                "--range", f"{offset}-{end}", "--output", str(piece),
                                "--write-out", "%{http_code}|%header{content-range}", url],
                               capture_output=True, text=True)
            received = piece.stat().st_size if piece.exists() else 0
            matched = p.stdout.strip().lower() == f"206|bytes {offset}-{end}/{size}"
            if p.returncode in (0,18,28,56) and matched and 0 < received <= end-offset+1:
                with partial.open("ab") as out, piece.open("rb") as source:
                    shutil.copyfileobj(source, out)
                failures = 0
            else:
                failures += 1
            piece.unlink(missing_ok=True)
            if p.returncode:
                print(name, offset, "range retry", attempt, p.returncode, received, flush=True)
        raise RuntimeError(f"Could not download validated range {name} {start}-{end}")
    starts = list(range(begin, size, CHUNK))
    with ThreadPoolExecutor(max_workers=8) as pool:
        chunks = list(pool.map(block, starts))
    combined = final.with_suffix(".assembling")
    with combined.open("wb") as out:
        for path in ([prefix] if begin else []) + chunks:
            with path.open("rb") as source:
                shutil.copyfileobj(source, out, CHUNK)
    if not valid(combined, model):
        raise RuntimeError("Final model SHA256 mismatch: " + name)
    combined.replace(final)
    print(name, "SHA256 verified", flush=True)
    for path in chunks:
        path.unlink()
    if begin:
        prefix.unlink()
    parts.rmdir()

if __name__ == "__main__":
    for entry in MANIFEST["models"]:
        fetch(entry)
