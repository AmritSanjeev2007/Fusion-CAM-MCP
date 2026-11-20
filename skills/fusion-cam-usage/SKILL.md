---
name: fusion-cam-usage
description: How to run CAM in Autodesk Fusion through the custom fusion-cam MCP server (FusionMCP add-in), on STEP files exported from Inventor. Use whenever you are about to call any mcp__fusion-cam__* tool, import a STEP for machining, create setups or operations, generate toolpaths, estimate cycle time, post G-code, check G-code with gcode_check, start or debug the FusionMCP add-in, or when a call times out, returns a traceback, refuses to run, or Fusion crashes. Covers startup checks, the Inventor -> STEP -> Fusion hand-off, the full tool reference with real outputs, a worked job, main-thread and timeout behaviour, the crash rules, Personal Use limits and troubleshooting. For strategy, stock/WCS and feeds/speeds choices see the sibling skill fusion-cam-recipes.
---

# Using the fusion-cam MCP server

This is the operating manual: how to get the server and add-in running, which tool to use,
how to check each step worked, and what goes wrong. Machining choices (which strategy, stock,
WCS, feeds) are in `fusion-cam-recipes`.

Everything here was verified on **Fusion 2706.1.17** (Python 3.14.7 inside Fusion, Personal
Use licence), with Inventor 2027.1 as the CAD side, on 2026-11-20, unless marked otherwise.
The repo is `C:\Fusion MCP`. Phase records with the raw outputs are in `docs/phase*-results.md`.

## 1. Before the first tool call

1. **Use the registered MCP tools.** They appear as `mcp__fusion-cam__*` and may be
   deferred, so load them with ToolSearch (e.g. `select:mcp__fusion-cam__status,...`).
   Registration (`.mcp.json` in the repo, from `.mcp.json.example`):
   ```json
   {"mcpServers": {"fusion-cam": {"command": "C:/Fusion MCP/.venv/Scripts/fusion-cam-mcp.exe"}}}
   ```
   New or changed *tools* in `src/server.py` only appear in a **new session**: the server
   process is started once per session. Changes to `addin/FusionMCP/ops/*.py` need only
   `reload_api`.
2. **Call `status` first.** It answers in about 40 ms when everything is up:
   ```
   {"fusion_version": "2706.1.17", "python": "3.14.7", "active_document": "Untitled",
    "active_workspace": "FusionSolidEnvironment", "active_command": "SelectCommand", "idle": true,
    "documents": [{"name": "Untitled", "saved": false, "modified": false, "scratch": false, ...}],
    "last_crash": null, "addin": {"addin_version": ..., "port": 27190, ...}}
   ```
   - `ERROR (not_running) ... Cannot reach the FusionMCP add-in`: Fusion is not running or
     the add-in is stopped. In Fusion: Shift+S > Add-Ins > FusionMCP > Run.
   - "Add-in reachable but Fusion's main thread did not answer": a modal dialog or a long
     operation is blocking Fusion. Ask the user to check Fusion.
   - **`last_crash` set: Fusion died during that call last time.** Read section 6 before
     doing anything.
3. **Look at the user's documents and leave them alone.** `documents[].scratch` is true only
   for documents `import_step` created. The tools refuse to modify anything else, and you
   must not work around that with `execute_python`.
4. **`idle` must be true** for anything that modifies a document. If `active_command` is
   e.g. `"Extrude"`, the user has a dialog open: `import_step`, `close_scratch` and the CAM
   tools refuse with `Fusion is not idle (active command: Extrude)`. Ask the user to finish
   or cancel it. Never terminate their command yourself unless they say so.

## 2. How it works

```
Claude ─stdio─> fusion-cam-mcp.exe (src/server.py) ─TCP 127.0.0.1:27190 + token─> FusionMCP add-in
          socket thread ─> custom event ─> Fusion main thread ─> ops/*.py ─> Fusion API
```
- The token is in `%APPDATA%\FusionMCP\token`. Logs are `addin.log` and `breadcrumb.log` in the
  same folder, plus Fusion's Text Commands window.
- All work runs on Fusion's **single main thread**, one request at a time, with about 10 ms
  overhead when Fusion is idle.
- **Units:** every tool takes and returns **mm** (mm², mm³), seconds and mm/min. Inside
  `execute_python` you are on the raw API, which is **cm and radians**; `mm()` and `deg()` are
  bound for you.
- **Timeouts:** each call has a timeout (default 60 s).
  - Timed out *before* the main thread picked it up: it was cancelled and **did not run**.
  - Timed out *while running*: it **may still finish**. Call `status`/`inspect`/`cam_status`
    before retrying. A retried `import_step` makes a second document.
