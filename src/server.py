"""fusion-cam MCP server: CAM in Autodesk Fusion via the FusionMCP add-in.

Phase 1 tools: status, execute_python, reload_api.
Phase 2 tools (STEP import from Inventor): import_step, inspect, screenshot, close_scratch.
Phase 3 tools (CAM): cam_status, cam_create_setup, cam_list_strategies, cam_list_tools,
cam_add_operation, cam_generate, cam_machining_time, cam_post.
Phase 4 tool: gcode_check (external/gcode_check.py, runs outside Fusion).
Every tool takes and returns mm (times in s, feeds in mm/min).
"""

import json

from mcp.server.fastmcp import FastMCP

from .bridge_client import BridgeClient, BridgeError
from .polling import poll_generation

mcp = FastMCP(
    "fusion-cam",
    instructions=(
        "CAM in Autodesk Fusion through the FusionMCP add-in. Models come from Inventor as STEP files; "
        "Fusion is only used for machining (setups, toolpaths, posting). Call status first. "
        "Workflow: import_step (STEP exported from Inventor) -> inspect -> screenshot -> cam_create_setup -> "
        "cam_list_tools -> cam_add_operation (face, contour2d, drill, ...) -> cam_generate -> cam_machining_time -> "
        "cam_post -> gcode_check (always, before any machine) -> close_scratch. CAM tools only modify scratch "
        "documents from import_step. "
        "All tool inputs and outputs are in mm (mm^2, mm^3), seconds and mm/min. A call that times out may still finish inside "
        "Fusion: call status and inspect before retrying it. "
        "execute_python runs on Fusion's main thread in a persistent namespace with app, ui, adsk, doc, "
        "design, root, cam, mm(), deg() bound; the raw API uses cm and radians. A bad adsk.cam call can crash "
        "Fusion outright: if status reports last_crash, do not repeat that call."
    ),
)

_client = BridgeClient()


def _fmt_error(e: BridgeError) -> str:
    text = f"ERROR ({e.kind}): {e}"
    if e.traceback and e.kind != "exception":
        text += f"\n{e.traceback}"
    return text


@mcp.tool()
def status() -> str:
    """Check the Fusion connection: add-in reachable, Fusion's main thread responsive, open documents,
    active workspace/command, and whether Fusion crashed during a call last time (last_crash)."""
    try:
        hello = _client.call("hello", timeout=5)
    except BridgeError as e:
        return _fmt_error(e)
    try:
        st = _client.call("status", timeout=10)
    except BridgeError as e:
        if e.kind == "timeout":
            return (f"Add-in reachable ({json.dumps(hello)}) but Fusion's main thread did not answer within 10 s: "
                    "Fusion is busy or a modal dialog/command is open. Ask the user to check Fusion.")
        return _fmt_error(e)
    st["addin"] = hello
    out = json.dumps(st, indent=1)
    if st.get("last_crash"):
        c = st["last_crash"]
        out = (f"WARNING: Fusion crashed during '{c['op']}' ({c['summary']}) at {c['when']}. "
               "Do not repeat that call unchanged.\n") + out
    return out


@mcp.tool()
def execute_python(code: str, timeout_s: float = 60) -> str:
    """Run Python inside Fusion on its main thread.

    Pre-bound (refreshed each call): app, ui, adsk, math, doc (active document), design, root
    (design.rootComponent), cam (CAM product or None), mm(v) mm->cm, deg(v) deg->rad.
    Variables persist between calls. Raw API lengths are CENTIMETERS. print() output is returned;
    a trailing bare expression is echoed like a REPL. Never close or save documents you did not
    create. Avoid long loops: the main thread is blocked while this runs.
    """
    try:
        return _client.call("exec", {"code": code}, timeout=timeout_s)
    except BridgeError as e:
        return _fmt_error(e)


