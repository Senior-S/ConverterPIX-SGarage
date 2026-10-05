#!/usr/bin/env python3
"""Compare upstream ConverterPIX with optional garage modes using real game data.

All output directories must be under E:\\ETS2-Garage. Timings use Windows' normal
filesystem caches, not a cold-disk setup. Run order alternates in every repeat.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import struct
import subprocess
import sys
import time
import zipfile

sys.dont_write_bytecode = True
ROOT = Path("E:/ETS2-Garage")
DEFAULT_MODELS = (
    "/vehicle/truck/scania_2016/chassis/chs_6x4_l",
    "/vehicle/trailer_owned/scs_box/chassis/ch_3",
    "/vehicle/truck/scania_2016/accessory/b_grill/b_grill_01_h",
    "/vehicle/truck/scania_2016/interior/anim",
)


def invoke(executable, bases, arguments, folder, label, *, allow_failure=False):
    """Share command logging, timeout and converter error checks across cases."""
    command = [str(executable), *[item for base in bases for item in ("-b", str(base))], *map(str, arguments)]
    started = time.perf_counter()
    result = subprocess.run(command, cwd=folder, capture_output=True, timeout=600,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    seconds = time.perf_counter() - started
    output = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")
    log = folder / (label + ".log")
    log.write_text(output, encoding="utf-8")
    failed = bool(result.returncode or re.search(r"(?:^|\s)(?:ERROR|FATAL)(?:\s|:)|<error>\s*\d*", output, re.I))
    mount = re.search(r"Mounting time:.*?\|\s*([\d.]+)\s+s", output)
    record = {"command": command, "wallSeconds": seconds, "exitCode": result.returncode,
              "mountSeconds": float(mount[1]) if mount else None, "log": str(log), "failed": failed}
    if failed and not allow_failure:
        raise RuntimeError(f"{label} failed, see {log}: {output[-1500:]}")
    return record, output


def file_digest(root):
    """Compare content and file names independently of timestamps or ordering."""
    return {"/" + path.relative_to(root).as_posix(): (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(root.rglob("*")) if path.is_file()}


def bundle_digest(path):
    """Read the public bundle contract without creating per-file outputs."""
    entries = {}
    with path.open("rb") as file:
        if file.read(8) != b"SGDEFB1\0":
            raise ValueError("Bundle signature differs")
        while header := file.read(12):
            if len(header) != 12:
                raise ValueError("Truncated bundle header")
            path_length, length = struct.unpack("<IQ", header)
            raw_name = file.read(path_length)
            if len(raw_name) != path_length:
                raise ValueError("Truncated bundle path")
            name = raw_name.decode("utf-8")
            if not name.startswith("/") or ".." in name.split("/") or name in entries:
                raise ValueError(f"Invalid or duplicate bundle path {name!r}")
            digest = hashlib.sha256()
            remaining = length
            while remaining:
                block = file.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError(f"Truncated payload for {name}")
                digest.update(block)
                remaining -= len(block)
            entries[name] = (length, digest.hexdigest())
    return entries


def equivalent(expected, actual, label):
    if expected != actual:
        missing = sorted(set(expected) - set(actual))[:10]
        extra = sorted(set(actual) - set(expected))[:10]
        changed = [key for key in expected.keys() & actual.keys() if expected[key] != actual[key]][:10]
        raise AssertionError(f"{label}: missing={missing}, extra={extra}, changed={changed}")
    return {"equivalent": True, "fileCount": len(expected), "bytes": sum(value[0] for value in expected.values())}


def compare_scene(expected, actual, exports, trail="scene"):
    """Keep exact mesh values; permit PIM's decimal locator transform rounding."""
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            raise AssertionError(f"{trail} keys differ: {expected.keys()} / {actual.keys()}")
        for key in expected:
            compare_scene(expected[key], actual[key], exports, trail + "." + key)
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            raise AssertionError(f"{trail} length differs: {len(expected)} / {len(actual)}")
        for index, (left, right) in enumerate(zip(expected, actual)):
            compare_scene(left, right, exports, trail + f"[{index}]")
    elif isinstance(expected, str) and expected.startswith("/cache/"):
        paths = [export.parent.parent / value.removeprefix("/cache/") for export, value in zip(exports, (expected, actual))]
        if hashlib.sha256(paths[0].read_bytes()).digest() != hashlib.sha256(paths[1].read_bytes()).digest():
            raise AssertionError(f"{trail} texture contents differ")
    elif isinstance(expected, (int, float)) and not isinstance(expected, bool) and "locators" in trail:
        if not math.isclose(expected, actual, rel_tol=1e-5, abs_tol=1e-5):
            raise AssertionError(f"{trail}: {expected!r} != {actual!r}")
    elif expected != actual:
        raise AssertionError(f"{trail}: {expected!r} != {actual!r}")


