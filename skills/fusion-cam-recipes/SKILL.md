---
name: fusion-cam-recipes
description: Machining recipes for the fusion-cam MCP server in Autodesk Fusion. Which strategy to use for each feature (facing, outside profile, pockets, through and blind holes, tapped holes), stock and WCS conventions, tool choice from Fusion's bundled sample libraries with real tool ids, which parameters to set (verified names and value formats), feeds and speeds sources and caveats, post configuration and splitting on Personal Use, and the rule that every posted program goes through gcode_check. Use when planning or reviewing a CAM job on a part imported from Inventor, choosing tools or parameters for cam_add_operation, or when a toolpath or program looks wrong. For operating the tools (startup, crash rules, troubleshooting) see fusion-cam-usage.
---

# CAM recipes for fusion-cam

How to turn `inspect()` output into a job. Operating details are in `fusion-cam-usage`.
Recipes marked **verified** were generated, checked and posted on Fusion 2706.1.17 on
2026-11-20 (the Inventor test plate). Unverified ones are marked as such.

## 1. Read the part first

`inspect()` gives everything a 2.5D job needs:
- `overall.bbox_mm`: the stock size and the WCS. Check the part sits Z-up (`import_step` warns
  if the axes don't match Inventor's).
- `top_face_z_mm` and `horizontal_planar_faces`: the facing height, floors (pockets, steps)
  and their Z values.
- `holes`: diameter, depth, `through`, count and centres. `through_basis: "topology"` means
  it was decided from the end faces. Holes along X or Y need a second setup.
- `convex_cylinder_faces`: rounded corners and bosses, which are not holes.

## 2. Stock and WCS

- Default: `cam_create_setup(stock_offset_mm=1, wcs="top-centre")`, a relative box 1 mm on the
  sides and top and 0 at the bottom. The WCS is at the stock top centre, so **Z=0 is the top of
  the stock** and the part top is Z−1 (verified: stock in WCS −41…41 × −26…26 × −11…0).
- Machinist's zero on a corner: `wcs="top-corner-1"` (−X−Y). Fusion's numbering is 1 = −X−Y,
  2 = +X−Y, 3 = −X+Y, 4 = +X+Y (verified). With `{"sides": 2, "top": 0.5}` the origin was at
  world (−42, −27, 10.5).
- Zero on the part instead of the stock: prefix `model-`, e.g. `model-top-centre`.
- Real blank sizes: set offsets so the stock matches the bar you have. Feed the same box to
  `gcode_check` (it reads it from the setup by default).

## 3. Strategy by feature

| Feature | Strategy | Geometry / key params | Status |
|---|---|---|---|
| Top face clean-up | `face` | none. Uses the stock top → model top | **verified** (T1 Ø50 face mill: 1 mm off, 4264 mm³, 42 s) |
| Outside profile, through | `contour2d` | `geometry={"contour":"outline","side":"outside"}`, `bottomHeight_offset "-0.5 mm"`, `doMultipleDepths true`, `maximumStepdown "4 mm"` | **verified** (3 passes to Z−11.5, climb, R10 corner arcs) |
| Through hole ≥ the drill | `drill` | `geometry={"holes":{"diameter_mm":16}}`, `drillTipThroughBottom true`, `breakThroughDepth "1 mm"` | **verified** (G81 to Z−16.807) |
| Small or deep holes | `drill` with peck | add `cycleType "'deep-drilling'"`, `incrementalDepth "3 mm"` | **verified** (G83 Q1.225) |
| Pocket / slot | `pocket2d` or `adaptive2d` | needs pocket face or contour selections: not wrapped yet, use `execute_python` on the op's `pockets`/`contours` parameter | unverified |
| Hole bigger than the drills you have | `bore` or `circular` | `geometry={"holes":{"diameter_mm":d}}` | unverified |
| Tapped hole (M6 arrives as Ø4.917) | `drill` with a tap tool | spot drill, then Ø5.0 drill, then tap (`tap right hand` M6 from `Hole Making Tools (Metric)`). `cycleType` follows the tool (`'tapping'`) | unverified |
| Chamfer edges | `chamfer2d` | chamfer mill (Tutorial T50 Ø10) | unverified |
| 3D surfaces | `adaptive` then `parallel`/`scallop` | ball end mill | unverified |

Order: face → roughing → finishing profiles → spot → drill → tap → chamfer. Keep same-tool
operations consecutive. On Personal Use each tool group becomes its own program.

## 4. Tools from the bundled samples (no cloud)

Library files: `<Fusion install>\Applications\CAM360\Data\libraries\Samples\*.json`.
`cam_list_tools()` lists them; filter by `library`, `type` and diameter.