@mcp.tool()
def reload_api() -> str:
    """Re-import the add-in's ops modules from disk (after editing addin/FusionMCP/ops/*.py) and clear
    the execute_python namespace. Changes to FusionMCP.py or bridge/* need the add-in stopped and run
    again in Fusion (Shift+S > Add-Ins)."""
    try:
        return json.dumps(_client.call("reload", timeout=30))
    except BridgeError as e:
        return _fmt_error(e)


def _json(op: str, args: dict, timeout: float) -> str:
    try:
        return json.dumps(_client.call(op, args, timeout=timeout), indent=1)
    except BridgeError as e:
        out = _fmt_error(e)
        if e.kind == "timeout":
            out += "\nThe call may still be running in Fusion: call status and inspect before retrying."
        return out


@mcp.tool()
def import_step(path: str, expected_size_mm: list[float] | None = None,
                expected_volume_mm3: float | None = None, tolerance_pct: float = 0.1,
                timeout_s: float = 120) -> str:
    """Import a STEP file (e.g. exported from Inventor) into a NEW Fusion design marked as scratch.

    Units are millimetres. path: absolute path to a .step/.stp file. Returns the document name,
    body count, per-body and overall bounding box (mm) and volume (mm^3). Optional check:
    expected_size_mm=[L, W, H] and/or expected_volume_mm3, compared within tolerance_pct (default
    0.1 %). Sizes are compared sorted (axis-agnostic) and a warning says if the axes differ (e.g. a
    Y-up vs Z-up export). Refuses while Fusion is mid-command. Only documents created here can be
    closed later with close_scratch. If the call times out it may still finish inside Fusion: call
    status and inspect before retrying, or you get a second document.
    """
    args = {"path": path, "tolerance_pct": tolerance_pct}
    if expected_size_mm is not None:
        args["expected_size_mm"] = expected_size_mm
    if expected_volume_mm3 is not None:
        args["expected_volume_mm3"] = expected_volume_mm3
    return _json("import_step", args, timeout_s)


@mcp.tool()
def inspect(timeout_s: float = 60) -> str:
    """Describe the active design for CAM planning. Read-only. All values in mm (areas mm^2,
    volumes mm^3), world coordinates.

    Returns bodies (including occurrence proxies) with bounding boxes and volumes, the overall
    bbox/volume, top_face_z_mm (highest upward-facing planar face), horizontal planar faces grouped
    by Z with area and facing, and cylindrical holes (concave cylinders, split faces merged) grouped
    by diameter/depth/axis with a count, centres, entry points and through/blind. Hole depth is the
    cylindrical wall only (no drill point). If a call times out it may still be running in Fusion:
    call status, then inspect again, before retrying anything.
    """
    return _json("inspect", {}, timeout_s)


@mcp.tool()
def screenshot(path: str | None = None, width: int = 1600, height: int = 1000, view: str = "iso",
               timeout_s: float = 60) -> str:
    """Save the active viewport as a PNG and return its path (sizes in px).

    view: iso | top | front | right | current ('current' keeps the user's camera; the others
    reorient and fit the view). Default path: <repo>/test_output/screenshot_<time>_<view>.png.
    Read the PNG to look at it. A timed-out call may still finish: call status before retrying.
    """
    args = {"width": width, "height": height, "view": view}
    if path:
        args["path"] = path
    return _json("screenshot", args, timeout_s)


@mcp.tool()
def close_scratch(name: str | None = None, scratch_id: str | None = None, timeout_s: float = 60) -> str:
    """Close, WITHOUT saving, documents that import_step created and that were never saved.

    name=None closes all of them; with a name, only that document, and it refuses (with the reason)
    if the document was not created by import_step or has been saved. scratch_id (returned by
    import_step) selects exactly one document; prefer it, because imported documents are often named
    "Untitled" like the user's own. Never closes the user's documents. A timed-out call may still
    finish: call status before retrying.
    """
    args = {}
    if name:
        args["name"] = name
    if scratch_id:
        args["scratch_id"] = scratch_id
    return _json("close_scratch", args, timeout_s)