- `cam_generate` never blocks the main thread. The add-in starts generation and the server
  polls every 0.5 s, so Fusion stays responsive.

## 3. Inventor → STEP → Fusion hand-off

1. Inventor (inventor MCP): open or build the part and read its `inspect()`. The test plate
   gives `bbox x[-40,40] y[-25,25] z[0,10]`, `volume_mm3: 37395`.
2. Export STEP. On Inventor 2027 `export_document` fails with
   `Translator not found: Autodesk Inventor STEP Translator`, so use the ClassId:
   ```python
   # mcp__inventor__execute_python
   tr = app.ApplicationAddIns.ItemById("{90AF7F40-0C01-11D5-8E83-0010B541CD80}")   # STEP
   if not tr.Activated: tr.Activate()
   ctx = app.TransientObjects.CreateTranslationContext(); ctx.Type = 13059
   dm = app.TransientObjects.CreateDataMedium(); dm.FileName = r"C:\Fusion MCP\test_output\part.step"
   tr.SaveCopyAs(app.ActiveDocument, ctx, app.TransientObjects.CreateNameValueMap(), dm)
   ```
3. Fusion: `import_step(path, expected_size_mm=[80, 50, 10], expected_volume_mm3=37395)`,
   using the numbers from Inventor's `inspect()`.

## 4. Tool reference (real outputs, abridged)

### Connection and documents
| Tool | Notes |
|---|---|
| `status()` | See section 1. |
| `execute_python(code, timeout_s=60)` | Escape hatch on the main thread. Pre-bound: `app, ui, adsk, math, doc, design, root, cam, mm(), deg()`. The namespace persists between calls. `print()` the output; a trailing expression is echoed. Errors show your line: `line 2: b = a / 0` / `ZeroDivisionError`. It warns (but still runs) when Fusion is not idle. |
| `reload_api()` | Re-imports `ops/*.py` (about 11 ms) and clears the namespace. `FusionMCP.py`, `bridge/*`, `ops/state.py` and `ops/breadcrumb.py` need the add-in stopped and run again. |
| `import_step(path, expected_size_mm, expected_volume_mm3, tolerance_pct=0.1)` | New document, marked scratch. Real output: `"document": "Untitled", "scratch_id": "aeb13ca990f1", "import_s": 3.61, "design_type": "parametric", "modeling_orientation": "Z-up", "body_count": 1, bbox size [80.0, 50.0, 10.0], "volume_mm3": 37395.009, "check": {"ok": true, ...}`. The size check is axis-agnostic and adds a warning when axes differ (Y-up export). **Keep the `scratch_id`.** |
| `inspect()` | Read-only. For the plate: `top_face_z_mm: 10.0`, horizontal faces `[{z 10, up, 3739.5 mm²}, {z 0, down, 3739.5}]`, holes `[{diameter 16.0, depth 10.0, through, count 1, centres [[0,0,5]]}, {diameter 4.917, depth 10.0, through, count 2, centres [[28,0,5],[-28,0,5]]}]`, `convex_cylinder_faces: 4` (the R5 corners, not holes). Hole depth is the cylindrical wall only. |
| `screenshot(path, width=1600, height=1000, view="iso")` | `iso`/`top`/`front`/`right`/`current`. Default path `test_output/screenshot_<time>_<view>.png`. Read the PNG to look at it. |
| `close_scratch(name=None, scratch_id=None)` | Closes without saving, scratch and never-saved documents only. **Use `scratch_id`**: imported documents are named "Untitled", the same as a fresh user document. Closing the user's document by name gives `Refusing to close 'Untitled': not created by import_step`. |

