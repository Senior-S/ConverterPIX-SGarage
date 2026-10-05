# Garage viewer geometry format

`--viewer-geometry` exports `<model>.sgm` and `<model>.sgb` instead of PIM. PIT still holds material traits, looks and variants; normal texture conversion still runs. The manifest follows the original model's virtual directory under the output directory.

## Manifest

The `.sgm` file is UTF-8 JSON. Version 1 has these fields:

| Field | Value |
| --- | --- |
| `format` | `"SGarageModel"` |
| `version` | `1` |
| `binary` | Sibling `.sgb` filename, without directories |
| `binaryBytes` | Exact binary file length, including alignment padding |
| `name`, `source` | Model name and converter version |
| `materials` | Ordered `{alias, effect}` records from the first look, matching PIM declarations |
| `parts` | Ordered `{name, pieces, locators}` records with arrays of original piece and locator indices |
| `pieces` | Ordered mesh records described below |
| `locators` | Ordered locator records described below |
| `bones` | Ordered skeleton records, or an empty array |

Every piece, locator and bone has an `index` equal to its array position. Parts retain their original mappings. Missing material declarations remain missing; consumers can use the same neutral material fallback as their PIM reader.

## Binary arrays

The `.sgb` file has no header. Each array descriptor has `offset`, `count`, `components` and `type`. `offset` is a byte offset; `count` is the number of scalar values, including all components, rather than the number of vertices. Types are `f32`, `u32` and `u8`, with widths of four, four and one byte respectively. All multibyte values use little-endian encoding. Each offset is aligned to its scalar width. Any padding between arrays consists of zero bytes and is excluded from their counts.

A piece has `index`, `material`, `vertexCount`, `boneCount`, `streams`, `uvAliases` and `indices`. The material value is the original signed material index. `indices` is a `u32` descriptor with `components: 1`, three scalar values per triangle. Triangle winding and vertex stream values are identical to the PIM exporter, without coordinate, UV or normal transforms.

The `streams` object maps tags to descriptors:

| Tag | Type | Components |
| --- | --- | --- |
| `_POSITION`, `_NORMAL` | `f32` | 3 |
| `_TANGENT`, `_RGBA`, `_FACTOR` | `f32` | 4 |
| `_UV0` through `_UV3` | `f32` | 2 |
| `_BONE_INDEX`, `_BONE_WEIGHT` | `u8` | `boneCount` |

Optional decoded streams are omitted if absent. Each present stream has `vertexCount * components` scalar values. `uvAliases` maps each emitted `_UVn` tag to its PIM `_TEXCOORDn` aliases; aliases do not add binary arrays.

Skinned pieces have `boneCount` between one and eight and both bone streams. Slots retain their original order and zero weights. `_BONE_WEIGHT` contains the original bytes; divide by 255 and round to float32 to reproduce PIM weights. A bone index only needs to be valid when its corresponding weight is nonzero. Static pieces have `boneCount: 0` and no bone streams. Positions remain the decoded bind geometry, as in PIM.

## Locators and skeleton

Each locator has `index`, `name`, `position`, `rotation`, `scale` and `hookup`. Position and scale are arrays of three numbers. Rotation is a quaternion in `[x, y, z, w]` order, already converted from PIM's `[w, x, y, z]` order. Hookup is a plain string or `null` when absent.

Each bone has `index`, `name`, `parent`, `matrix`, `inverseMatrix`, `translation`, `rotation`, `scale`, `stretch` and `determinantSign`. Parent is another bone index, or `255` for a root. Both matrices are arrays of 16 numbers in row-major order, with translation at indices 3, 7 and 11. The PIS exporter uses the transpose when flattening its matrix; transpose a PIS matrix before comparing its scalar order with the native manifest. Translation and scale have three numbers; rotation and stretch have four numbers in `[x, y, z, w]` order. All emitted float values must be finite. JSON float metadata preserves the float32 value promoted to double, matching Python PIM parsing.

`--garage-preview` skips separate PIS, PIC and PIP exports and collision/prefab loading. The native manifest still retains skeleton and locator data decoded from the model.

## Completion and failures

The converter removes an existing viewer manifest before starting a replacement job. It writes sibling `.tmp` files, checks writes, flushes and closes both files, then publishes the binary and manifest in that order. A manifest is the completion marker. It is only published after geometry, PIT, textures and any required auxiliary exports succeed. A failed job returns failure and leaves no complete manifest for that job. A leftover binary alone is incomplete and must not be consumed.

The converter serializes jobs; callers must not export the same model to the same output directory concurrently. This publication protocol prevents incomplete new files from being accepted but does not provide concurrent readers with a snapshot across a replacement.
