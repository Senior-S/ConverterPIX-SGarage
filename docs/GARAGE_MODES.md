# Garage CLI modes

These switches are optional. Existing model conversion writes the normal PIM/PIT and auxiliary files unless a garage switch is supplied. Mount priorities follow the order of `-b` arguments, with the last mounted archive taking precedence.

## Capability check

`converter_pix --garage-capabilities` prints only this JSON object and exits successfully:

```json
{"garageFormatVersion":1,"definitionBundle":true,"viewerGeometry":true,"batch":true,"garagePreview":true}
```

## Definition bundle

```text
converter_pix -b def.scs -b mod.zip --extract-bundle /def/vehicle/truck -e E:/ETS2-Garage/definitions.sgbundle
```

The export argument is a file path. The converter creates its parent directory if needed. It writes a sibling temporary file, checks input reads and output writes, flushes and closes it, then replaces the destination atomically. A failed extraction returns a nonzero exit code and leaves an existing destination intact.

The file starts with eight bytes `SGDEFB1` followed by a zero byte. Records continue until EOF. Each record contains, in order:

| Field | Encoding |
| --- | --- |
| Path byte length | Unsigned 32-bit little-endian integer |
| Contents byte length | Unsigned 64-bit little-endian integer |
| Virtual path | UTF-8 bytes, slash separators, leading slash, no terminator |
| Contents | Raw file bytes |

Directories have no records. Empty files have zero-length contents. Both directory enumeration and file opening use the mounted virtual filesystem so a higher priority mod can mask a lower priority resource. There is no record count or footer. Each file is read in one call because HashFS v2 GDeflate entries require full-entry reads. The converter reuses buffer capacity across files; extraction memory follows the largest file, plus decoder workspace, rather than the entire directory. Lengths must fit both the addressable buffer and output stream before allocation.

## Batch conversion

```text
converter_pix -b base.scs -b base_vehicle.scs --batch E:/ETS2-Garage/jobs.tsv --garage-preview --viewer-geometry
```

The UTF-8 manifest accepts an optional BOM and LF or CRLF line endings. Every line has exactly three tab-separated nonempty fields:

```text
model	/vehicle/truck/scania_2016/chassis/chs_6x4_l	E:/ETS2-Garage/chassis
model	/vehicle/truck/scania_2016/chassis/chs_6x4_l	E:/ETS2-Garage/chassis-copy
tobj	/material/environment/vehicle_reflection.tobj	E:/ETS2-Garage/reflection
```

Use literal tab characters in the manifest. Model paths omit `.pmg` and `.pmd`; texture object paths include `.tobj`. Unknown kinds, empty fields, extra fields, blank lines, parent traversal, and malformed lines fail validation before any jobs execute. An empty manifest fails validation. Do not combine batch with another conversion mode or pass extra animation arguments. Each job has its own output directory field; `-e` is not used for batch jobs.

The converter mounts the bases once and clears `ResourceLibrary` before each job, so converted texture state cannot suppress files in later output directories. A failed job does not stop subsequent jobs. The aggregate exit code is nonzero if any job fails. Regular diagnostic lines remain, followed by one JSON status line per job:

```json
{"garageJob":0,"kind":"model","path":"/vehicle/truck/scania_2016/chassis/chs_6x4_l","success":true}
```

`garageJob` is a zero-based manifest index. Model status includes geometry, PIT, and loaded texture export results. An unavailable texture binding that the upstream material loader skipped keeps that behavior.

## Model switches and timing

`--garage-preview` skips collision and prefab loading and auxiliary PIS/PIC/PIP exports. `--viewer-geometry` writes direct viewer geometry instead of PIM and retains PIT and texture conversion. The switches can be used together or independently with `-m`, whole-base conversion, or model batch jobs. See [the viewer geometry format](VIEWER_FORMAT.md) for the binary array layout.

`--show-elapsed-time` prints mounting time separately from the existing conversion timer. The line reports microseconds, milliseconds, and seconds. Whole-base conversion includes its additional primary archive mount in the mounting total.

The existing `copyFile` helper lazily retains one 10 MiB scratch allocation per copying thread. It checks seeking, full reads, and full writes and returns false on failure.
