"""Core ops: ping, status, exec. Runs on Fusion's main thread only."""

import ast
import contextlib
import io
import math
import sys
import traceback

import adsk.core
import adsk.fusion
import adsk.cam

from . import guards, state

MAX_OUTPUT = 20000


def _app():
    return adsk.core.Application.get()


def ping(args: dict):
    return "pong"


def _doc_info(doc) -> dict:
    info = {"name": doc.name, "saved": doc.isSaved, "modified": doc.isModified,
            "scratch": guards.is_scratch(doc)}
    try:
        info["products"] = [doc.products.item(i).productType for i in range(doc.products.count)]
    except Exception:
        pass
    return info


def status(args: dict):
    app = _app()
    ui = app.userInterface
    docs = [_doc_info(app.documents.item(i)) for i in range(app.documents.count)]
    active = app.activeDocument
    try:
        ws = ui.activeWorkspace.id if ui.activeWorkspace else None
    except Exception:
        ws = None
    try:
        active_cmd = ui.activeCommand        # "SelectCommand" when idle
    except Exception:
        active_cmd = None
    return {
        "fusion_version": app.version,
        "python": sys.version.split()[0],
        "active_document": active.name if active else None,
        "active_workspace": ws,
        "active_command": active_cmd,
        "idle": active_cmd in (None, "SelectCommand"),
        "documents": docs,
        "namespace_vars": sorted(k for k in state.namespace if not k.startswith("_") and k not in _BOUND),
        "last_crash": state.last_crash,
        "addin_started": state.started_at,
    }


_BOUND = {"app", "ui", "adsk", "math", "mm", "deg", "doc", "design", "root", "cam", "__builtins__"}


def _bind(ns: dict) -> None:
    app = _app()
    ns.update(app=app, ui=app.userInterface, adsk=adsk, math=math,
              mm=lambda v: v / 10.0,                 # mm -> cm (API length unit)
              deg=lambda v: math.radians(v))         # deg -> rad (API angle unit)
    doc = app.activeDocument
    ns["doc"] = doc
    design = cam = None
    if doc is not None:
        try:
            design = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
        except Exception:
            design = None
        try:
            cam = adsk.cam.CAM.cast(doc.products.itemByProductType("CAMProductType"))
        except Exception:
            cam = None
    ns["design"] = design
    ns["root"] = design.rootComponent if design else None
    ns["cam"] = cam


def exec_code(args: dict):
    """Run code in the persistent namespace. stdout is returned; a trailing expression is echoed."""
    code = args.get("code", "")
    ns = state.namespace
    _bind(ns)
    # Verified 2026-11-20: requests still run while a command dialog (e.g. Extrude) is open, which
    # can interfere with what the user is doing. Warn rather than refuse (exec is the escape hatch);
    # the CAM tools in later phases refuse to modify documents unless Fusion is idle.
    try:
        active_cmd = ns["ui"].activeCommand
    except Exception:
        active_cmd = None
    warn = (f"WARNING: Fusion is not idle (active command: {active_cmd}). The user may be mid-command; "
            "avoid modifying documents until they finish.\n") if active_cmd not in (None, "SelectCommand") else ""
    buf = io.StringIO()
    try:
        tree = ast.parse(code, "<mcp>", "exec")
        tail = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            tail = ast.Expression(tree.body.pop().value)
        with contextlib.redirect_stdout(buf):
            exec(compile(tree, "<mcp>", "exec"), ns)
            if tail is not None:
                val = eval(compile(tail, "<mcp>", "eval"), ns)
                if val is not None:
                    print(repr(val))
    except Exception as e:
        # report only the user's frames (<mcp>), with the offending source line
        src = code.splitlines()
        frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == "<mcp>"]
        lines = [f"  line {f.lineno}: {src[f.lineno - 1].strip() if 0 < f.lineno <= len(src) else ''}"
                 for f in frames]
        msg = "".join(traceback.format_exception_only(type(e), e)).strip()
        out = buf.getvalue().rstrip()
        err = RuntimeError(((out + "\n") if out else "") + "\n".join(lines + [msg]))
        err.plain = True            # mainthread: report the message as-is, no "RuntimeError:" prefix
        raise err from None
    out = buf.getvalue().rstrip()
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n... (truncated, {len(out)} chars)"
    return warn + (out if out else "(ok, no output)")
