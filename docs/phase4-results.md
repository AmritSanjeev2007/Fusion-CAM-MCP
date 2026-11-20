# Phase 4 results: G-code check outside Fusion (2026-11-20)

## Built
- `external/gcode_check.py` uses the standard library only and runs in the MCP server or as a CLI.
  - **Parser:** modal G0/G1/G2/G3 (G17/G18/G19 planes, I/J/K and R arcs), canned cycles G81/G82/G83/G73/G85/G86 with G98/G99, R and Z, G90/G91, G20/G21, G43 H, T/M06, S with M03/M04/M05, F, G28, and comments. Files are simulated **in order on one stock**, because Personal Use posting writes one program per tool.
  - **Height-map simulation:** a Z dexel grid of the stock, 0.5 mm cells by default. Cutters are flat, bull-nose, ball or drill-point, taken from the post's tool comments, e.g. `(T3 D=16. CR=0. TAPER=118DEG - …)`. Distances from the tool axis to cell centres are exact.
  - **Rules, reported by file, line and N number:** `RAPID_INTO_STOCK`, `RAPID_BELOW_TOP`, `FEED_MISSING`, `SPINDLE_OFF`, `NO_TOOL`, `TOOL_UNEXPECTED`, `H_MISMATCH`, `ENVELOPE`, `ARC_RADIUS`, `CYCLE_INCOMPLETE`, `NO_END`, `BELOW_STOCK` (info), `UNKNOWN_CODE` (info).
- **MCP tool `gcode_check(paths, stock_wcs_mm=None, setup=None, limits_wcs_mm, expected_tools, clearance_mm, resolution_mm)`.** If no stock is given, it reads the stock box (`stock_in_wcs_mm`) and the expected tool numbers from the Fusion setup through `cam_status`. `ok=false` means don't run the program.
- **CLI:** `python external/gcode_check.py --stock -41,-26,-11,41,26,0 --tools 1,2,3,4 a.nc b.nc …` exits with 0 when the program passes and 1 when it has errors.

## CAMotics
**Not installed on this PC**: not on PATH, and not in `Program Files` or `Program Files (x86)`. I didn't install it, because downloading software needs your approval. `gcode_check` reports `camotics: {installed: false}` and uses the built-in height-map simulation. That simulation detects rapids through material and reports removed volume, which is what the plan wanted CAMotics for. If CAMotics is installed later, its path is reported, but it isn't run automatically because that path couldn't be verified here.

## Results on the Phase 3 test plate job
Job: the four Fanuc programs from Phase 3. T1 Ø50 face mill, T2 Ø10 2D contour, T3 Ø16 drill (G81), T4 Ø4.9 peck drill (G83). Stock 82×52×11 mm, WCS at the top centre of the stock.

Run through MCP over stdio: `import_step` → setup → 4 ops → `cam_generate` → `cam_post` → `gcode_check(paths)`, with no stock argument.

| Item | Result |
|---|---|
| Stock source | Read from Fusion: setup `Setup1`, WCS box (−41, −26, −11) to (41, 26, 0). Expected tools [1, 2, 3, 4] |
| Verdict | **ok: true**, 0 errors, 0 warnings, 3 info |
| Info | `BELOW_STOCK`. T2 reaches Z−11.5 (0.5 mm below the stock, as asked for with `bottomHeight_offset`). T3 reaches Z−16.807 and T4 Z−13.472 (drill points and 1 mm breakthrough). These are normal for through features; check the spoilboard or fixture |
| Removed volume | **9524 mm³** at 0.5 mm cells (9506.5 mm³ at 0.25 mm). Stock 46 904 mm³ − removed = **37 380 mm³** (37 397.5 at 0.25 mm) against the CAD part's **37 395 mm³**: −0.04 % (+0.007 %) |
| Per tool | T1 removed 4264 mm³, exactly the 1 mm facing (82×52×1). T3 removed 1992 mm³ for the Ø16 hole |
| Time | 1.6 s for all four files at 0.5 mm |

## Done criterion: a rapid through the stock is flagged at the right line
I inserted `N62 G00 Z-5.` and `N63 G00 X36. Y-33.` into the T2 program, before `N65`. Result: `ok: false`, and the first error is exactly the inserted move:
```
RAPID_INTO_STOCK  bad_rapid.nc line 22 (N63)  "N63 G00 X36. Y-33."  rapid move cuts 4.000 mm into material with T2 (D10)
```
The cut is 4 mm, not 5, because T1's facing had already lowered the top to Z−1. Two follow-on `ARC_RADIUS` errors are expected: the inserted move leaves the tool somewhere the next arcs don't start from. This case is in `tests/test_gcode_check.py::test_done_criterion_rapid_through_stock_flagged_at_right_line`, which uses the real posted programs in `tests/data/plate/`.

## Tests
Unit tests: **56/56**, of which 13 are new G-code checks. They cover:
- the real job is clean and its removal matches the CAD part within 0.2 %
- the done criterion
- a rapid starting low
- a rapid through air below the top giving a warning (and the clearance option)
- feed, spindle and tool rules
- expected tools, H mismatch and missing M30
- the axis envelope
- arc radius, and a G18 quarter arc that must not sweep 270°
- inch and incremental input
- canned-cycle removal and a cycle missing R
- tool profiles
- CAMotics status
- CLI exit codes

## Limits
- 3-axis only, with no holder or shank collision: only the cutting profile is modelled. Fixtures and clamps are not modelled.
- Personal Use posts slow rapids to the cutting feed. If a post turns rapids into G1 moves, they are checked as cuts, not as rapids.
- G83 pecks are simulated as one feed to depth plus a retract. Removal is the same.