# ---------------------------------------------------------------- Phase 3: CAM
@mcp.tool()
def cam_status(check: bool = True, timeout_s: float = 60) -> str:
    """CAM state of the active document. Read-only; lengths in mm.

    Setups (WCS, stock offsets, stock box in WCS mm, model size), operations with strategy, tool
    (number, type, diameter mm), toolpath present/valid, errors/warnings and checkToolpath (check=true),
    NC programs, running generation jobs, and last_crash (op and step) if Fusion crashed in a call.
    """
    return _json("cam_status", {"check": check}, timeout_s)


@mcp.tool()
def cam_create_setup(stock_offset_mm: float | dict = 1.0, wcs: str = "top-centre", body: str | int | None = None,
                     name: str | None = None, allow_user_document: bool = False, timeout_s: float = 60) -> str:
    """Create a milling setup on the scratch document (opens the Manufacture workspace). Units mm.

    stock_offset_mm: relative-box stock, a number (sides and top; bottom 0) or {"sides", "top", "bottom"}.
    wcs: stock box point: 'top-centre' (default), 'top-corner-1'..'top-corner-4' (1=-X-Y, 2=+X-Y, 3=-X+Y,
    4=+X+Y), 'centre', 'bottom-centre', 'bottom-corner-N'; prefix 'model-' for the model box instead; or
    'model-origin'. body: body name or index (default: all solid bodies). Returns the stock box in world mm
    and in WCS mm and the WCS origin in world mm. Refuses on documents not created by import_step unless
    allow_user_document=true, and while Fusion is mid-command.
    """
    args = {"stock_offset_mm": stock_offset_mm, "wcs": wcs, "allow_user_document": allow_user_document}
    if body is not None:
        args["body"] = body
    if name:
        args["name"] = name
    return _json("cam_create_setup", args, timeout_s)


@mcp.tool()
def cam_list_strategies(setup: str | None = None, timeout_s: float = 30) -> str:
    """List the operation strategies Fusion offers for a setup (live; 55 milling strategies on 2706.1.17,
    e.g. face, adaptive2d, pocket2d, contour2d, drill, bore, chamfer2d, adaptive, parallel, scallop).
    Pass the 'name' to cam_add_operation."""
    return _json("cam_list_strategies", {"setup": setup} if setup else {}, timeout_s)


@mcp.tool()
def cam_list_tools(source: str = "samples", library: str | None = None, type: str | None = None,
                   diameter_min_mm: float | None = None, diameter_max_mm: float | None = None,
                   text: str | None = None, limit: int = 40, timeout_s: float = 60) -> str:
    """Find cutting tools; diameters in mm. Returns tool ids for cam_add_operation.

    source: 'samples' (Fusion's bundled libraries, e.g. 'Milling Tools (Metric)', 'Hole Making Tools
    (Metric)', 'Tutorial Tools (Metric)'; call with no filters to list them), a path to a tool library .json
    exported from Fusion, or 'cloud' (Fusion's online library: slow, about 8 s, and implicated in a Fusion
    crash in Phase 0, so only when the user asks). type: e.g. 'flat end mill', 'face mill', 'drill',
    'spot drill', 'tap right hand', 'chamfer mill'. text filters the description. The sample tools carry
    generic feeds/speeds presets (e.g. S5000 F1000): review them before real machining.
    """
    args = {"source": source, "limit": limit}
    for k, v in (("library", library), ("type", type), ("diameter_min_mm", diameter_min_mm),
                 ("diameter_max_mm", diameter_max_mm), ("text", text)):
        if v is not None:
            args[k] = v
    return _json("cam_list_tools", args, timeout_s)


