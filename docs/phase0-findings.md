# Phase 0 findings

Fusion **2706.1.17** (`%LOCALAPPDATA%\Autodesk\webdeploy\production\39e8a3d5…\Fusion360.exe`).
Part A was checked statically on 2026-11-20 against the installed build and its API stubs
(`API\Python\packages\adsk\defs\adsk\*.py`). Part B is filled in from `phase0-results.json`
after running `spikes\FusionMCP_Phase0` inside Fusion.

## A. Static checks (done)

| Plan item | Finding | Plan change |
|---|---|---|
| Fusion's Python | **3.14.7** (bundled `Python\python.exe`, `python314.dll`) | Add-in code must be 3.14-safe. The server venv is separate and can stay 3.12+. |
| Manifest `autodeskProduct` | `"Fusion"` (from Autodesk's bundled ElectronicsPackageGenerator manifest) | [verify] resolved: use `"Fusion"` |
| AddIns / Scripts paths | `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns` and `...\API\Scripts` both exist (empty before this spike) | [verify] resolved |
| Custom events | `Application.registerCustomEvent`, `fireCustomEvent`, `unregisterCustomEvent`, `CustomEventHandler` all present | Design unchanged (runtime check: B.2) |
| Script lifetime | `adsk.autoTerminate(False)` / `adsk.terminate()` / `adsk.doEvents()` present | Used by the spike; `doEvents()` lets CAM polling keep the interface responsive |
| Screenshot | `Viewport.saveAsImageFile(filename, w, h)` and `saveAsImageFileWithOptions` | Design unchanged |
| CAM setup / operation | `Setups.createInput(OperationTypes)`, `Operations.createInput(strategy: str)`, `Operations.compatibleStrategies` → `OperationStrategy` | Strategy names are read live (B.5) |
| CAM generate | `CAM.generateToolpath` / `generateAllToolpaths` → `GenerateToolpathFuture.isGenerationCompleted` | Design unchanged (poll) |
| CAM checks | `CAM.checkToolpath(ops)` / `checkAllToolpaths()` return a bool (toolpath valid or not). **Not a collision or stock simulation.** | Expose as part of `cam_status` |
| Machining time | `CAM.getMachiningTime(ops, feedScale %, rapidFeed cm/s, toolChange s)` → `machiningTime`, `feedDistance`, `rapidDistance`, `toolChangeCount`, … | Design unchanged |
| Post-processing | **There is no `PostProcessInput`.** Posting is `NCPrograms.createInput()` → set `operations` and `parameters` → `ncPrograms.add` → `nc.postConfiguration = …` → `nc.postProcess(NCProgramPostProcessOptions.create())`. Posts come from `PostLibrary.createQuery(location).execute()`. | `cam_post` must be written this way |
| Tool lookup | `ToolLibraries.createQuery(location)` with `criteria`; or `urlByLocation` → `childAssetURLs` → `toolLibraryAtURL` (iterable) | Design unchanged |
| Threads | `ThreadFeatures` and `HoleFeatures` present; holes have `isThreaded` | Design unchanged (runtime check deferred to Phase 2) |
| MachiningPlan | Present in this build's stubs | Note only |

## Run 1 (2026-11-20 09:45): Fusion crashed

- Checks 1, 4 and 6 completed: the 10 mm cube was built and `docs/phase0-screenshot.png` was written.
- The CAM design was created and the Manufacture workspace opened (log: `CAM Create Iron Document` at 09:45:26).
- Fusion's main thread then stayed busy for about 25 s while it downloaded hundreds of cloud library files (`cam.autodesk.com/printsettings/...`). That was the hang.
- At 09:45:51 it crashed with an unhandled exception **on the main thread, inside an `adsk.cam` call made directly from the script's `run()`**. The stack goes `python314` → `_cam` → `CAMXLayer10`, and the nearest exported symbols are in the tool-library/vendor-integration code.
- `test_output/phase0_nc` was never created, so the crash came before the post step. The likely cause is the cloud tool-library query (`ToolLibraries.createQuery(Fusion360LibraryLocation).execute()`), but run 1 had no per-step trace to prove it.
- Source: CER report `%LOCALAPPDATA%\Autodesk\CER\a922…\1795167951406` (`AppLogFile20261120T094124.log`, "Crashing thread ID: 29552").

**Lesson for the add-in:** a bad `adsk.cam` call can kill Fusion outright; there's no Python exception to catch. So:
- keep cloud tool-library access out of normal tool calls
- build tools from JSON files (`Tool.createFromJson`), using Fusion's bundled `Applications\CAM360\Data\libraries\Samples\*.json` or the user's exported libraries
- look posts up by URL, not by enumerating the library
- write a breadcrumb before every CAM call so a crash can be traced to the call that caused it

**Run 2 changes to the spike:**
- a BEGIN/END trace flushed to disk before each step
- results rewritten after every step
- a step that crashed on the previous run is skipped automatically
- the tool now comes from the bundled "6mm Flat Endmill" JSON, and the post from `fanuc.cps` by URL
- the cloud library query and scan run last

## B. Runtime checks

Run: start Fusion → **Shift+S** → **Scripts** → **FusionMCP_Phase0** → **Run**. It creates two
scratch designs, closes them without saving, and never edits your own documents. A message box
reports the pass count at the end.

### Run 2 (2026-11-20 10:45): no crash, 12/18 passed (1 failed because of a spike bug, 5 skipped as a result)

| # | Check | Result |
|---|---|---|
| 1 | Python and Fusion version at runtime | **Pass.** Python 3.14.7, Fusion 2706.1.17. **`sys.executable` is `Fusion360.exe`**, not python.exe, so never derive paths from it by assuming a `Python\` subfolder. |
| 2 | Custom event from a worker thread runs on the main thread and edits geometry | **Pass.** The handler ran on the main thread and created a sketch. Latency **344 ms** (the main thread was busy finishing the CAM steps), so expect about 0.1–0.4 s overhead per bridged call. |
| 3 | Loopback TCP server thread on 127.0.0.1:27190 | **Pass.** Echo round trip worked and `shutdown()` stopped the thread cleanly. |
| 4 | 10 mm cube = 1.0 cm³ | **Pass.** The API is in cm, as planned. |
| 5a–d | CAM design, workspace, setup, strategies | **Pass.** Workspace id `CAMEnvironment`; **55 milling strategies**, e.g. `face`, `adaptive`, `adaptive2d`, `pocket2d`, `contour2d`, `drill`, `parallel`, `scallop`. |
| 5e | Tool from the bundled JSON file | **Fail (spike bug):** the path was built from `sys.executable` one folder too high. Fixed for run 3. |
| 5f–i, 5k | Operation, generate, check, time, post | Skipped (needed the tool). |
| 5j | Post by URL | **Pass.** `system://fanuc.cps` → "FANUC". It took 3 s, the first time the post library loaded. |
| 6 | Screenshot | **Pass** (800×600 PNG). |
| 7 | Cloud tool-library query (`flat end mill`) | **Pass, no crash.** 30 hits in **7.7 s**. Slow, so cache results in the add-in. |
| 8 | Tool-library scan | **Pass.** It lists `systemlibraryroot://Samples/...` libraries. |
| – | Behaviour with a command dialog open | Tested in Phase 1: requests still run, and `status` shows the active command (see `phase1-results.md`) |

**On run 1's crash:** the plain library query did *not* crash this time. Run 1 differed in two ways:
- it added `tool_diameter.min`/`max` criteria to the query
- it went on to create an operation and generate with the library tool

So the crash is in one of those, or in generate/time with that tool. Run 3 exercises the operation, generate, time and post steps with the JSON tool. If it crashes, the trace names the exact step.

### Run 3 (2026-11-20 10:56): 18/19 passed, no crash. The whole CAM pipeline works on an imported STEP

| Step | Result |
|---|---|
| STEP export → import to a new document | **Pass.** 8.9 kB STEP; imported as 1 body, **50.0×40.0×20.0 mm, 40000.0 mm³** (exact). It arrives as a **parametric** design. |
| Tool from the bundled JSON | **Pass.** `Tool.createFromJson` with the "6mm Flat Endmill" from `Milling Tools (Metric).json`. No library API needed. |
| Face operation → generate | **Pass.** `Face1`, generated in **1.8 s**, `hasToolpath` and `isToolpathValid` both true. |
| `checkToolpath` | **Pass** (true). |
| Machining time | **Pass.** **43.1 s**, feed 705.9 mm, rapid 25.4 mm, 0 tool changes. |
| Post | **Partial.** `postProcess` returned True and wrote valid Fanuc G-code (58 lines: T1 M06, S5000 M03, G54, G43 H01, G01 facing passes). But **the file went to `%USERPROFILE%\OneDrive\Documents\Fusion 360\NC Programs\phase0.nc`**. The `nc_program_filename` parameter was honoured; `nc_program_output_folder` set on the *input* was not. |
| Custom event | **Pass.** Latency **10.7 ms** with an idle main thread (344 ms in run 2 was a busy main thread). |
| Cloud library query / scan | Pass again (8.3 s); no crash in two runs. |

**Licence finding: this Fusion is on a Personal Use licence.**
- The posted G-code says so: rapid moves are slowed to the cutting feed rate, and full-speed rapids need a subscription. The crash log also showed `isHobbyistRestricted`.
- Consequences: posted programs run slower on a real machine (rapids at cutting feed), and other Personal Use limits may apply (number of editable documents, some posts and features).
- Fine for coursework and the MCP. Check before machining real parts: an Education licence (free with a University of Bath account) removes these limits.

**Post output folder: fix for the add-in.** Run 4's `5k` sets the folder again on the created NC program (as a value and as a quoted expression) and records what Fusion reads back and whether each parameter is editable. Either way, `cam_post` will **locate the written file and move it** to the requested folder if the folder isn't honoured. That doesn't depend on parameter quirks.

**Phase 0 conclusion:** every assumption the CAM-only plan depends on is confirmed on 2706.1.17, except "dialog open" behaviour (Phase 1 hang test). Run 1's crash didn't reproduce with JSON tools; keep the breadcrumbs and keep the cloud library opt-in.

### Run 3 changes (scope is now CAM-only)
- New step `5_step_export`: builds a 50×40×20 mm block and exports `test_output/phase0_block.step`. This stands in for the Inventor export.
- `5a_cam_doc` now **imports that STEP into a new document** (`importManager.importToNewDocument`) and checks the size (50×40×20 mm) and volume (40000 mm³). It also records whether the import arrives as a direct or parametric design. The CAM steps run on the imported body.
- Fixed the sample tool-library path (install root = the folder containing `Fusion360.exe`).
