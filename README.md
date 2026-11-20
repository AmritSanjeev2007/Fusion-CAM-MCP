# Fusion-CAM-MCP
A Python MCP server that lets AI clients import STEP files from Inventor, machine them with Fusion's native CAM tools, and check the resulting G-code outside Fusion.

```
Inventor (inventor MCP) --STEP--> Fusion (FusionMCP add-in) <--TCP 127.0.0.1:27190-- fusion-cam MCP server <--stdio-- Claude
                                         setups, toolpaths, posting                  gcode_check (outside Fusion)
```

Verified on Fusion 2706.1.17 (Personal Use licence) and Inventor 2027.1, 2026-11-20. Phase records with real outputs are in `docs/`: [phase0](docs/phase0-findings.md), [phase1](docs/phase1-results.md), [phase2](docs/phase2-results.md), [phase3](docs/phase3-results.md) and [phase4](docs/phase4-results.md). The design is in [FUSION_MCP_PLAN.md](docs/FUSION_MCP_PLAN.md).

## Layout
- `addin/FusionMCP`: the Fusion add-in. It runs a loopback socket bridge (127.0.0.1:27190, token in `%APPDATA%\FusionMCP\token`) and the ops that run on Fusion's main thread (`ops/*.py`, registered in `ops/__init__.py`). The maths with no Fusion import lives in `ops/geomcalc.py`, `ops/camcalc.py` and `ops/units.py`.
- `src/`: the `fusion-cam` MCP server. Its tools are thin wrappers around `BridgeClient.call(op, args)`. Generation polling is in `src/polling.py`.
- `external/gcode_check.py`: the G-code checker (standard library only; usable as a library, a CLI or the `gcode_check` tool).
- `skills/`: `fusion-cam-usage` (operating manual) and `fusion-cam-recipes` (machining choices), packaged as a plugin by `.claude-plugin/`.
- `tests/`: unit tests (`pytest`) and live tests (`pytest -m fusion`). `tests/data/plate` holds the Fanuc programs posted for the test plate.
- `tools/install_addin.ps1`: links the add-in into Fusion.

## Setup

### 1. Server environment
```powershell
cd "C:\Fusion MCP"
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```
This creates `.venv\Scripts\fusion-cam-mcp.exe`. `mcp` is pinned below 2, because mcp 2.x removed `mcp.server.fastmcp`.

### 2. Fusion add-in
```powershell
powershell -ExecutionPolicy Bypass -File tools\install_addin.ps1
```
This creates a junction from `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\FusionMCP` to the repo, so edits are live. Then in Fusion: **Shift+S → Add-Ins → FusionMCP → Run**, and tick **Run on Startup**. Text Commands shows `FusionMCP listening on 127.0.0.1:27190`. After editing `ops/*.py`, call `reload_api`. Changes to `FusionMCP.py`, `bridge/*`, `ops/state.py` or `ops/breadcrumb.py` need the add-in stopped and run again.

### 3. Register the server in Claude
Per project: copy `.mcp.json.example` to `.mcp.json` (it's gitignored) and approve the server when Claude Code asks. Or for all projects:
```powershell
claude mcp add --scope user fusion-cam -- "C:\Fusion MCP\.venv\Scripts\fusion-cam-mcp.exe"
```
For Claude Desktop (chat), put the same block under `mcpServers` in `%APPDATA%\Claude\claude_desktop_config.json`. Start a new session, then call `status`.

### 4. Skills (plugin)
```text
/plugin marketplace add "C:\Fusion MCP"
/plugin install fusion-cam@fusion-cam-mcp
```
For a quick local test, copy `skills\fusion-cam-usage` and `skills\fusion-cam-recipes` into `%USERPROFILE%\.claude\skills\` instead.

## Tools
All tool inputs and outputs are in **mm** (mm², mm³), seconds and mm/min. The raw Fusion API inside `execute_python` uses cm and radians.

| Tool | What it does |
|---|---|
| `status` | Connection, open documents, active command/idle, last crash |
| `execute_python` | Python on Fusion's main thread (persistent namespace) |
| `reload_api` | Re-import `ops/*.py` after editing them |
| `import_step(path, expected_size_mm, expected_volume_mm3, tolerance_pct=0.1)` | Imports a STEP into a **new scratch design**. Returns bodies, bbox and volume, plus an optional axis-agnostic check |
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
| `gcode_check(paths, stock_wcs_mm=None, setup, limits_wcs_mm, expected_tools)` | Runs **outside Fusion**: height-map stock simulation plus rule checks by line. The stock and tool list come from the setup if not given |

Tools that modify documents (`import_step`, `close_scratch` and every `cam_*` op that creates or posts) refuse while a Fusion command dialog is open. The CAM tools only modify scratch documents from `import_step` unless you pass `allow_user_document=true`. A call that times out may still be running in Fusion, so call `status`/`inspect`/`cam_status` before retrying.

Each call writes fsync'd breadcrumbs to `%APPDATA%\FusionMCP\breadcrumb.log`: `BEGIN`/`END` per request, plus a `STEP` line before each risky adsk.cam call. If Fusion crashes, `status` reports `last_crash` with the op and the step. Do not repeat that call unchanged.

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
3. Run `inspect()` to choose strategies (top face, floors, holes), then `screenshot()` for a visual check.
4. Run the CAM job (below), always followed by `gcode_check`, then `close_scratch(scratch_id=...)`.

## Worked job: the test plate (80×50×10 mm, R5 corners, Ø16 and 2× M6)
```text
import_step("C:\\Fusion MCP\\test_output\\MCP_Test_Plate.step", expected_size_mm=[80, 50, 10], expected_volume_mm3=37395)
                        # 80.000×50.000×10.000 mm, 37395.009 mm³, check ok
inspect()               # top face Z10; holes 1× Ø16 and 2× Ø4.917, all through
cam_create_setup(stock_offset_mm=1, wcs="top-centre")                                 # stock 82×52×11
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
gcode_check(paths=[...the 4 files...])   # ok; removed 9524 mm³, remaining 37380 vs CAD 37395 mm³
close_scratch(scratch_id=...)
```
G-code check CLI: `python external/gcode_check.py --stock -41,-26,-11,41,26,0 --tools 1,2,3,4 file1.nc file2.nc`. The sample tools carry generic feeds and speeds; set `tool_spindleSpeed`/`tool_feedCutting` before machining anything real.

## Known limits
- **Fusion Personal Use**: no tool changes in one NC program (so `cam_post` splits per tool), and rapids are posted at the cutting feed. An Education licence removes both.
- `gcode_check` is 3-axis and models the cutter profile only (no holder or fixture). CAMotics is detected if installed but not run (not installed on the development PC).
- Geometry selection is wrapped for holes and the outline contour. Pocket and face selections for other strategies go through `execute_python`.
- The official Autodesk Fusion MCP (port 27182) is not set up or verified here; see the usage skill, section 9.

## Tests
```powershell
.venv\Scripts\python.exe -m pytest                 # unit tests (no Fusion)
.venv\Scripts\python.exe -m pytest -m fusion -v    # live: needs Fusion + add-in and test_output\MCP_Test_Plate.step
```