@mcp.tool()
def cam_add_operation(strategy: str, tool_id: str, setup: str | None = None, geometry: dict | None = None,
                      params: dict | None = None, params_json: str | None = None, tool_number: int | None = None,
                      name: str | None = None, allow_user_document: bool = False, timeout_s: float = 60) -> str:
    """Add an operation to a setup with a tool from cam_list_tools (made with Tool.createFromJson).

    strategy: a name from cam_list_strategies (face, contour2d, pocket2d, adaptive2d, drill, ...).
    geometry, the selections a strategy needs:
      {"holes": {"diameter_mm": 16, "tol_mm": 0.01}} or {"holes": "all"}: hole faces for drill/bore;
      {"contour": "outline", "side": "outside"}: contour2d on the part's base silhouette (climb cut).
    params / params_json: Fusion parameter name -> value. Strings are expressions and LENGTHS NEED UNITS
    ('1 mm', '-0.5 mm'); bools become true/false; numbers are unitless. Verified names: contour2d
    bottomHeight_offset, doMultipleDepths, maximumStepdown; drill drillTipThroughBottom, breakThroughDepth,
    cycleType ("'drilling'", "'deep-drilling'"), incrementalDepth; any op tool_spindleSpeed, tool_feedCutting.
    tool_number: default reuses the number of the same tool, else the next free one (H/D offsets follow).
    Returns parameter_errors for any names that did not apply.
    """
    args = {"strategy": strategy, "tool_id": tool_id, "allow_user_document": allow_user_document}
    for k, v in (("setup", setup), ("geometry", geometry), ("params", params), ("params_json", params_json),
                 ("tool_number", tool_number), ("name", name)):
        if v is not None:
            args[k] = v
    return _json("cam_add_operation", args, timeout_s)


@mcp.tool()
def cam_generate(targets: list[str] | None = None, setup: str | None = None, job: str | None = None,
                 timeout_s: float = 600, allow_user_document: bool = False) -> str:
    """Generate toolpaths and wait for them (seconds). Returns each operation's toolpath validity and
    checkToolpath result, plus all_valid.

    targets: setup or operation names (default: all operations of the last setup). Fusion generates in the
    background and this tool polls with short calls, so Fusion stays responsive. If timeout_s passes,
    generation continues in Fusion: call cam_generate(job=<id>) to keep waiting rather than starting again.
    A bad adsk.cam call can crash Fusion: if status reports last_crash, do not repeat that call unchanged.
    """
    if job is None:
        args = {"allow_user_document": allow_user_document}
        if targets:
            args["targets"] = targets
        if setup:
            args["setup"] = setup
        try:
            started = _client.call("cam_generate_start", args, timeout=60)
        except BridgeError as e:
            return _fmt_error(e)
        job = started["job"]
    try:
        res = poll_generation(lambda op, a, t: _client.call(op, a, timeout=t), job, timeout_s)
    except BridgeError as e:
        return (_fmt_error(e) + f"\nGeneration job {job} may still be running: call cam_status or "
                f"cam_generate(job='{job}').")
    return json.dumps(res, indent=1)


@mcp.tool()
def cam_machining_time(target: str | None = None, rapid_mm_min: float = 5000, tool_change_s: float = 15,
                       feed_scale_pct: float = 100, timeout_s: float = 60) -> str:
    """Estimated cycle time in seconds for a setup or operation (default: the last setup), with feed and
    rapid distances in mm, tool changes and a per-operation breakdown. rapid_mm_min (mm/min) and
    tool_change_s are machine assumptions. Needs generated toolpaths."""
    args = {"rapid_mm_min": rapid_mm_min, "tool_change_s": tool_change_s, "feed_scale_pct": feed_scale_pct}
    if target:
        args["target"] = target
    return _json("cam_machining_time", args, timeout_s)


