"""Bridge tests without Fusion: real sockets, a fake 'main thread', the real client."""

import os
import socket
import sys
import threading
import time
import types
import queue

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "addin"))
sys.path.insert(0, ROOT)

# stub adsk so the add-in package imports outside Fusion (ops.core only touches it at call time)
for name in ("adsk", "adsk.core", "adsk.fusion", "adsk.cam"):
    sys.modules.setdefault(name, types.ModuleType(name))

from FusionMCP.bridge import mainthread, protocol, tcp_server  # noqa: E402
from FusionMCP.ops import breadcrumb  # noqa: E402
from src.bridge_client import BridgeClient, BridgeError  # noqa: E402

TOKEN = "t" * 48


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeFusion:
    """Stands in for Fusion: fireCustomEvent puts the key on a queue; one 'main thread' drains it."""

    def __init__(self, handle):
        self.keys = queue.Queue()
        self.q = mainthread.MainThreadQueue(self.keys.put)
        self.handle = handle
        self.paused = threading.Event()
        self.ran = []
        self._stop = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self._stop:
            try:
                key = self.keys.get(timeout=0.05)
            except queue.Empty:
                continue
            while self.paused.is_set():
                time.sleep(0.01)
            self.q.run(key, self._wrapped)

    def _wrapped(self, req):
        self.ran.append(req["op"])
        return self.handle(req)

    def close(self):
        self._stop = True


def default_handle(req):
    op = req["op"]
    if op == "echo":
        return req["args"]
    if op == "boom":
        raise ValueError("kaboom")
    if op == "slow":
        time.sleep(req["args"]["s"])
        return "slow done"
    raise KeyError(op)


@pytest.fixture
def bridge():
    fake = FakeFusion(default_handle)
    port = free_port()
    srv = tcp_server.start(fake.q.submit, TOKEN, port=port, log=lambda m: None)
    client = BridgeClient(port=port, token=TOKEN)
    yield fake, srv, client, port
    client.close()
    tcp_server.stop(srv)
    fake.close()


def test_hello_answers_on_socket_thread_even_when_main_thread_blocked(bridge):
    fake, srv, client, _ = bridge
    fake.paused.set()
    hello = client.call("hello", timeout=2)
    assert hello["addin_version"] == tcp_server.ADDIN_VERSION
    assert hello["port"] == srv.server_address[1]
    fake.paused.clear()


def test_round_trip_and_unicode(bridge):
    _, _, client, _ = bridge
    assert client.call("echo", {"x": 1, "s": "ø 50×40 mm"}) == {"x": 1, "s": "ø 50×40 mm"}
    # several requests on one connection
    for i in range(20):
        assert client.call("echo", {"i": i}) == {"i": i}


def test_bad_token_rejected(bridge):
    _, _, _, port = bridge
    bad = BridgeClient(port=port, token="wrong")
    with pytest.raises(BridgeError) as e:
        bad.call("echo", {})
    assert e.value.kind == "auth"
    bad.close()


def test_exception_reported_with_traceback(bridge):
    _, _, client, _ = bridge
    with pytest.raises(BridgeError) as e:
        client.call("boom")
    assert e.value.kind == "exception" and "kaboom" in str(e.value)
    assert "ValueError" in (e.value.traceback or "")
    assert client.call("echo", {"ok": True}) == {"ok": True}      # connection still usable


def test_timeout_before_pickup_is_cancelled_and_never_runs(bridge):
    fake, _, client, _ = bridge
    fake.paused.set()
    with pytest.raises(BridgeError) as e:
        client.call("echo", {"never": 1}, timeout=0.3)
    assert e.value.kind == "timeout" and "did NOT run" in str(e.value)
    fake.paused.clear()
    time.sleep(0.2)
    assert "echo" not in fake.ran


def test_timeout_while_running_says_it_may_finish(bridge):
    _, _, client, _ = bridge
    with pytest.raises(BridgeError) as e:
        client.call("slow", {"s": 0.8}, timeout=0.2)
    assert e.value.kind == "timeout" and "may still finish" in str(e.value)
    time.sleep(0.8)
    assert client.call("echo", {"after": 1}) == {"after": 1}


def test_reconnects_after_addin_restart(bridge):
    fake, srv, client, port = bridge
    assert client.call("echo", {"a": 1}) == {"a": 1}
    tcp_server.stop(srv)
    srv2 = tcp_server.start(fake.q.submit, TOKEN, port=port, log=lambda m: None)
    try:
        assert client.call("echo", {"b": 2}) == {"b": 2}
    finally:
        tcp_server.stop(srv2)


def test_not_running_message():
    client = BridgeClient(port=free_port(), token=TOKEN)
    with pytest.raises(BridgeError) as e:
        client.call("hello", timeout=1)
    assert e.value.kind == "not_running" and "Shift+S" in str(e.value)


def test_invalid_json_line(bridge):
    _, _, _, port = bridge
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(b"not json\n")
        reply = protocol.decode(s.makefile("rb").readline())
    assert reply["ok"] is False and reply["kind"] == "bad_request"


def test_breadcrumb_detects_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert breadcrumb.startup() is None                      # first start: nothing
    breadcrumb.begin("exec", "r1", "x = 1")
    breadcrumb.end("exec", "r1", True)
    breadcrumb.begin("exec", "r2", "cam.generateToolpath(op)")
    # Fusion "dies" here: no END for r2
    crash = breadcrumb.startup()
    assert crash["op"] == "exec" and crash["id"] == "r2" and "generateToolpath" in crash["summary"]
    assert breadcrumb.startup() is None                      # reported once, then cleared by STARTUP


def test_token_created_once(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert protocol.read_token() is None
    t1 = protocol.ensure_token()
    assert len(t1) == 48 and protocol.ensure_token() == t1 == protocol.read_token()
