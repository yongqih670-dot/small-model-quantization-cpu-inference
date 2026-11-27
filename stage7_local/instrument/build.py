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
mingw = bool(shutil.which("gcc") and shutil.which("objdump"))
if not mingw and not all(shutil.which(tool) for tool in ("cl.exe", "dumpbin.exe", "lib.exe")):
    raise SystemExit("Use MinGW-w64 on PATH or the Visual Studio x64 Native Tools prompt.")
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
    if not mingw:
        dump = subprocess.check_output(["dumpbin.exe", "/nologo", "/exports", str(path)], text=True)
        return {name: int(ordinal) for ordinal, name in re.findall(
            r"^ +([0-9]+) +[0-9A-F]+ +(?:[0-9A-F]{8} +)?(\S+)", dump, re.M)}
    dump = subprocess.check_output(["objdump", "-p", str(path)], text=True)
    base = int(re.search(r"Ordinal Base\s+(\d+)", dump).group(1))
    names = dump.split("[Ordinal/Name Pointer] Table", 1)[1].strip().split("\n\n", 1)[0]
    return {name: base + int(index) for index, name in re.findall(r"^\s*\[\s*(\d+)\]\s+(\S+)", names, re.M)}

original = exports(output / "llama_original.dll")
timed = "llama_model_load_from_file"
assert timed in original and len(original) > 100
lines = ["LIBRARY llama", "EXPORTS"]
for name, ordinal in original.items():
    if mingw:
        entry = f'"{name}"' if name == timed else f'"{name}"="llama_original.{name}"'
    else:
        entry = name if name == timed else f"{name}=llama_original.{name}"
    lines.append(f"  {entry} @{ordinal}")
definition = HERE / "exports.def"
definition.write_text("\n".join(lines) + "\n", encoding="ascii")
target = output / "llama.dll"
if mingw:
    command = ["gcc", "-std=c11", "-O2", "-shared", "-I", str(HERE / "include"), str(HERE / "load_timer.c"),
               str(definition), "-o", str(target)]
else:
    # LIB emits the export table for the mixed C and C++ forwarders.
    subprocess.run(["lib.exe", "/nologo", "/MACHINE:X64", "/DEF:" + str(definition),
                    "/OUT:llama_timer.lib"], cwd=output, check=True)
    command = ["cl.exe", "/nologo", "/utf-8", "/std:c11", "/O2", "/LD", "/MT", "/I" + str(HERE / "include"),
               str(HERE / "load_timer.c"), "llama_timer.exp", "/link", "/OUT:" + str(target)]
subprocess.run(command, cwd=output, check=True)
assert exports(target) == original
print(f"{len(original)} matching export names and ordinals; timer SHA256 {hashlib.sha256(target.read_bytes()).hexdigest()}")