### CAM (the scratch document must be active; it is right after import_step)
| Tool | Notes |
|---|---|
| `cam_create_setup(stock_offset_mm=1, wcs="top-centre", body=None)` | Activates the Manufacture workspace; the CAM product doesn't exist until then. Real output: `stock_box_world_mm size [82, 52, 11]`, `stock_in_wcs_mm min [-41,-26,-11] max [41,26,0]`, `wcs_origin_world_mm [0, 0, 11]`. `stock_offset_mm` takes a number or `{"sides", "top", "bottom"}`. |
| `cam_list_strategies()` | 55 names: `adaptive, adaptive2d, bore, chamfer2d, contour2d, pocket2d, drill, face, parallel, scallop, ...` |
| `cam_list_tools(source="samples", library, type, diameter_min_mm, diameter_max_mm, text)` | With no filters it lists the sample libraries. Example: `library="Hole Making Tools (Metric)", type="drill", 4.8..5.0` returns 3 drills (4.8, 4.9, 5.0). `source` can also be a library `.json` path, or `"cloud"` (opt-in, see section 6). |
| `cam_add_operation(strategy, tool_id, geometry, params, tool_number)` | Tool numbers are auto-assigned (1, 2, 3, …; the same tool reuses its number). H and D offsets follow the number. Returns `parameter_errors` for names that didn't apply, so read it. |
| `cam_generate(targets=None, timeout_s=600, job=None)` | Plate job: `completed: true, elapsed_s: 2.6, all_valid: true`, with each op `toolpath_valid` and `check_ok`. On timeout, call it again with `job=<id>`. Don't start a new generation. |
| `cam_machining_time(target, rapid_mm_min=5000, tool_change_s=15)` | Plate: `total_s 151.2 ("2:31.2")`, feed 1294 mm, rapid 702.9 mm, 3 tool changes, with a per-op breakdown. |
| `cam_post(post="fanuc.cps", output_dir, filename, split="auto")` | Returns files with `lines` and `head`. Personal Use auto-splits into one program per tool (section 7). The default folder is `test_output/nc`. |
| `cam_status(check=True)` | Setups, ops, tools, validity, NC programs, `last_crash`. |
| `gcode_check(paths, stock_wcs_mm=None, setup=None, limits_wcs_mm, expected_tools, camotics=True)` | **Always run it on posted files.** It runs outside Fusion. If no stock is given, the stock box and tool list come from the setup. Plate: `ok: true`, 3 info `BELOW_STOCK` (through cuts), removed 9524 mm³, remaining 37380 against CAD 37395. With CAMotics 1.2.0 installed it also runs camsim (about 10 s): `simulation_comparison` gives built-in 9524 against CAMotics 9767 mm³ (+2.55 %), and more than 5 % apart gives `SIM_DISAGREE`. |

## 5. Worked job: the Inventor test plate

```text
import_step("C:\\Fusion MCP\\test_output\\MCP_Test_Plate.step", expected_size_mm=[80,50,10], expected_volume_mm3=37395)
inspect()                                     # top face Z10; Ø16 ×1 and Ø4.917 ×2, through
cam_create_setup(stock_offset_mm=1, wcs="top-centre")
cam_add_operation("face", "49d0ef72-1d9e-4d80-aa4c-2fd9384de358")              # T1 Ø50 face mill (Tutorial)
cam_add_operation("contour2d", "593e3a17-2722-4974-b6d6-46521c351aa6",          # T2 Ø10 flat (Milling)
    geometry={"contour": "outline", "side": "outside"},
    params={"bottomHeight_offset": "-0.5 mm", "doMultipleDepths": true, "maximumStepdown": "4 mm"})
cam_add_operation("drill", "4e82fd33-fab1-4910-b0b1-768c41323e30",              # T3 Ø16 drill
    geometry={"holes": {"diameter_mm": 16}}, params={"drillTipThroughBottom": true, "breakThroughDepth": "1 mm"})
cam_add_operation("drill", "e4b5958e-d3b7-4249-a084-d9cca4830757",              # T4 Ø4.9 drill
    geometry={"holes": {"diameter_mm": 4.917}},
    params={"drillTipThroughBottom": true, "breakThroughDepth": "1 mm", "cycleType": "'deep-drilling'", "incrementalDepth": "3 mm"})
cam_generate()                                # 4/4 valid, check_ok, 2.6 s
cam_machining_time()                          # 151.2 s
cam_post("fanuc.cps", filename="plate")       # plate_01_T1.nc (40 lines) ... plate_04_T4.nc (31)
gcode_check(paths=[...4 files...])            # ok: true
close_scratch(scratch_id=...)
```
Posted T4 head: `(T4 D=4.9 CR=0. TAPER=118DEG - ZMIN=-13.472 - DRILL)`, `N30 T4 M06`,
`N60 G43 Z15. H04`, `N70 G98 G83 X28. Y0. Z-13.472 R4. Q1.225 F1000.`

## 6. Crash rules (read this if `last_crash` is set)

Phase 0 run 1 killed Fusion inside an adsk.cam call (cloud tool-library query plus an
operation using that tool). Nothing raises in Python when this happens; the process just
dies. So:
- Every request writes an fsync'd `BEGIN <op> <id>` before it runs and `END <op> <id> ok|fail`
  after. CAM ops also write `STEP <op> <id> <call>` before each risky call (generateToolpath,
  postProcess, createFromJson, …) in `%APPDATA%\FusionMCP\breadcrumb.log`.
- On the next add-in start, a BEGIN without an END becomes `status.last_crash`
  `{op, summary, when, step}`.
