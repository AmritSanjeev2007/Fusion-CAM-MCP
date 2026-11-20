"""Loopback TCP server for the add-in. Stdlib only; never touches adsk.*.

Each connection may send many requests, one JSON line each. Requests are answered in order.
Most ops are handed to `submit(request, timeout) -> response`, which the add-in implements by
queueing the request onto Fusion's main thread. A few ops are answered right here on the
socket thread because they must work even when the main thread is blocked:

  hello  -> add-in version, pid, port, uptime, pending count, last request info
"""

import hmac
import os
import socketserver
import threading
import time
import traceback

from . import protocol

ADDIN_VERSION = "1.0.0"
SOCKET_OPS = {"hello"}


class _Handler(socketserver.StreamRequestHandler):
    timeout = None       # connections stay open; each request has its own timeout

    def handle(self):
        srv: "BridgeServer" = self.server          # type: ignore[assignment]
        while True:
            try:
                line = self.rfile.readline(protocol.MAX_LINE + 1)
            except (ConnectionError, OSError):
                return
            if not line:
                return
            if len(line) > protocol.MAX_LINE:
                self._send(protocol.error(None, "bad_request", "message too large"))
                return
            resp = srv.process_line(line)
            if not self._send(resp):
                return

    def _send(self, obj) -> bool:
        try:
            self.wfile.write(protocol.encode(obj))
            self.wfile.flush()
            return True
        except (ConnectionError, OSError):
            return False


class BridgeServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False          # Windows: True would allow port hijacking

    def __init__(self, submit, token: str, port: int | None = None, log=print):
        self._submit = submit
        self._token = token
        self._log = log
        self.started = time.time()
        self.last_request = None          # (op, id, time) of the most recent main-thread request
        self.pending = 0
        self._lock = threading.Lock()
        super().__init__((protocol.HOST, port or protocol.port()), _Handler)

    # -- request processing (socket thread) --
    def process_line(self, line: bytes) -> dict:
        try:
            req = protocol.decode(line)
        except Exception as e:
            return protocol.error(None, "bad_request", f"invalid JSON: {e}")
        rid = req.get("id")
        tok = req.get("token")
        if not isinstance(tok, str) or not hmac.compare_digest(tok, self._token):
            return protocol.error(rid, "auth", "bad or missing token")
        op = req.get("op")
        if not isinstance(op, str):
            return protocol.error(rid, "bad_request", "missing op")
        if op in SOCKET_OPS:
            return protocol.ok(rid, self._hello())
        try:
            timeout = float(req.get("timeout") or protocol.DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = protocol.DEFAULT_TIMEOUT
        with self._lock:
            self.pending += 1
            self.last_request = {"op": op, "id": rid, "at": time.strftime("%H:%M:%S")}
        try:
            return self._submit(req, timeout)
        except Exception as e:
            return protocol.error(rid, "exception", f"bridge failure: {e}", traceback.format_exc())
        finally:
            with self._lock:
                self.pending -= 1

    def _hello(self) -> dict:
        return {"addin_version": ADDIN_VERSION, "pid": os.getpid(), "port": self.server_address[1],
                "uptime_s": round(time.time() - self.started, 1), "pending": self.pending,
                "last_request": self.last_request}


def start(submit, token: str, port: int | None = None, log=print) -> BridgeServer:
    srv = BridgeServer(submit, token, port, log)
    th = threading.Thread(target=srv.serve_forever, name="FusionMCP-socket", daemon=True)
    th.start()
    srv.thread = th
    return srv


def stop(srv: BridgeServer | None):
    if srv is None:
        return
    srv.shutdown()
    srv.server_close()
    th = getattr(srv, "thread", None)
    if th is not None:
        th.join(5)
