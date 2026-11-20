"""Op registry. `handle(req)` runs on Fusion's main thread, wrapped in breadcrumbs.

Add new ops by registering them in `_build()`. `reload` (handled in FusionMCP.py) reimports
every ops module except `state` and `breadcrumb`, then calls `_build()` again.
"""

from . import breadcrumb, core, docs, geometry

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
        summary = str(args.get("name") or "all")[:80]
    breadcrumb.begin(op, req.get("id"), summary)
    ok = False
    try:
        result = fn(args)
        ok = True
        return result
    finally:
        breadcrumb.end(op, req.get("id"), ok)
