# Phase 2 results: STEP import from Inventor (2026-11-20)

Fusion 2706.1.17 (Python 3.14.7, modelling orientation Z-up); Inventor 2027.1.

## Built
- Add-in ops (all in mm):
  - `ops/docs.py`: `import_step`, `close_scratch`, `screenshot`
  - `ops/geometry.py`: `inspect`
  - `ops/geomcalc.py` + `ops/units.py`: the maths, with no adsk import
  - `ops/guards.py`: `require_idle`, scratch attribute helpers
- Scratch marking is a design attribute `FusionMCP/scratch` = `{"id", "source", "created"}`. It lives in the document, so it survives `reload_api` and add-in restarts. `close_scratch` also requires the document to have no data file and never to have been saved.
- MCP tools: `import_step`, `inspect`, `screenshot`, `close_scratch` (by `name` or `scratch_id`).

## Inventor side
`MCP_Test_Plate.ipt`: Inventor `inspect()` gives 80×50×10 mm (x −40…40, y −25…25, z 0…10) and 37395 mm³, with 13 faces.
Exported to `test_output\MCP_Test_Plate.step` (20 104 bytes) with the STEP translator looked up by ClassId. The Inventor MCP's `export_document` still fails with "Translator not found" on 2027.

## Verified (live)
| Check | Result |
|---|---|
| `import_step(..., expected_size_mm=[80,50,10], expected_volume_mm3=37395)` | **Pass.** 1 body, 3.6 s. Imported as a parametric design. bbox min (−40, −25, 0), max (40, 25, 10), size **80.000×50.000×10.000 mm** (0.000 % on every axis, axes match). Volume **37395.009 mm³** (+0.00002 %). `check.ok: true` |
| `inspect` | Top face Z **10.0** (one up face, 3739.5 mm²; one down face at Z 0, same area). Holes: **1× Ø16.000 through**, depth 10, at (0, 0); **2× Ø4.917 through** (M6 tap drill), depth 10, at (±28, 0). Axis +Z, entry at Z 10. 4 convex cylinder faces (the R5 corners), correctly not counted as holes |
| `screenshot(view="iso")` | 1600×1000 PNG, 148 kB, in `test_output\`. It shows the plate with the three holes and the R5 corners |
| `close_scratch(scratch_id=...)` | Closed only the imported document. The user's own unsaved "Untitled" stayed open |
| `close_scratch(name="Untitled")` against the user's document | **Refused:** "not created by import_step (no FusionMCP scratch attribute)" |
| `close_scratch(scratch_id="nope")` | Error: no such scratch document |
| `import_step` / `close_scratch` with the Extrude dialog open | **Refused:** "Fusion is not idle (active command: Extrude)". `inspect` and `screenshot` still run |
| Unit tests (`pytest`) | 29/29 |
| Live tests (`pytest -m fusion`) | 11/11: the 6 Phase 1 tests plus refusal, import check, inspect holes, screenshot, close by id |

## Findings
- **Imported documents are named "Untitled"**, the same name as a fresh user document. Closing by name could therefore pick the wrong one, which is why `scratch_id` was added. The add-in still never closes a document without the scratch attribute.
- Inventor's tapped M6 holes come through STEP as plain Ø4.917 cylinders (the tap-drill/minor diameter). Thread data is not in the STEP, so Phase 3 has to be told which holes are tapped.
- `MeasureManager.getOrientedBoundingBox` with world X/Y gives the exact box. `BRepBody.boundingBox` is kept only as a fallback.

## Next: Phase 3 (CAM)
`cam_status`, `cam_create_setup`, `cam_list_strategies`, `cam_list_tools`, `cam_add_operation`, `cam_generate`, `cam_machining_time`, `cam_post`.
