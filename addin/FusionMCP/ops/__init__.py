"""Op registry. `handle(req)` runs on Fusion's main thread, wrapped in breadcrumbs.

Add new ops by registering them in `_build()`. `reload` (handled in FusionMCP.py) reimports
every ops module except `state` and `breadcrumb`, then calls `_build()` again.
"""

from . import breadcrumb, cam, core, docs, geometry, tools, trace

OPS: dict = {}


def _build():
    OPS.clear()
    OPS.update({
        "ping": core.ping,
        "status": core.status,
        "exec": core.exec_code,
        "import_step": docs.import_step,
        "inspect": geometry.inspect,
        "screenshot": docs.screenshot,
        "close_scratch": docs.close_scratch,
        "cam_status": cam.cam_status,
        "cam_create_setup": cam.cam_create_setup,
        "cam_list_strategies": cam.cam_list_strategies,
        "cam_list_tools": tools.cam_list_tools,
        "cam_add_operation": cam.cam_add_operation,
        "cam_generate_start": cam.cam_generate_start,
        "cam_generate_poll": cam.cam_generate_poll,
        "cam_machining_time": cam.cam_machining_time,
        "cam_post": cam.cam_post,
    })


_build()


def handle(req: dict):
    op = req.get("op")
    fn = OPS.get(op)
    if fn is None:
        raise KeyError(f"unknown op '{op}'. Known: {sorted(OPS) + ['reload', 'hello']}")
    args = req.get("args") or {}
    summary = ""
    if op == "exec":
        summary = (args.get("code", "").strip().splitlines() or [""])[0][:80]
    elif op == "import_step":
        summary = str(args.get("path", ""))[-120:]
    elif op == "close_scratch":
        summary = str(args.get("scratch_id") or args.get("name") or "all")[:80]
    elif op.startswith("cam_"):
        summary = " ".join(f"{k}={args[k]}" for k in ("setup", "strategy", "tool_id", "targets", "target", "post",
                                                     "source") if args.get(k) is not None)[:120]
    trace.current.update(op=op, id=req.get("id"))
    breadcrumb.begin(op, req.get("id"), summary)
    ok = False
    try:
        result = fn(args)
        ok = True
        return result
    finally:
        breadcrumb.end(op, req.get("id"), ok)
