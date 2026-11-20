"""FusionMCP add-in: local bridge between the fusion-cam MCP server and Fusion's API.

Socket thread (bridge.tcp_server) -> MainThreadQueue -> app.fireCustomEvent ->
_DispatchHandler.notify (main thread) -> ops.handle.

Listens on 127.0.0.1:27190 (FUSION_MCP_PORT overrides). Requests need the token in
%APPDATA%\\FusionMCP\\token. Logs go to Fusion's Text Commands window and
%APPDATA%\\FusionMCP\\addin.log.
"""

import importlib
import os
import sys
import time
import traceback

import adsk.core

from .bridge import mainthread, protocol, tcp_server
from . import ops
from .ops import breadcrumb, state

EVENT_ID = "FusionMCP_Dispatch"

_app = adsk.core.Application.get()
_handlers = []
_queue: mainthread.MainThreadQueue | None = None
_server = None
_event = None


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} [FusionMCP] {msg}"
    try:
        _app.log(line)          # Text Commands window
    except Exception:
        print(line)
    try:
        with open(os.path.join(protocol.state_dir(), "addin.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _reload(args: dict):
    """Re-import ops modules (not state/breadcrumb) and clear the execute_python namespace."""
    names = sorted(n for n in sys.modules
                   if n.startswith(ops.__name__ + ".") and not n.endswith((".state", ".breadcrumb")))
    for n in names:
        importlib.reload(sys.modules[n])
    importlib.reload(ops)
    state.namespace.clear()
    short = [n[len(ops.__name__) - len("ops"):] for n in names + [ops.__name__]]   # Fusion mangles the package name
    return {"reloaded": short, "ops": sorted(ops.OPS) + ["reload", "hello"]}


def _handle(req: dict):
    if req.get("op") == "reload":
        return _reload(req.get("args") or {})
    return ops.handle(req)


class _DispatchHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            _queue.run(args.additionalInfo, _handle)
        except Exception:
            log("dispatch failure:\n" + traceback.format_exc())


def _fire(key: str) -> None:
    _app.fireCustomEvent(EVENT_ID, key)


def run(context):
    global _queue, _server, _event
    try:
        state.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        state.last_crash = breadcrumb.startup()
        if state.last_crash:
            log(f"WARNING: Fusion crashed during '{state.last_crash['op']}' "
                f"({state.last_crash['summary']}) at {state.last_crash['when']}")
        _event = _app.registerCustomEvent(EVENT_ID)
        h = _DispatchHandler()
        _event.add(h)
        _handlers.append(h)
        _queue = mainthread.MainThreadQueue(_fire)
        token = protocol.ensure_token()
        _server = tcp_server.start(_queue.submit, token, log=log)
        log(f"listening on {protocol.HOST}:{_server.server_address[1]} "
            f"(add-in {tcp_server.ADDIN_VERSION}, Fusion {_app.version}, Python {sys.version.split()[0]})")
    except Exception:
        log("failed to start:\n" + traceback.format_exc())
        stop(context)


def stop(context):
    global _server, _event
    try:
        tcp_server.stop(_server)
    except Exception:
        log("socket stop failure:\n" + traceback.format_exc())
    _server = None
    try:
        if _event is not None:
            for h in _handlers:
                _event.remove(h)
        _app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _handlers.clear()
    _event = None
    log("stopped")