| Use | Library | Tool | id |
|---|---|---|---|
| Face mill Ø50 | Tutorial Tools (Metric) | face mill | `49d0ef72-1d9e-4d80-aa4c-2fd9384de358` |
| Flat end mill Ø10 | Milling Tools (Metric) | 10mm Flat Endmill | `593e3a17-2722-4974-b6d6-46521c351aa6` |
| Flat end mill Ø6 | Milling Tools (Metric) | 6mm Flat Endmill | `95592c49-3364-4b7f-873a-5a00ffdb78f1` |
| Drill Ø16 | Hole Making Tools (Metric) | drill 118° | `4e82fd33-fab1-4910-b0b1-768c41323e30` |
| Drill Ø4.9 | Hole Making Tools (Metric) | drill 118° | `e4b5958e-d3b7-4249-a084-d9cca4830757` |
| Drill Ø5.0 (M6 tap drill) | Hole Making Tools (Metric) | drill 118° | `2647ba7d-243c-4ab0-95f9-e44a5968aef1` |
| Tap M6 | Hole Making Tools (Metric) | tap right hand | `f5a23d3a-d8bb-4296-ad42-0cb77cb418b6` |

Libraries: Milling (Metric) has 66 tools (ball, flat and bull-nose 3–25 mm). Hole Making
(Metric) has 258 (179 drills, 60 taps, 12 spot drills, reamers, counterbores). Tutorial
(Metric) has 26 numbered tools: face mill, end mills, drills, taps, chamfer and spot.

Pick a drill within a few hundredths of the hole. For a reamed or tight hole, drill
undersize and finish with `bore`.

## 5. Parameters: names and value formats (verified)

- **Lengths need units**: `"1 mm"`, `"-0.5 mm"`. Bools: `true`/`false`. Choice values are
  quoted *inside* the string: `"'deep-drilling'"`. Plain numbers are unitless.
- contour2d: `bottomHeight_offset`, `doMultipleDepths`, `maximumStepdown`. Defaults:
  `compensation 'left'`, `compensationType 'computer'`, `bottomHeight_mode 'from contour'`.
- drill: `drillTipThroughBottom`, `breakThroughDepth`, `cycleType`, `incrementalDepth`.
- feeds and speeds (any op): `tool_spindleSpeed`, `tool_feedCutting`, `tool_feedPlunge`,
  `tool_feedEntry`, `tool_feedExit`, `tool_coolant`.
- Setup (set by `cam_create_setup`): `job_stockMode 'default'`, `job_stockOffsetMode 'simple'`,
  `job_stockOffsetSides/Top/Bottom`, `wcs_origin_mode`, `wcs_origin_boxPoint`.
- Unknown names come back in `parameter_errors`. To find a name, list them with
  `execute_python`: `[op.parameters.item(i).name for i in range(op.parameters.count)]`
  (327 on a face op).

## 6. Feeds and speeds

**The sample tools carry generic presets**: S5000 F1000 for the end mill and both drills, and
S955 for the face mill. That is wrong for a Ø16 drill in steel. Before machining anything real:
- Use the user's own tool library (exported `.json` via `cam_list_tools(source=path)`), whose
  presets match their machine and material, or
- Set `tool_spindleSpeed` / `tool_feedCutting` / `tool_feedPlunge` in `params`, from the tool
  maker's data. Spindle speed n = 1000·Vc / (π·D).
- Say in the summary which feeds are placeholders.

## 7. Post and output

- `cam_post(post="fanuc.cps")`, with the post looked up by URL (`system://fanuc.cps` → "FANUC").
  Other posts from Fusion's library are passed by name, e.g. `"haas.cps"` (unverified).
- **Personal Use**: tool changes in one program are refused, so `split="auto"` writes one
  program per tool group. Each starts with `Tn M06 … G43 Hn` and ends with M30. Run them in
  order, with the same WCS (G54).
- `program_name` (default `"1001"`) is the O number. `filename` is the base name of the files.

## 8. Always check the G-code (no exceptions)

Fusion has no stock-simulation API, and `checkToolpath` only validates the toolpath. Before
telling the user a program is ready:
```text
gcode_check(paths=[all files cam_post returned, in order])
```
- `ok: false` means **do not run it.** Report each error with its file and line, e.g.
  `RAPID_INTO_STOCK bad_rapid.nc line 22 (N63) rapid move cuts 4.000 mm into material with T2`.
- `BELOW_STOCK` (info) is normal for through holes and profiles. Check that the spoilboard or
  fixture allows the reported depth (T3 Z−16.807 = 5.8 mm below the stock).
- `remaining_volume_mm3` should be close to the CAD volume after full machining (plate:
  37380 against 37395 mm³). A large difference means something was missed or over-cut.
- Pass `limits_wcs_mm` with the machine's travels in WCS when they are known.
- CAMotics is not installed on this PC; `gcode_check` uses its own height-map simulation (3-axis,
  cutter profile only, no holder or fixture).