@mcp.tool()
def cam_post(post: str = "fanuc.cps", targets: list[str] | None = None, setup: str | None = None,
             output_dir: str | None = None, filename: str | None = None, program_name: str = "1001",
             split: str = "auto", head_lines: int = 20, allow_user_document: bool = False,
             timeout_s: float = 180) -> str:
    """Post-process to G-code; returns the .nc paths, line counts and the first head_lines lines.

    post: a post from Fusion's library by name ('fanuc.cps') or a system:// URL. targets: setup/operation
    names (default: all operations of the last setup), which need valid toolpaths. output_dir default:
    <repo>/test_output/nc. split: 'auto' posts one program, but Fusion for Personal Use refuses tool
    changes in one program, so it then writes one program per tool (<filename>_01_T1.nc, ...; run them in
    order); 'tool' always splits; 'none' fails instead. Files Fusion writes to its default NC folder are
    moved to output_dir. A bad adsk.cam call can crash Fusion: if status reports last_crash, do not repeat
    that call unchanged.
    """
    args = {"post": post, "program_name": program_name, "split": split, "head_lines": head_lines,
            "allow_user_document": allow_user_document}
    for k, v in (("targets", targets), ("setup", setup), ("output_dir", output_dir), ("filename", filename)):
        if v is not None:
            args[k] = v
    return _json("cam_post", args, timeout_s)


# ---------------------------------------------------------------- Phase 4: G-code check (outside Fusion)
def _load_gcode_check():
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "external", "gcode_check.py")
    spec = importlib.util.spec_from_file_location("fusionmcp_gcode_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@mcp.tool()
def gcode_check(paths: list[str], stock_wcs_mm: list[float] | None = None, setup: str | None = None,
                limits_wcs_mm: list[float] | None = None, expected_tools: list[int] | None = None,
                clearance_mm: float = 0.0, resolution_mm: float = 0.5) -> str:
    """Check posted G-code OUTSIDE Fusion before it goes near a machine (Fusion has no stock simulation;
    checkToolpath is not one). Runs in the MCP server, so Fusion is only needed to read the stock.

    paths: .nc files in machining order (pass all files cam_post wrote, e.g. one per tool on Personal
    Use). Coordinates are WCS mm. stock_wcs_mm: [xmin, ymin, zmin, xmax, ymax, zmax]; if omitted, the
    stock box and the expected tool numbers are read from the Fusion setup (cam_status stock_in_wcs_mm;
    `setup` name, default the last setup). limits_wcs_mm: optional axis envelope, same format.
    clearance_mm: rapids below stock top + clearance inside the footprint are warned about.
    Simulates material removal on a height map (resolution_mm cells; tool shapes from the post's tool
    comments) and reports problems by file and line: RAPID_INTO_STOCK, FEED_MISSING, SPINDLE_OFF,
    NO_TOOL, TOOL_UNEXPECTED, H_MISMATCH, ENVELOPE, ARC_RADIUS, NO_END (errors/warnings) and BELOW_STOCK
    (info: normal for through holes and profiles). ok=false means do not run the program.
    """
    gc = _load_gcode_check()
    source = "argument"
    if stock_wcs_mm is None:
        try:
            st = _client.call("cam_status", {"check": False}, timeout=30)
        except BridgeError as e:
            return _fmt_error(e) + "\nPass stock_wcs_mm explicitly to check without Fusion."
        setups = st.get("setups") or []
        s = next((x for x in setups if x["name"] == setup), None) if setup else (setups[-1] if setups else None)
        if s is None or "stock_in_wcs_mm" not in s:
            return f"ERROR: no setup{' ' + setup if setup else ''} with a stock box in Fusion; pass stock_wcs_mm."
        stock_wcs_mm = s["stock_in_wcs_mm"]["min"] + s["stock_in_wcs_mm"]["max"]
        if expected_tools is None:
            expected_tools = sorted({o["tool"]["number"] for o in s["operations"] if o.get("tool", {}).get("number")})
        source = f"Fusion setup '{s['name']}' in {st['document']}"
    try:
        r = gc.check(paths, stock_wcs_mm, limits_wcs_mm, expected_tools, clearance_mm, resolution_mm)
    except (FileNotFoundError, ValueError) as e:
        return f"ERROR: {e}"
    r["stock_source"] = source
    r["expected_tools"] = expected_tools
    return json.dumps(r, indent=1)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