- **Never repeat that call unchanged.** Tell the user what crashed (op and step), change
  the input (another tool, another strategy, fewer targets), or ask.
- The cloud tool library is **opt-in**: `cam_list_tools(source="cloud")` takes about 8 s and was
  part of the crash. Use the bundled samples or the user's exported `.json`.
- The tools will not edit the user's documents, and you must not edit them through
  `execute_python` either. Work on the scratch copy.

## 7. Things that are different on this Fusion (all verified)

| Symptom | Cause | What the tools do / what you do |
|---|---|---|
| `postProcess` returns False; NC error "When using Fusion for Personal Use, you cannot post process operations that contain tool changes in a single NC program" | **Personal Use licence** | `cam_post(split="auto")` posts one program per consecutive tool group (`<name>_01_T1.nc`, …). Tell the user to run them in order. |
| Posted G-code says rapids are reduced to the cutting feed | Personal Use | Real cycle time is longer than `cam_machining_time` suggests. An Education licence removes this. |
| 2D contour cut on the **inside** of the outline (tool centre at ±35, ±20 on the 80×50 plate) | The default chain side on a down-facing face's outer loop | `geometry={"contour": "outline", "side": "outside"}` reverses the chain: ±45, ±30 with R10 corner arcs, climb cut. |
| WCS corner in the wrong place | Fusion's numbering is 1 = −X−Y, 2 = +X−Y, **3 = −X+Y, 4 = +X+Y** | Use `top-corner-N` with that mapping. |
| `G43 … H00` in the program | Sample tools have length offset 0 | Fixed: H and D follow the tool number. |
| `RuntimeError: 3 : failed to find product` for CAM | No CAM product until the Manufacture workspace is opened | `cam_create_setup` activates it. |
| `cam.checkAllToolpaths()` raises "The operations are not CAM objects"; `checkToolpath([ops])` raises TypeError | API quirks on 2706.1.17 | The tools call `checkToolpath(op)` once per operation. |
| `op.isGenerating` stays True after generation | API quirk | Ignored; only `GenerateToolpathFuture.isGenerationCompleted` is used. |
| NC file missing from `output_dir` | Phase 0 saw the folder ignored (set as `.value.value`) | Setting it as a quoted expression works; `cam_post` still looks in Fusion's default `NC Programs` folder and moves the file. |
| CAMotics: `Not implemented: G83 (Peck Drilling)`, `Tool Length Offset not supported`; `camsim --version` prints 0.0 | camsim 1.2.0 limits | `gcode_check` feeds camsim a copy with G83/G73 → G81 (same lines), ignores the G43 warnings, and reads the version from CAMotics' CHANGELOG. CAMotics reports no collisions: `RAPID_INTO_STOCK` comes from the built-in check. Its other warnings (e.g. `Arc radiuses differ`) come back with file and line. |
| Imported M6 holes are Ø4.917 cylinders | STEP carries no thread data | Tapping needs an explicit op with a tap tool (recipes). |

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `Refusing to run cam_… : not a scratch document` | Import the part with `import_step` first. Pass `allow_user_document=true` only if the user explicitly asked to work on their document. |
| `no setups. Call cam_create_setup first.` / `has no CAM data yet` | Create the setup on the scratch document. |
| `unknown strategy` | Use a name from `cam_list_strategies` (e.g. `contour2d`, not `2D contour`). |
| `unknown tool_id` | Ids are library guids from `cam_list_tools`. |
| `parameter_errors: {name: "no such parameter"}` | That strategy has no such parameter. Lengths need units (`"1 mm"`); choice values need quotes inside the string (`"'deep-drilling'"`). |
| `operations without a valid toolpath` on post | Run `cam_generate` first; check the op's `error` in `cam_status`. |
| Timed out "while running" | It may still finish. Use `status`, then `cam_status`, then `cam_generate(job=…)`. |
| New tool or argument missing in the MCP tool list | The server started before the change; it needs a new session. |
| `reload_api` didn't pick up a change to `state.py`, `breadcrumb.py`, `bridge/*` or `FusionMCP.py` | Stop and run the add-in in Fusion (Shift+S > Add-Ins). |

## 9. When to use the official Autodesk Fusion MCP instead

Autodesk's own server (Preferences > General > API > "Fusion MCP Server", port 27182) is
good for API documentation search, opening or saving cloud documents and undo/redo. It is
not set up on this PC and **not verified**; its tool list changes between releases. Use this
server for STEP import, inspection and the whole CAM pipeline. Don't send writes through both
at once: they share Fusion's main thread. After an official-MCP script, call `inspect`/`cam_status`
here before continuing.
