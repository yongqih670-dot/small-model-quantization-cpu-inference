"""Construct a private instrumented copy of the pinned Windows CPU release."""
from pathlib import Path
import hashlib
import re
import shutil
import subprocess
import zipfile

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
ARCHIVE = ASSETS / "llama-b11347-bin-win-cpu-x64.zip"
EXPECTED = "f1f7198a7c1cdf13bec7c2cf960947e0bec87401837a057134186132d870aa04"
if hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() != EXPECTED:
    raise SystemExit("Official archive SHA256 mismatch")
output = HERE.parent / "timed_backend"
output.mkdir(exist_ok=True)
with zipfile.ZipFile(ARCHIVE) as archive:
    for entry in archive.namelist():
        if Path(entry).name != entry:
            raise ValueError(entry)
        dest = output / ("llama_original.dll" if entry == "llama.dll" else entry)
        with archive.open(entry) as source, dest.open("wb") as target:
            shutil.copyfileobj(source, target)

def exports(path):
    dump = subprocess.check_output(["objdump", "-p", str(path)], text=True)
    base = int(re.search(r"Ordinal Base\s+(\d+)", dump).group(1))
    names = dump.split("[Ordinal/Name Pointer] Table", 1)[1].strip().split("\n\n", 1)[0]
    return {name: base + int(index) for index, name in re.findall(r"^\s*\[\s*(\d+)\]\s+(\S+)", names, re.M)}

original = exports(output / "llama_original.dll")
timed = "llama_model_load_from_file"
assert timed in original and len(original) > 100
lines = ["LIBRARY llama", "EXPORTS"]
for name, ordinal in original.items():
    dest = "" if name == timed else '="llama_original.' + name + '"'
    lines.append(f'  "{name}"{dest} @{ordinal}')
definition = HERE / "exports.def"
definition.write_text("\n".join(lines) + "\n", encoding="ascii")
target = output / "llama.dll"
subprocess.run(["gcc", "-std=c11", "-O2", "-shared", "-I", str(HERE / "include"), str(HERE / "load_timer.c"),
                str(definition), "-o", str(target)], check=True)
assert exports(target) == original
print(f"{len(original)} matching export names and ordinals; timer SHA256 {hashlib.sha256(target.read_bytes()).hexdigest()}")