def compare_streams(assets, pim, manifest):
    """Validate every UV/extra stream, including pieces hidden by the viewer."""
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    binary = (manifest.parent / metadata["binary"]).read_bytes()
    if metadata["binaryBytes"] != len(binary):
        raise AssertionError("Geometry binary length differs from manifest")
    pim_text = pim.read_text(encoding="utf-8")
    baseline = assets._blocks(pim_text, "Piece")
    expected_materials = [{"alias": assets._properties(body).get("Alias", [""])[-1],
                           "effect": assets._properties(body).get("Effect", [""])[-1]}
                          for body in assets._blocks(pim_text, "Material")]
    expected_parts = []
    for body in assets._blocks(pim_text, "Part"):
        fields = assets._properties(body)
        expected_parts.append({"name": fields.get("Name", [""])[-1],
                               "pieces": [int(value) for value in re.findall(r"\d+", " ".join(fields.get("Pieces", [])))],
                               "locators": [int(value) for value in re.findall(r"\d+", " ".join(fields.get("Locators", [])))]})
    if expected_materials != metadata["materials"] or expected_parts != metadata["parts"]:
        raise AssertionError("Raw material aliases/effects or part mappings differ")
    expected_locators = []
    for index, body in enumerate(assets._blocks(pim_text, "Locator")):
        fields = assets._properties(body)
        rotation = assets._float_text(fields.get("Rotation", [""])[-1])
        expected_locators.append({"index": index, "name": fields.get("Name", [""])[-1],
                                  "position": assets._float_text(fields.get("Position", [""])[-1]),
                                  "rotation": rotation[1:] + rotation[:1],
                                  "scale": assets._float_text(fields.get("Scale", [""])[-1]),
                                  "hookup": fields.get("Hookup", [None])[-1]})
    compare_scene(expected_locators, metadata["locators"], (), "all.locators")
    if len(baseline) != len(metadata["pieces"]):
        raise AssertionError("Raw piece count differs")
    stream_count = 0
    skin_stream_count = 0
    for body, piece in zip(baseline, metadata["pieces"]):
        fields = assets._properties(body)
        if int(fields.get("Material", ["0"])[-1]) != piece["material"]:
            raise AssertionError(f"Raw material index differs for piece {piece['index']}")
        aliases = {}
        for stream in assets._blocks(body, "Stream"):
            tag = assets._properties(stream)["Tag"][-1].strip('"')
            if tag.startswith("_UV"):
                value = re.search(r"Aliases:\s*([^\r\n]*)", stream)
                aliases[tag] = re.findall(r'"([^"\n]*)"', value[1]) if value else []
        if aliases != piece["uvAliases"]:
            raise AssertionError(f"UV aliases differ for piece {piece['index']}")
        expected = {assets._properties(stream)["Tag"][-1].strip('"'): assets._vectors(stream)
                    for stream in assets._blocks(body, "Stream")}
        for tag, descriptor in piece["streams"].items():
            if tag in ("_BONE_INDEX", "_BONE_WEIGHT"):
                if descriptor["type"] != "u8" or descriptor["count"] != piece["vertexCount"] * descriptor["components"]:
                    raise AssertionError(f"Invalid skin stream {tag}")
                skin_stream_count += 1
                continue
            if tag not in expected:
                raise AssertionError(f"Extra exported stream {tag}")
            kind = "f" if descriptor["type"] == "f32" else "I"
            values = list(struct.unpack_from("<" + str(descriptor["count"]) + kind, binary, descriptor["offset"]))
            if values != expected.pop(tag):
                raise AssertionError(f"Raw stream differs for piece {piece['index']} {tag}")
            stream_count += 1
        if expected:
            raise AssertionError(f"Missing exported streams {list(expected)}")
        triangles = [int(value) for block in assets._blocks(body, "Triangles")
                     for match in re.finditer(r'^\s*\d+\s*\(\s*(\d+)\s+(\d+)\s+(\d+)\s*\)', block, re.M)
                     for value in match.groups()]
        descriptor = piece["indices"]
        if list(struct.unpack_from("<" + str(descriptor["count"]) + "I", binary, descriptor["offset"])) != triangles:
            raise AssertionError(f"Raw triangle indices differ for piece {piece['index']}")
    if skin_stream_count:
        expected_skin = {}
        for match in re.finditer(r"Weights:\s*(\d+)\s*([^\r\n]*)[\r\n]+\s*Clones:\s*1\s+(\d+)\s+(\d+)", pim_text):
            weights = assets._numbers(match[2])
            if len(weights) != int(match[1]) * 2:
                raise AssertionError("PIM skin weight count differs")
            expected_skin[(int(match[3]), int(match[4]))] = list(zip(weights[::2], weights[1::2]))
        actual_skin = {}
        for piece in metadata["pieces"]:
            if "_BONE_WEIGHT" not in piece["streams"]:
                continue
            index_stream, weight_stream = (piece["streams"][name] for name in ("_BONE_INDEX", "_BONE_WEIGHT"))
            indices = binary[index_stream["offset"]:index_stream["offset"] + index_stream["count"]]
            weights = binary[weight_stream["offset"]:weight_stream["offset"] + weight_stream["count"]]
            width = weight_stream["components"]
            for vertex in range(piece["vertexCount"]):
                actual_skin[(piece["index"], vertex)] = [(index, struct.unpack("<f", struct.pack("<f", weight / 255))[0])
                    for index, weight in zip(indices[vertex * width:(vertex + 1) * width], weights[vertex * width:(vertex + 1) * width]) if weight]
        if expected_skin != actual_skin:
            raise AssertionError("Native skin indices/weights differ from PIM Skin")
        from backend.converter_formats import read_viewer_model
        decoded = read_viewer_model(manifest)
        pis_text = pim.with_suffix(".pis").read_text(encoding="utf-8")
        baseline_bones = re.findall(r'\d+\s*\(\s*Name:\s*"([^"\n]*)"\s+Parent:\s*"([^"\n]*)"\s+Matrix:\s*\(([^)]*)\)', pis_text)
        if len(baseline_bones) != len(decoded["bones"]):
            raise AssertionError("Bone count differs from PIS")
        for expected_bone, bone in zip(baseline_bones, decoded["bones"]):
            parent = "" if bone["parent"] == 255 else decoded["bones"][bone["parent"]]["name"]
            native_matrix_for_pis = [bone["matrix"][column * 4 + row] for row in range(4) for column in range(4)]
            if expected_bone[0] != bone["name"] or expected_bone[1] != parent or assets._numbers(expected_bone[2]) != native_matrix_for_pis:
                raise AssertionError(f"Bone name/parent/matrix differs for {bone['name']}")
    return {"rawStreams": stream_count, "rawPieces": len(baseline), "skinStreams": skin_stream_count,
            "bones": len(metadata["bones"]), "allMetadataEquivalent": True}


