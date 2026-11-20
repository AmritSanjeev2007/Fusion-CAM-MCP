# Fusion-CAM-MCP
An Python MCP server that allows AI Clients to import STEP files from Inventor, use them with Fusion's native CAM tools and also a G-code toolpath check outside Fusion.

## Layout
- `addin/FusionMCP`: Fusion add-in. It runs a loopback socket bridge (127.0.0.1:27190, token in `%APPDATA%\FusionMCP\token`) and the ops that run on Fusion's main thread (`ops/*.py`, registered in `ops/__init__.py`).
- `src/`: the `fusion-cam` MCP server. Its tools are thin wrappers around `BridgeClient.call(op, args)`.
- `tests/`: unit tests (`pytest`) and live tests (`pytest -m fusion`, which need Fusion running with the add-in).

## Tools
All tool inputs and outputs are in **mm** (mm², mm³). The raw Fusion API inside `execute_python` uses cm.

| Tool | What it does |
|---|---|
| `status` | Connection, open documents, active command/idle, last crash |
| `execute_python` | Python on Fusion's main thread (persistent namespace) |
| `reload_api` | Re-import `ops/*.py` after editing them |
| `import_step(path, expected_size_mm, expected_volume_mm3, tolerance_pct=0.1)` | Imports a STEP into a **new scratch design**. Returns bodies, bbox and volume, plus an optional check |
| `inspect()` | Bodies, bbox, volume, top-face Z, horizontal planar faces by Z, holes (diameter, depth, through/blind, count) |
| `screenshot(path, width, height, view)` | Viewport PNG (`iso`/`top`/`front`/`right`/`current`), saved to `test_output/` by default |
| `close_scratch(name=None, scratch_id=None)` | Closes, without saving, only documents that `import_step` created and that were never saved. Prefer `scratch_id`, because imported documents are named "Untitled" like a new user document |

`import_step` and `close_scratch` refuse while a Fusion command dialog is open. A call that times out may still be running in Fusion, so call `status`/`inspect` before retrying.

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
4. Do the CAM work (Phase 3), then `close_scratch()`.

## Phase 2 verification
1. In Inventor, export the test plate (80×50×10 mm, R5 corners, Ø16 through hole, 2× M6) to `test_output\MCP_Test_Plate.step`.
2. Run `import_step("C:\\Fusion MCP\\test_output\\MCP_Test_Plate.step", expected_size_mm=[80, 50, 10], expected_volume_mm3=37395)`. Expect `check.ok: true`, i.e. within 0.1 %.
3. Run `inspect()`. Expect top face Z = 10, one Ø16 through hole and two identical through holes at the M6 tap-drill diameter.
4. Run `screenshot(view="iso")`, then `close_scratch(scratch_id=...)`.

Results from 2026-11-20 are in [docs/phase2-results.md](docs/phase2-results.md): 80.000×50.000×10.000 mm, 37395.009 mm³, holes 1× Ø16 and 2× Ø4.917, all through.

Tests:
```powershell
.venv\Scripts\python.exe -m pytest                 # unit tests (no Fusion)
.venv\Scripts\python.exe -m pytest -m fusion -v    # live, needs Fusion + add-in; uses test_output\MCP_Test_Plate.step if present
```
