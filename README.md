# Fusion-CAM-MCP
An Python MCP server that allows AI Clients to import STEP files from Inventor, use them with Fusion's native CAM tools and also a G-code toolpath check outside Fusion.

## Layout
- `addin/FusionMCP`: Fusion add-in. It runs a loopback socket bridge (127.0.0.1:27190, token in `%APPDATA%\FusionMCP\token`) and the ops that run on Fusion's main thread (`ops/*.py`, registered in `ops/__init__.py`).
- `src/`: the `fusion-cam` MCP server. Its tools are thin wrappers around `BridgeClient.call(op, args)`.
- `tests/`: unit tests (`pytest`) and live tests (`pytest -m fusion`, which need Fusion running with the add-in).

## Tools
All tool inputs and outputs are in **mm** (mm², mm³), seconds and mm/min. The raw Fusion API inside `execute_python` uses cm.

| Tool | What it does |
|---|---|
| `status` | Connection, open documents, active command/idle, last crash |
| `execute_python` | Python on Fusion's main thread (persistent namespace) |
| `reload_api` | Re-import `ops/*.py` after editing them |
| `import_step(path, expected_size_mm, expected_volume_mm3, tolerance_pct=0.1)` | Imports a STEP into a **new scratch design**. Returns bodies, bbox and volume, plus an optional check |
| `inspect()` | Bodies, bbox, volume, top-face Z, horizontal planar faces by Z, holes (diameter, depth, through/blind, count) |
| `screenshot(path, width, height, view)` | Viewport PNG (`iso`/`top`/`front`/`right`/`current`), saved to `test_output/` by default |
| `close_scratch(name=None, scratch_id=None)` | Closes, without saving, only documents that `import_step` created and that were never saved. Prefer `scratch_id`, because imported documents are named "Untitled" like a new user document |
| `cam_status(check=True)` | Setups (WCS, stock), operations (tool, toolpath valid, `checkToolpath`), NC programs, `last_crash` |
| `cam_create_setup(stock_offset_mm=1, wcs="top-centre", body)` | Milling setup with relative-box stock. Returns the stock box and WCS origin (world and WCS mm) |
| `cam_list_strategies(setup)` | Live strategy names (55 milling strategies on 2706.1.17) |
| `cam_list_tools(source="samples", library, type, diameter_min_mm, diameter_max_mm, text)` | Tools from Fusion's bundled sample JSON, a library `.json`, or `cloud` (opt-in) |
| `cam_add_operation(strategy, tool_id, geometry, params, tool_number)` | Operation with a JSON tool. `geometry` takes `{"holes": {"diameter_mm": d}}` or `{"contour": "outline", "side": "outside"}`. `params` are Fusion parameters by name; lengths need units, e.g. `"1 mm"` |
| `cam_generate(targets, timeout_s=600, job)` | Starts generation and polls with short calls. Returns validity and `checkToolpath` per operation |
| `cam_machining_time(target, rapid_mm_min=5000, tool_change_s=15)` | Cycle time (s), feed/rapid distance (mm), tool changes, per operation |
| `cam_post(post="fanuc.cps", output_dir, filename, split="auto")` | G-code via an NC program. On Personal Use it posts one program per tool. Returns the `.nc` paths, line counts and file heads |

Tools that modify documents (`import_step`, `close_scratch` and every `cam_*` op that creates or posts) refuse while a Fusion command dialog is open. The CAM tools only modify scratch documents from `import_step` unless you pass `allow_user_document=true`. A call that times out may still be running in Fusion, so call `status`/`inspect`/`cam_status` before retrying.

Each CAM call writes fsync'd breadcrumbs: `BEGIN`/`END` per request, plus a `STEP` line before each risky adsk.cam call, in `%APPDATA%\FusionMCP\breadcrumb.log`. If Fusion crashes, `status` reports `last_crash` with the op and the step. Do not repeat that call unchanged.

