# Phase 3 results: CAM (2026-11-20)

Fusion 2706.1.17 (Personal Use licence), Python 3.14.7. The part is the Inventor test plate (`test_output\MCP_Test_Plate.step`, 80×50×10 mm, R5 corners, Ø16 and 2× Ø4.917 through holes).

## Built
- `ops/cam.py`: `cam_status`, `cam_create_setup`, `cam_list_strategies`, `cam_add_operation`, `cam_generate_start` / `cam_generate_poll`, `cam_machining_time`, `cam_post`.
- `ops/tools.py`: `cam_list_tools`, with tools from Fusion's bundled `Samples\*.json`, a user library `.json`, or the cloud (opt-in only).
- `ops/camcalc.py`: the maths, with no adsk import. It covers WCS/stock parameters, the stock box, the WCS origin, parameter expressions, time conversion, tool numbering, the post URL and tool-group splitting.
- `ops/trace.py` + `breadcrumb.py`: `STEP <op> <id> <call>` lines are written (and fsync'd) before each risky adsk.cam call. After a crash, `status` → `last_crash` names both the op and the last step it reached. The step parser is in `breadcrumb.py`, which is never reloaded, so it takes effect at the next add-in start. The STEP lines themselves are written already.
- `src/polling.py`: the add-in only *starts* generation. The server polls `cam_generate_poll` every 0.5 s with short main-thread calls, so the main thread is never blocked. If `cam_generate` times out, generation keeps running in Fusion; resume waiting with `cam_generate(job=...)`.
- Guards: every CAM op that modifies a document requires Fusion to be idle and the document to be a scratch document from `import_step`, unless `allow_user_document=true` is passed. `cam_status`, `cam_list_*` and `cam_machining_time` are read-only.

## API facts found live (2706.1.17)
| Item | Finding |
|---|---|
| CAM product | `doc.products.itemByProductType("CAMProductType")` raises "failed to find product" until the Manufacture workspace (`CAMEnvironment`) has been activated on the document. `cam_create_setup` activates it |
| Setup parameters | Stock: `job_stockMode` (`'default'` = relative box), `job_stockOffsetMode` (`'simple'`), `job_stockOffsetSides/Top/Bottom`. WCS: `wcs_origin_mode` (`'stockPoint'`, `'modelPoint'`, `'modelOrigin'`, …), `wcs_origin_boxPoint` (`'top center'`, `'top 1'`…`'top 4'`, `'center'`, `'bottom …'`). Defaults are 1 mm sides/top, stock box point top centre |
| Corner numbering | Read back from `stockX/YLow/High`: 1 = −X−Y, 2 = +X−Y, **3 = −X+Y, 4 = +X+Y** |
| Stock read-back | `stockXLow…stockZHigh` are in WCS coordinates. The 1 mm offset with a top-centre WCS gives X −41…41, Y −26…26, Z −11…0 |
| Strategies | `setup.operations.compatibleStrategies`: **55**, the same list as Phase 0 |
| Tools | `Tool.createFromJson` on the bundled sample JSON. The samples have tool number 0 and length/diameter offsets 0, which posts as `H00`, so the add-in sets number = H = D |
| Geometry | Drill: `holeFaces` is a `CadObjectParameterValue`; set `.value = [faces]`. The Ø16 hole is 1 face and the 2× Ø4.917 are 2 faces. 2D contour: `contours` is a `CadContours2dParameterValue` → `getCurveSelections()` → `createNewChainSelection()` → `inputGeometry=[edge]` → `applyCurveSelections`. **On the bottom face's outer loop the default side is inside** (tool centre at ±35, ±20). `isReverted=True` puts it outside (±45, ±30 with R10 arcs) as a climb cut with left compensation |
| Parameters that worked | `bottomHeight_offset`, `doMultipleDepths`, `maximumStepdown`, `drillTipThroughBottom`, `breakThroughDepth`, `cycleType` (`'deep-drilling'` → G83), `incrementalDepth` |
| Generation | `cam.generateToolpath(op)` returns a `GenerateToolpathFuture` immediately. It completes while the main thread is idle between bridge calls. `numberOfCompleted` under-counts (2 of 4 when complete), so only `isGenerationCompleted` is used. `op.isGenerating` stays True after completion, so it isn't reported |
| Checks | `cam.checkToolpath(op_or_setup)` takes **one** object (a list raises TypeError). **`cam.checkAllToolpaths()` fails** with "The operations are not CAM objects" on this build, so it isn't used |
| Machining time | `cam.getMachiningTime(op_or_setup, feedScale %, rapid cm/s, toolChange s)` also returns `totalFeedTime`, `totalRapidTime`, `totalToolChangeTime` |
| Posting | `nc_program_output_folder` set as a quoted expression on the input **is honoured**: files were written to the requested folder (Phase 0 had set `.value.value`). The add-in still searches Fusion's default NC folder and moves the file if it lands there |
| **Personal Use limit** | One NC program with tool changes fails: `postProcess` returns False and `nc.error` says Personal Use cannot post tool changes in one program. `cam_post(split="auto")` detects this and writes **one program per consecutive tool group**. The posted G-code also carries the Personal Use comment that rapids run at the cutting feed |
| Post lookup | `postLibrary.postConfigurationAtURL(URL.create("system://fanuc.cps"))` → "FANUC" |

## Live job: test plate (face + 2D contour + 2 drilling ops)
| Step | Result |
|---|---|
| `cam_create_setup(stock_offset_mm=1, wcs="top-centre")` | Stock 82×52×11 mm (world X −41…41, Y −26…26, Z 0…11). WCS origin at world (0, 0, 11). No parameter errors |
| Tools (all from the bundled samples) | T1 Ø50 face mill (Tutorial), T2 Ø10 flat end mill (Milling), T3 Ø16 drill, T4 Ø4.9 drill (Hole Making) |
| Operations | `Face1`. `2D Contour1`: outline, outside, 3 depths of ≤4 mm down to −0.5 mm below the part. `Drill1`: Ø16, G81 to Z−16.807. `Drill2`: 2× Ø4.9, G83 peck to Z−13.472 with Q1.225 |
| `cam_generate` | All 4 valid in **2.6 s**, `checkToolpath` true for each |
| `cam_machining_time` (rapid 5000 mm/min, 15 s per tool change) | **151.2 s (2:31)**: feed 1294 mm in 104.8 s, rapid 702.9 mm, 3 tool changes (45 s). Face 42.4 s, contour 58.9 s, Ø16 drill 1.3 s, Ø4.9 drills 3.5 s |
| `cam_post("fanuc.cps")` | The single program was refused (Personal Use), so it auto-split into `mcp_plate_01_T1.nc` (40 lines), `_02_T2.nc` (85), `_03_T3.nc` (30), `_04_T4.nc` (31), in `test_output\nc\`. Each has `Tn M06` and `G43 … Hn`, and ends with `M30` |
| MCP over stdio | `cam_create_setup` refuses the user's document. With `{"sides": 2, "top": 0.5}` and `top-corner-1`, the WCS is at world (−42, −27, 10.5) and the stock in WCS runs 0…84 × 0…54 × −10.5…0. `cam_generate` took 2 polls / 1.2 s. A single-tool job posts as one file (`Setup1.nc`, 40 lines) |
| Crashes | None. Every call left a BEGIN/STEP/END trail in `breadcrumb.log` |

Head of `mcp_plate_04_T4.nc`:
```
(T4 D=4.9 CR=0. TAPER=118DEG - ZMIN=-13.472 - DRILL)
N30 T4 M06
N35 S5000 M03
N60 G43 Z15. H04
N70 G98 G83 X28. Y0. Z-13.472 R4. Q1.225 F1000.
```

## Tests
- Unit tests (`pytest`): **43/43**. That includes 14 new CAM tests: WCS/stock parameters, the stock box against Fusion's read-back, corner numbering, parameter parsing, time conversion, tool JSON numbering, tool-group splitting, the Personal Use message, polling with a fake clock, and the breadcrumb crash step.
- Live tests (`pytest -m fusion`): **17/17**. Phase 1 (6), Phase 2 (5) and Phase 3 (6) in `tests/test_live_cam.py`: setup, strategies/tools, 4 operations, generate and check, time, Fanuc post.

## Caveats
- The sample tools' feeds and speeds are generic presets (S5000 F1000 even for the Ø16 drill; the face mill has S955). Set `tool_spindleSpeed` / `tool_feedCutting` in `params`, or use a real tool library, before machining.
- `checkToolpath` validates the toolpath. It is **not** a stock or collision simulation; that is Phase 4.
- Tapped M6 holes arrive as plain Ø4.917 cylinders (Phase 2), so tapping needs an explicit `drill` op with a tap tool.
