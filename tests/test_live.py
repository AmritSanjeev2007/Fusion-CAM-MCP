"""Live checks against a running Fusion with the FusionMCP add-in started.

Run: .venv\\Scripts\\python.exe -m pytest -m fusion -v
Read-only apart from execute_python variables; never opens, edits or closes documents.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.bridge_client import BridgeClient, BridgeError  # noqa: E402

pytestmark = pytest.mark.fusion


@pytest.fixture(scope="module")
def client():
    c = BridgeClient()
    yield c
    c.close()


def test_hello(client):
    h = client.call("hello", timeout=5)
    assert h["addin_version"] and h["port"] == 27190


def test_status_main_thread(client):
    st = client.call("status", timeout=10)
    assert st["fusion_version"].startswith("2706") or st["fusion_version"]
    assert st["python"].startswith("3.")
    print(st)


def test_exec_persistent_namespace(client):
    assert client.call("exec", {"code": "x = 41"}) == "(ok, no output)"
    assert client.call("exec", {"code": "x + 1"}) == "42"
    assert client.call("exec", {"code": "print(app.version)"}).startswith("2")


def test_exec_error_shows_user_line(client):
    with pytest.raises(BridgeError) as e:
        client.call("exec", {"code": "a = 1\nb = a / 0"})
    assert "line 2" in str(e.value) and "ZeroDivisionError" in str(e.value)


def test_main_thread_identity(client):
    out = client.call("exec", {"code": "import threading; threading.current_thread() is threading.main_thread()"})
    assert out == "True"


def test_reload_clears_namespace(client):
    client.call("exec", {"code": "keep_me = 1"})
    r = client.call("reload", timeout=30)
    assert "exec" in r["ops"]
    with pytest.raises(BridgeError):
        client.call("exec", {"code": "keep_me"})
