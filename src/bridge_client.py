"""Client for the FusionMCP add-in's loopback socket.

The wire protocol lives in addin/FusionMCP/bridge/protocol.py (stdlib only) and is loaded
by path, so the add-in and the server can never disagree about it.
"""

import importlib.util
import itertools
import os
import socket
import threading

_PROTO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "addin", "FusionMCP", "bridge", "protocol.py")
_spec = importlib.util.spec_from_file_location("fusionmcp_protocol", _PROTO_PATH)
protocol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(protocol)


class BridgeError(RuntimeError):
    def __init__(self, kind: str, message: str, traceback: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.traceback = traceback


class BridgeClient:
    """One persistent connection; reconnects once per call if the add-in restarted."""

    def __init__(self, host: str = protocol.HOST, port: int | None = None, token: str | None = None):
        self.host = host
        self.port = port or protocol.port()
        self._token = token
        self._sock: socket.socket | None = None
        self._file = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def _connect(self, timeout: float) -> None:
        self.close()
        tok = self._token or protocol.read_token()
        if not tok:
            raise BridgeError("not_running", "No token at %APPDATA%\\FusionMCP\\token: the FusionMCP add-in "
                                             "has never run. In Fusion: Shift+S > Add-Ins > FusionMCP > Run.")
        self._token_in_use = tok
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=min(timeout, 5))
        except OSError as e:
            raise BridgeError("not_running",
                              f"Cannot reach the FusionMCP add-in on {self.host}:{self.port} ({e}). Is Fusion "
                              "running with the add-in started (Shift+S > Add-Ins > FusionMCP > Run)?") from None
        self._file = self._sock.makefile("rb")

    def close(self) -> None:
        for obj in (self._file, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self._sock = self._file = None

    def call(self, op: str, args: dict | None = None, timeout: float = protocol.DEFAULT_TIMEOUT):
        """Send one request and return its result, or raise BridgeError."""
        with self._lock:
            last_exc = None
            for attempt in (1, 2):
                fresh = False
                try:
                    if self._sock is None:
                        self._connect(timeout)
                        fresh = True
                    rid = f"r{next(self._ids)}"
                    req = {"id": rid, "token": self._token_in_use, "op": op, "args": args or {},
                           "timeout": timeout}
                    self._sock.settimeout(timeout + 10)        # the add-in answers timeouts itself
                    self._sock.sendall(protocol.encode(req))
                    line = self._file.readline(protocol.MAX_LINE + 1)
                    if not line:
                        raise ConnectionError("add-in closed the connection")
                    resp = protocol.decode(line)
                    break
                except BridgeError:
                    raise
                except socket.timeout:
                    self.close()
                    raise BridgeError("timeout", f"No reply from the add-in within {timeout + 10:g}s.") from None
                except (ConnectionError, OSError, ValueError) as e:
                    # A reused connection that fails means the add-in restarted and closed it, so the
                    # request never reached a live server: reconnect and retry once. A failure on a
                    # fresh connection is reported, never retried (it might have run).
                    self.close()
                    if fresh or attempt == 2:
                        raise BridgeError("not_running", f"Connection to the add-in failed: {e}") from None
            if resp.get("ok"):
                return resp.get("result")
            if resp.get("kind") == "auth":
                self._token = None           # token may have been regenerated; re-read next time
                self.close()
            raise BridgeError(resp.get("kind", "exception"), resp.get("error", "unknown error"),
                              resp.get("traceback"))
