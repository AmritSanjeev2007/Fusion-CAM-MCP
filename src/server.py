"""fusion-cam MCP server: CAM in Autodesk Fusion via the FusionMCP add-in.

Phase 1 tools: status, execute_python, reload_api. CAM and STEP-import tools arrive in
Phases 2-3 (see docs/FUSION_MCP_PLAN.md).
"""

import json

from mcp.server.fastmcp import FastMCP

from .bridge_client import BridgeClient, BridgeError

mcp = FastMCP(
    "fusion-cam",
    instructions=(
        "CAM in Autodesk Fusion through the FusionMCP add-in. Models come from Inventor as STEP files; "
        "Fusion is only used for machining (setups, toolpaths, posting). Call status first. "
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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
