#!/usr/bin/env python3
"""Exercise garage CLI validation and failures without installed game archives."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("--converter", type=Path, required=True)
parser.add_argument("--baseline", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output = args.output.resolve()
if not args.output.is_relative_to(Path("E:/ETS2-Garage").resolve()):
    parser.error("All outputs must stay under E:/ETS2-Garage")
args.output.mkdir(parents=True, exist_ok=False)
repo = Path(__file__).resolve().parents[1]
base = args.output / "base"
(base / "def").mkdir(parents=True)
(base / "def/empty.sii").write_bytes(b"")
(base / "def/sample.sii").write_bytes(b"SiiNunit { sample : value {} }\n")
for filename in ("some_texture.tobj", "some_texture.dds"):
    shutil.copyfile(repo / "test/tobj_hashfs_v2/data" / filename, base / filename)
results = []


def run(label, arguments, *, expected=0, executable=None):
    command = [str(executable or args.converter), *map(str, arguments)]
    process = subprocess.run(command, capture_output=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output = process.stdout.decode("utf-8", errors="replace")
    (args.output / (label + ".log")).write_bytes(process.stdout + b"\n" + process.stderr)
    assert (process.returncode == 0) == (expected == 0), (label, process.returncode, output)
    results.append({"case": label, "exitCode": process.returncode})
    return output


def files(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob("*") if p.is_file()}


capabilities = json.loads(run("capabilities", ["--garage-capabilities"]))
assert capabilities == {"garageFormatVersion": 1, "definitionBundle": True,
                        "viewerGeometry": True, "batch": True, "garagePreview": True}
for option in ("--batch", "--extract-bundle", "-b", "-m", "-e"):
    run("missing-" + option.lstrip("-"), [option], expected=1)
run("conflicting-modes", ["-b", base, "--extract-bundle", "/def", "-m", "/bad", "-e", args.output / "bad"], expected=1)
run("missing-bundle-export", ["-b", base, "--extract-bundle", "/def"], expected=1)

bundle = args.output / "defs.bundle"
run("bundle", ["-b", base, "--extract-bundle", "/def", "-e", bundle, "--show-elapsed-time"])
data = bundle.read_bytes()
assert data[:8] == b"SGDEFB1\0"
records, offset = {}, 8
while offset < len(data):
    path_size, content_size = struct.unpack_from("<IQ", data, offset)
    offset += 12
    path = data[offset:offset + path_size].decode("utf-8")
    offset += path_size
    records[path] = data[offset:offset + content_size]
    offset += content_size
assert offset == len(data)
assert records == {"/def/empty.sii": b"", "/def/sample.sii": (base / "def/sample.sii").read_bytes()}
previous = data
large_zip = args.output / "large.zip"
large_contents = b"full-entry-read" * (11 * 1024 * 1024 // len(b"full-entry-read") + 1)
with zipfile.ZipFile(large_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("large/contents.sii", large_contents)
large_bundle = args.output / "large.bundle"
run("bundle-large-zip", ["-b", large_zip, "--extract-bundle", "/large", "-e", large_bundle])
large_data = large_bundle.read_bytes()
path_size, content_size = struct.unpack_from("<IQ", large_data, 8)
assert content_size == len(large_contents)
assert large_data[20:20 + path_size] == b"/large/contents.sii"
assert large_data[20 + path_size:] == large_contents
run("missing-bundle-directory", ["-b", base, "--extract-bundle", "/missing", "-e", bundle], expected=1)
assert bundle.read_bytes() == previous
blocked = args.output / "directory-target"
blocked.mkdir()
run("bundle-publish-failure", ["-b", base, "--extract-bundle", "/def", "-e", blocked], expected=1)
assert blocked.is_dir() and not list(args.output.glob("directory-target.tmp.*"))
corrupt_zip = args.output / "corrupt.zip"
with zipfile.ZipFile(corrupt_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("def/corrupt.sii", b"X" * 1000)
with zipfile.ZipFile(corrupt_zip) as archive:
    info = archive.getinfo("def/corrupt.sii")
    corrupt = bytearray(corrupt_zip.read_bytes())
    filename_size, extra_size = struct.unpack_from("<HH", corrupt, info.header_offset + 26)
    corrupt[info.header_offset + 30 + filename_size + extra_size] = 0xff
    corrupt_zip.write_bytes(corrupt)
run("bundle-short-read", ["-b", base, "-b", corrupt_zip, "--extract-bundle", "/def", "-e", bundle], expected=1)
assert bundle.read_bytes() == previous and not list(args.output.glob("defs.bundle.tmp.*"))

normal = args.output / "normal"
baseline = args.output / "baseline"
run("normal-tobj", ["-b", base, "-t", "/some_texture.tobj", "-e", normal])
run("baseline-tobj", ["-b", base, "-t", "/some_texture.tobj", "-e", baseline], executable=args.baseline)
assert files(normal) == files(baseline)
manifest = args.output / "jobs.tsv"
not_created = args.output / "invalid-output"
for i, bad_line in enumerate(("unknown\t/x\t/y", "model\t/x", "model\t\t/y", "model\t/x\t/y\textra", "", "model\t/x.pmg\t/y", "model\t/../x\t/y")):
    manifest.write_text(f"tobj\t/some_texture.tobj\t{not_created}\n{bad_line}\n", encoding="utf-8")
    run(f"invalid-manifest-{i}", ["-b", base, "--batch", manifest], expected=1)
    assert not not_created.exists()
manifest.write_text("", encoding="utf-8")
run("empty-manifest", ["-b", base, "--batch", manifest], expected=1)
first, second = args.output / "batch-first", args.output / "batch-second"
manifest.write_text(f"tobj\t/some_texture.tobj\t{first}\nmodel\t/missing\t{args.output / 'missing'}\ntobj\t/some_texture.tobj\t{second}\n", encoding="utf-8-sig", newline="\r\n")
output = run("mixed-batch", ["-b", base, "--batch", manifest], expected=1)
statuses = [json.loads(line) for line in output.splitlines() if line.startswith('{"garageJob":')]
assert [(item["garageJob"], item["success"]) for item in statuses] == [(0, True), (1, False), (2, True)]
assert files(first) == files(second) == files(normal)
blocked_texture = args.output / "blocked-texture"
(blocked_texture / "some_texture.dds").mkdir(parents=True)
manifest.write_text(f"tobj\t/some_texture.tobj\t{blocked_texture}\ntobj\t/some_texture.tobj\t{args.output / 'after-failure'}\n", encoding="utf-8")
output = run("texture-write-failure", ["-b", base, "--batch", manifest], expected=1)
statuses = [json.loads(line) for line in output.splitlines() if line.startswith('{"garageJob":')]
assert [item["success"] for item in statuses] == [False, True]
old_export = args.output / "old-export"
old_export.mkdir()
(old_export / "missing.sgm").write_text("old manifest", encoding="utf-8")
run("failed-native-load-clears-manifest", ["-b", base, "-m", "/missing", "-e", old_export, "--viewer-geometry"], expected=1)
assert not (old_export / "missing.sgm").exists()
(args.output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps({"passed": len(results), "output": str(args.output)}))
