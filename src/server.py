"""fusion-cam MCP server: CAM in Autodesk Fusion via the FusionMCP add-in.

Phase 1 tools: status, execute_python, reload_api.
Phase 2 tools (STEP import from Inventor): import_step, inspect, screenshot, close_scratch.
CAM tools arrive in Phase 3 (see FUSION_MCP_PLAN.md). Every tool takes and returns mm.
"""

import json

from mcp.server.fastmcp import FastMCP

from .bridge_client import BridgeClient, BridgeError

mcp = FastMCP(
    "fusion-cam",
    instructions=(
        "CAM in Autodesk Fusion through the FusionMCP add-in. Models come from Inventor as STEP files; "
        "Fusion is only used for machining (setups, toolpaths, posting). Call status first. "
        "Workflow: import_step (STEP exported from Inventor) -> inspect -> screenshot -> CAM -> close_scratch. "
        "All tool inputs and outputs are in mm (mm^2, mm^3). A call that times out may still finish inside "
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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
