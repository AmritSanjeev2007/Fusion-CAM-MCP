"""Live checks against a running Fusion with the FusionMCP add-in started.

Run: .venv\\Scripts\\python.exe -m pytest -m fusion -v
The Phase 1 checks are read-only apart from execute_python variables. The Phase 2 checks import
test_output/MCP_Test_Plate.step into one scratch document and close it again; they never touch other documents.
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


# ---- Phase 2: STEP import (creates one scratch document and closes it again)
PLATE_STEP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "test_output", "MCP_Test_Plate.step")


@pytest.fixture(scope="module")
def plate(client):
    if not os.path.exists(PLATE_STEP):
        pytest.skip(f"export the Inventor test plate to {PLATE_STEP} first")
    if not client.call("status", timeout=10)["idle"]:
        pytest.skip("Fusion is not idle (a command dialog is open)")
    r = client.call("import_step", {"path": PLATE_STEP, "expected_size_mm": [80, 50, 10],
                                    "expected_volume_mm3": 37395}, timeout=120)
    yield r
    try:
        client.call("close_scratch", {"scratch_id": r["scratch_id"]})
    except BridgeError:
        pass                                # already closed by the last test


def test_close_scratch_refuses_user_documents(client):
    """Runs before the plate is imported, so a name match can only hit the user's documents."""
    st = client.call("status", timeout=10)
    user = [d["name"] for d in st["documents"] if not d["scratch"]]
    if not user:
        pytest.skip("no user document open to test the refusal on")
    with pytest.raises(BridgeError) as e:
        client.call("close_scratch", {"name": user[0]})
    assert "Refusing" in str(e.value)
    with pytest.raises(BridgeError):
        client.call("close_scratch", {"scratch_id": "no-such-id"})


def test_import_step_test_plate(plate):
    assert plate["body_count"] == 1
    assert plate["check"]["ok"], plate["check"]


def test_inspect_test_plate(client, plate):
    r = client.call("inspect")
    assert r["document"] == plate["document"] and r["scratch"]
    assert r["top_face_z_mm"] == pytest.approx(10.0, abs=1e-3)
    assert r["hole_count"] == 3
    big = r["holes"][0]
    assert big["diameter_mm"] == pytest.approx(16.0, abs=1e-3) and big["count"] == 1 and big["through"]
    assert r["holes"][1]["count"] == 2


def test_screenshot(client, plate):
    r = client.call("screenshot", {"view": "iso", "width": 800, "height": 500})
    assert os.path.exists(r["path"]) and r["bytes"] > 1000


def test_close_by_scratch_id_leaves_namesakes(client, plate):
    """Imported documents are often named 'Untitled' like the user's; scratch_id picks only ours."""
    before = [d for d in client.call("status", timeout=10)["documents"] if not d["scratch"]]
    r = client.call("close_scratch", {"scratch_id": plate["scratch_id"]})
    assert r["closed"] == [plate["document"]]
    after = [d for d in client.call("status", timeout=10)["documents"] if not d["scratch"]]
    assert [d["name"] for d in after] == [d["name"] for d in before]