## Inventor → STEP → Fusion hand-off
1. **Inventor** (inventor MCP): open or build the part and check it with `inspect()`. Export STEP with the translator found by ClassId. On Inventor 2027, `export_document` fails with "Translator not found", so use `execute_python`:
   ```python
   tr = app.ApplicationAddIns.ItemById("{90AF7F40-0C01-11D5-8E83-0010B541CD80}")   # STEP
   if not tr.Activated: tr.Activate()
   ctx = app.TransientObjects.CreateTranslationContext(); ctx.Type = 13059
   dm = app.TransientObjects.CreateDataMedium(); dm.FileName = r"C:\Fusion MCP\test_output\part.step"
   tr.SaveCopyAs(app.ActiveDocument, ctx, app.TransientObjects.CreateNameValueMap(), dm)
   ```
2. **Fusion** (fusion-cam MCP): `import_step(path, expected_size_mm=[...], expected_volume_mm3=...)` with Inventor's bbox and volume. The size check is axis-agnostic and warns if the axes differ (Y-up vs Z-up).
3. Run `inspect()` to choose machining strategies (top face, floors, holes), then `screenshot()` for a visual check.
4. Do the CAM work (below), then `close_scratch(scratch_id=...)`.

## Phase 2 verification
1. In Inventor, export the test plate (80×50×10 mm, R5 corners, Ø16 through hole, 2× M6) to `test_output\MCP_Test_Plate.step`.
2. Run `import_step("C:\\Fusion MCP\\test_output\\MCP_Test_Plate.step", expected_size_mm=[80, 50, 10], expected_volume_mm3=37395)`. Expect `check.ok: true`, i.e. within 0.1 %.
3. Run `inspect()`. Expect top face Z = 10, one Ø16 through hole and two identical through holes at the M6 tap-drill diameter.
4. Run `screenshot(view="iso")`, then `close_scratch(scratch_id=...)`.

Results from 2026-11-20 are in [docs/phase2-results.md](docs/phase2-results.md): 80.000×50.000×10.000 mm, 37395.009 mm³, holes 1× Ø16 and 2× Ø4.917, all through.

## Phase 3: CAM job on the test plate
```text
cam_create_setup(stock_offset_mm=1, wcs="top-centre")
cam_add_operation("face", "49d0ef72-1d9e-4d80-aa4c-2fd9384de358")                     # T1 Ø50 face mill (Tutorial Tools)
cam_add_operation("contour2d", "593e3a17-2722-4974-b6d6-46521c351aa6",                 # T2 Ø10 flat end mill
                  geometry={"contour": "outline", "side": "outside"},
                  params={"bottomHeight_offset": "-0.5 mm", "doMultipleDepths": true, "maximumStepdown": "4 mm"})
cam_add_operation("drill", "4e82fd33-fab1-4910-b0b1-768c41323e30",                     # T3 Ø16 drill
                  geometry={"holes": {"diameter_mm": 16}}, params={"drillTipThroughBottom": true, "breakThroughDepth": "1 mm"})
cam_add_operation("drill", "e4b5958e-d3b7-4249-a084-d9cca4830757",                     # T4 Ø4.9 drill, peck
                  geometry={"holes": {"diameter_mm": 4.917}},
                  params={"drillTipThroughBottom": true, "breakThroughDepth": "1 mm", "cycleType": "'deep-drilling'", "incrementalDepth": "3 mm"})
cam_generate()          # all valid, checkToolpath true, 2.6 s
cam_machining_time()    # 151.2 s, 3 tool changes
cam_post("fanuc.cps")   # Personal Use: 4 programs, one per tool, in test_output\nc
```
Results from 2026-11-20 and the API facts found along the way are in [docs/phase3-results.md](docs/phase3-results.md). The sample tools carry generic feeds and speeds; set `tool_spindleSpeed`/`tool_feedCutting` before machining anything real.

Tests:
```powershell
.venv\Scripts\python.exe -m pytest                 # unit tests (no Fusion)
.venv\Scripts\python.exe -m pytest -m fusion -v    # live, needs Fusion + add-in; uses test_output\MCP_Test_Plate.step if present
```