def synthetic_cases(options, folder):
    base = folder / "synthetic-base.zip"
    overlay = folder / "synthetic-overlay.zip"
    expected = {"/fixture/empty.bin": b"", "/fixture/tiny.sii": b"SiiNunit { }\n",
                "/fixture/large.bin": bytes(range(256)) * (11 * 1024 * 1024 // 256 + 1),
                "/fixture/shared.sii": b"base\n", "/fixture/sub/nested.sui": b"nested\n"}
    with zipfile.ZipFile(base, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in expected.items():
            archive.writestr(path.lstrip("/"), data)
        archive.writestr("empty-directory/", b"")
    with zipfile.ZipFile(overlay, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("fixture/shared.sii", b"override\n")
    records = []
    for name, bases in (("base", [base]), ("overlay", [base, overlay])):
        wanted = {key: (len(data), hashlib.sha256(data).hexdigest()) for key, data in expected.items()}
        if name == "overlay":
            wanted["/fixture/shared.sii"] = (9, hashlib.sha256(b"override\n").hexdigest())
        for implementation, executable in (("baseline", options.baseline), ("candidate", options.candidate)):
            export = folder / (name + "-" + implementation)
            export.mkdir()
            record, _ = invoke(executable, bases, ["-e", export, "--extract-directory", "/fixture"], folder, name + "-" + implementation)
            record.update(equivalent(wanted, file_digest(export), name + "-" + implementation))
            records.append(record)
        bundle = folder / (name + ".sgdef")
        record, _ = invoke(options.candidate, bases, ["-e", bundle, "--extract-bundle", "/fixture"], folder, name + "-bundle")
        record.update(equivalent(wanted, bundle_digest(bundle), name + "-bundle"))
        records.append(record)
    for switch, path in (("--extract-file", "/fixture/tiny.sii"), ("--show-file", "/fixture/tiny.sii"),
                         ("--list-directory", "/fixture"), ("--list-directory-recursive", "/fixture"),
                         ("--calc-cityhash64", "garage-regression")):
        outputs = []
        for implementation, executable in (("baseline", options.baseline), ("candidate", options.candidate)):
            export = folder / (switch[2:] + "-" + implementation)
            export.mkdir()
            _, output = invoke(executable, [base], ["-e", export, switch, path], folder, switch[2:] + "-" + implementation)
            outputs.append(output)
        if outputs[0] != outputs[1]:
            raise AssertionError(f"Default CLI output changed for {switch}")
    # Batch validation must reject all jobs before any output is created.
    untouched = folder / "malformed-output"
    malformed = folder / "malformed.tsv"
    malformed.write_text(f"model\t{DEFAULT_MODELS[0]}\t{untouched}\nnot-a-job\n", encoding="utf-8")
    record, output = invoke(options.candidate, [], ["--batch", malformed], folder, "malformed-batch", allow_failure=True)
    if not record["failed"] or untouched.exists() or '"garageJob"' in output:
        raise AssertionError("Malformed batch was accepted or executed a job")
    records.append(record)
    empty_bundle = folder / "empty-directory.sgdef"
    record, _ = invoke(options.candidate, [base], ["-e", empty_bundle, "--extract-bundle", "/empty-directory"], folder, "empty-directory")
    record.update(equivalent({}, bundle_digest(empty_bundle), "empty directory"))
    records.append(record)
    bundle = folder / "preserve-on-failure.sgdef"
    bundle.write_bytes(b"keep-existing-output")
    record, _ = invoke(options.candidate, [base], ["-e", bundle, "--extract-bundle", "/missing-directory"], folder, "bundle-failure", allow_failure=True)
    if not record["failed"] or bundle.read_bytes() != b"keep-existing-output":
        raise AssertionError("Failed bundle extraction modified existing final output")
    records.append(record)
    blocked_target = folder / "bundle-directory-target"
    blocked_target.mkdir()
    record, _ = invoke(options.candidate, [base], ["-e", blocked_target, "--extract-bundle", "/fixture"], folder, "bundle-publish-failure", allow_failure=True)
    if not record["failed"] or list(blocked_target.iterdir()):
        raise AssertionError("Bundle publish failure was not reported")
    records.append(record)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--garage-repo", required=True, type=Path)
    parser.add_argument("--run-folder", type=Path)
    parser.add_argument("--game", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--model", action="append")
    parser.add_argument("--skip-game", action="store_true", help="Only run synthetic and CLI regressions")
    parser.add_argument("--definitions", default="/def/vehicle/truck/scania.s_2016")
    options = parser.parse_args()
    folder = options.run_folder or ROOT / "benchmarks" / time.strftime("converter-fork-%Y%m%d-%H%M%S")
    folder = folder.resolve()
    if not folder.is_relative_to(ROOT.resolve()):
        parser.error("All run outputs must stay under E:\\ETS2-Garage")
    folder.mkdir(parents=True, exist_ok=True)
    for key in ("TEMP", "TMP", "ETS2_CACHE_PATH"):
        os.environ[key] = str(folder / "temporary")
    (folder / "temporary").mkdir(exist_ok=True)
    sys.path.insert(0, str(options.garage_repo))
    from backend import assets
    game = options.game or assets._detect_game_path()
    archives = sorted(game.glob("*.scs"), key=lambda path: path.name.casefold()) if game else []
    models = options.model or DEFAULT_MODELS
    results = {"baseline": str(options.baseline), "candidate": str(options.candidate),
               "baselineSha256": hashlib.sha256(options.baseline.read_bytes()).hexdigest(),
               "candidateSha256": hashlib.sha256(options.candidate.read_bytes()).hexdigest(),
               "assetsSha256": hashlib.sha256(Path(assets.__file__).read_bytes()).hexdigest(),
               "game": str(game), "archives": [str(path) for path in archives],
               "cacheConditions": "Normal Windows filesystem caches, new output folders; not cold disk",
               "records": [], "checks": [], "complete": False}
    result_path = folder / "results.json"
    try:
        synthetic = folder / "synthetic"
        synthetic.mkdir()
        results["checks"].extend(synthetic_cases(options, synthetic))
        print("Synthetic extraction and CLI checks passed", flush=True)
        _, capabilities = invoke(options.candidate, [], ["--garage-capabilities"], folder, "capabilities")
        results["capabilities"] = capabilities
        if not options.skip_game:
            if not archives:
                raise ValueError("No installed game archives found")
            resources = folder / "real-resource-files"
            resources.mkdir()
            resource_bundle = folder / "real-resource.sgdef"
            resource_path = "/vehicle/truck/scania_2016/chassis"
            resource_records = []
            for implementation, target, switch in (("baseline", resources, "--extract-directory"), ("candidate", resource_bundle, "--extract-bundle")):
                record, _ = invoke(getattr(options, implementation), archives, ["-e", target, switch, resource_path],
                                   folder, "real-resource-" + implementation)
                record.update({"case": "realResources", "implementation": implementation})
                resource_records.append(record)
            resource_hashes = file_digest(resources)
            results["checks"].append({**equivalent(resource_hashes, bundle_digest(resource_bundle), "real PMD/PMG resource bundle"),
                                      "largestFileBytes": max(value[0] for value in resource_hashes.values())})
            results["records"].extend(resource_records)
            print("Real PMD/PMG bundle check passed", flush=True)
            for repeat in range(options.repeats):
                order = ("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline")
                for implementation in order:
                    record, _ = invoke(getattr(options, implementation), archives,
                                       ["-e", folder / "mount-probe-unused", "--list-directory", options.definitions, "--show-elapsed-time"],
                                       folder, f"mount-probe-{repeat}-{implementation}")
                    record.update({"case": "mountProbe", "repeat": repeat, "implementation": implementation})
                    results["records"].append(record)
                extracted = {}
                for implementation in order:
                    executable = getattr(options, implementation)
                    label = f"definitions-{repeat}-{implementation}"
                    target = folder / label
                    if implementation == "baseline":
                        target.mkdir()
                        arguments = ["-e", target, "--extract-directory", options.definitions, "--show-elapsed-time"]
                    else:
                        target = target.with_suffix(".sgdef")
                        arguments = ["-e", target, "--extract-bundle", options.definitions, "--show-elapsed-time"]
                    record, _ = invoke(executable, archives, arguments, folder, label)
                    started = time.perf_counter()
                    extracted[implementation] = file_digest(target) if implementation == "baseline" else bundle_digest(target)
                    record["readHashSeconds"] = time.perf_counter() - started
                    record.update({"case": "definitions", "repeat": repeat, "implementation": implementation,
                                   "rawBytes": sum(v[0] for v in extracted[implementation].values()),
                                   "outputBytes": sum(v[0] for v in extracted[implementation].values()) if implementation == "baseline" else target.stat().st_size,
                                   "fileCount": len(extracted[implementation]),
                                   "outputFileCount": len(extracted[implementation]) if implementation == "baseline" else 1})
                    results["records"].append(record)
                results["checks"].append(equivalent(extracted["baseline"], extracted["candidate"], "definitions"))
                print(f"Definition repeat {repeat + 1} equivalent", flush=True)
                for model_index, model in enumerate(models):
                    exports = {}
                    for implementation in order:
                        export = folder / "models" / f"{repeat}-{model_index}-{implementation}"
                        export.mkdir(parents=True)
                        exports[implementation] = export
                        label = f"model-{repeat}-{model_index}-{implementation}"
                        extra = ["--garage-preview", "--viewer-geometry"] if implementation == "candidate" else []
                        record, _ = invoke(getattr(options, implementation), archives,
                                           ["-e", export, "-m", model, "--show-elapsed-time", *extra], folder, label)
                        digest = file_digest(export)
                        record.update({"case": "model", "model": model, "repeat": repeat, "implementation": implementation,
                                       "fileCount": len(digest), "outputBytes": sum(v[0] for v in digest.values()), "exportDigest": digest})
                        geometry = export / (model.lstrip("/") + (".pim" if implementation == "baseline" else ".sgm"))
                        pit = geometry.with_suffix(".pit")
                        started = time.perf_counter()
                        record["scene"] = assets._parse_model(geometry, pit if pit.is_file() else None, export)
                        record["parseSeconds"] = time.perf_counter() - started
                        results["records"].append(record)
                    pair = results["records"][-2:]
                    scenes = {item["implementation"]: item.pop("scene") for item in pair}
                    compare_scene(scenes["baseline"], scenes["candidate"], (exports["baseline"], exports["candidate"]))
                    base_geometry = exports["baseline"] / (model.lstrip("/") + ".pim")
                    new_geometry = exports["candidate"] / (model.lstrip("/") + ".sgm")
                    raw = compare_streams(assets, base_geometry, new_geometry)
                    baseline_pit, candidate_pit = base_geometry.with_suffix(".pit"), new_geometry.with_suffix(".pit")
                    if baseline_pit.read_bytes() != candidate_pit.read_bytes():
                        raise AssertionError("PIT looks, variants or materials differ")
                    if repeat == 0:
                        pit_text = baseline_pit.read_text(encoding="utf-8")
                        looks = [assets._properties(body).get("Name", ["default"])[-1].strip('"') for body in assets._blocks(pit_text, "Look")]
                        variants = [assets._properties(body).get("Name", ["default"])[-1].strip('"') for body in assets._blocks(pit_text, "Variant")]
                        if len(looks) > 1 or len(variants) > 1:
                            selection = (looks[-1] if looks else None, variants[-1] if variants else None)
                            selected = [assets._parse_model(path, path.with_suffix(".pit"), export, *selection)
                                        for path, export in ((base_geometry, exports["baseline"]), (new_geometry, exports["candidate"]))]
                            compare_scene(*selected, (exports["baseline"], exports["candidate"]))
                            raw["selectedLookVariantEquivalent"] = list(selection)
                        if model_index == 2:
                            default_export = folder / "default-model"
                            default_export.mkdir()
                            default_record, _ = invoke(options.candidate, archives, ["-e", default_export, "-m", model], folder, "default-model")
                            baseline_digest = next(item["exportDigest"] for item in pair if item["implementation"] == "baseline")
                            results["checks"].append(equivalent(baseline_digest, file_digest(default_export), "default model CLI output"))
                            results["records"].append(default_record)
                    results["checks"].append({"model": model, "repeat": repeat, "sceneEquivalent": True, **raw})
                    print(f"Model {model} repeat {repeat + 1} equivalent", flush=True)
                    result_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
            batch = folder / "batch.tsv"
            first, second = folder / "batch-first", folder / "batch-second"
            batch.write_text(f"model\t{models[0]}\t{first}\nmodel\t/does/not/exist\t{folder / 'batch-missing'}\nmodel\t{models[0]}\t{second}\n", encoding="utf-8")
            record, output = invoke(options.candidate, archives, ["--batch", batch, "--garage-preview", "--viewer-geometry"], folder, "batch", allow_failure=True)
            reports = [json.loads(line) for line in output.splitlines() if line.startswith('{"garageJob"')]
            if [entry["success"] for entry in reports] != [True, False, True] or [entry["garageJob"] for entry in reports] != [0, 1, 2] or not record["failed"]:
                raise AssertionError(f"Batch did not report per-job failure and continue: {reports}")
            results["checks"].append(equivalent(file_digest(first), file_digest(second), "same model in separate batch directories"))
            if not any(second.rglob("*.dds")):
                raise AssertionError("Batch fixture had no textures, texture-directory validation would be empty")
            results["records"].append(record)
            stale_manifest = second / (models[0].lstrip("/") + ".sgm")
            record, _ = invoke(options.candidate, [synthetic / "synthetic-base.zip"],
                               ["-e", second, "-m", models[0], "--garage-preview", "--viewer-geometry"],
                               folder, "failed-native-retry", allow_failure=True)
            if not record["failed"] or stale_manifest.exists():
                raise AssertionError("A failed native retry left a previously complete manifest")
            results["checks"].append({"staleNativeManifestRemoved": True})
            results["records"].append(record)
        results["complete"] = True
        results["medians"] = []
        groups = {(item["case"], item.get("model"), item["implementation"])
                  for item in results["records"] if "case" in item}
        for case, model, implementation in sorted(groups, key=str):
            records = [item for item in results["records"] if (item.get("case"), item.get("model"), item.get("implementation")) == (case, model, implementation)]
            summary = {"case": case, "model": model, "implementation": implementation, "samples": len(records)}
            for key in ("wallSeconds", "parseSeconds", "mountSeconds", "outputBytes", "rawBytes", "fileCount", "outputFileCount", "readHashSeconds"):
                values = [item[key] for item in records if item.get(key) is not None]
                if values:
                    summary[key] = statistics.median(values)
            results["medians"].append(summary)
        print(json.dumps(results["medians"], indent=2), flush=True)
    except Exception as error:
        results["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        # Avoid retaining full scene arrays in an error result.
        for record in results["records"]:
            record.pop("scene", None)
        result_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"Results: {result_path}", flush=True)


if __name__ == "__main__":
    main()
