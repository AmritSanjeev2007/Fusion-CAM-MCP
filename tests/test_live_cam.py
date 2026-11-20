"""Live Phase 3 check: the Inventor test plate gets a facing, 2D-contour and drilling job that
generates, passes checkToolpath, reports a cycle time and posts Fanuc G-code.

Run: .venv\\Scripts\\python.exe -m pytest -m fusion tests/test_live_cam.py -v
Imports test_output/MCP_Test_Plate.step into one scratch document and closes it at the end;
G-code goes to test_output/nc_live. Never touches other documents.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.bridge_client import BridgeClient, BridgeError  # noqa: E402
from src.polling import poll_generation  # noqa: E402

pytestmark = pytest.mark.fusion

PLATE_STEP = os.path.join(ROOT, "test_output", "MCP_Test_Plate.step")
NC_DIR = os.path.join(ROOT, "test_output", "nc_live")
FACE_MILL_50 = "49d0ef72-1d9e-4d80-aa4c-2fd9384de358"      # Tutorial Tools (Metric)
FLAT_10 = "593e3a17-2722-4974-b6d6-46521c351aa6"           # Milling Tools (Metric)
DRILL_16 = "4e82fd33-fab1-4910-b0b1-768c41323e30"          # Hole Making Tools (Metric)
DRILL_4_9 = "e4b5958e-d3b7-4249-a084-d9cca4830757"


@pytest.fixture(scope="module")
def job():
    c = BridgeClient()
    if not os.path.exists(PLATE_STEP):
        pytest.skip(f"export the Inventor test plate to {PLATE_STEP} first")
    st = c.call("status", timeout=10)
    if not st["idle"]:
        pytest.skip("Fusion is not idle")
    if st.get("last_crash"):
        pytest.skip(f"Fusion crashed last time during {st['last_crash']}; investigate before rerunning")
    imp = c.call("import_step", {"path": PLATE_STEP, "expected_size_mm": [80, 50, 10],
                                 "expected_volume_mm3": 37395}, timeout=120)
    ctx = {"client": c, "import": imp}
    yield ctx
    try:
        c.call("close_scratch", {"scratch_id": imp["scratch_id"]})
    except BridgeError:
        pass
    c.close()


def test_setup(job):
    r = job["client"].call("cam_create_setup", {"stock_offset_mm": 1, "wcs": "top-centre"})
    assert r["stock_box_world_mm"]["size"] == [82.0, 52.0, 11.0]
    assert r["stock_in_wcs_mm"]["max"][2] == 0.0 and r["stock_in_wcs_mm"]["min"][2] == -11.0
    assert r["wcs_origin_world_mm"] == [0.0, 0.0, 11.0]
    assert "parameter_errors" not in r


def test_strategies_and_tools(job):
    c = job["client"]
    s = c.call("cam_list_strategies")
    names = [x["name"] for x in s["strategies"]]
    assert {"face", "contour2d", "drill"} <= set(names) and s["count"] >= 50
    t = c.call("cam_list_tools", {"library": "Hole Making Tools (Metric)", "type": "drill",
                                  "diameter_min_mm": 4.9, "diameter_max_mm": 4.9})
    assert [x["id"] for x in t["tools"]] == [DRILL_4_9]


def test_operations(job):
    c = job["client"]
    ops = [
        {"strategy": "face", "tool_id": FACE_MILL_50},
        {"strategy": "contour2d", "tool_id": FLAT_10, "geometry": {"contour": "outline", "side": "outside"},
         "params": {"bottomHeight_offset": "-0.5 mm", "doMultipleDepths": True, "maximumStepdown": "4 mm"}},
        {"strategy": "drill", "tool_id": DRILL_16, "geometry": {"holes": {"diameter_mm": 16}},
         "params": {"drillTipThroughBottom": True, "breakThroughDepth": "1 mm"}},
        {"strategy": "drill", "tool_id": DRILL_4_9, "geometry": {"holes": {"diameter_mm": 4.917}},
         "params": {"drillTipThroughBottom": True, "breakThroughDepth": "1 mm", "cycleType": "'deep-drilling'",
                    "incrementalDepth": "3 mm"}},
    ]
    numbers = []
    for spec in ops:
        r = c.call("cam_add_operation", spec)
        assert "parameter_errors" not in r, r
        numbers.append(r["tool"]["number"])
    assert numbers == [1, 2, 3, 4]


def test_generate_and_check(job):
    c = job["client"]
    start = c.call("cam_generate_start", {})
    r = poll_generation(lambda op, a, t: c.call(op, a, timeout=t), start["job"], timeout_s=300)
    assert r["completed"] and r["all_valid"], r
    assert all(o["check_ok"] for o in r["operations"])


def test_machining_time(job):
    r = job["client"].call("cam_machining_time", {"rapid_mm_min": 5000, "tool_change_s": 15})
    assert r["tool_changes"] == 3 and r["total_s"] > 60
    assert len(r["per_operation"]) == 4
    job["time"] = r


def test_post_fanuc(job):
    r = job["client"].call("cam_post", {"post": "fanuc.cps", "output_dir": NC_DIR, "filename": "plate"},
                           timeout=180)
    files = r["files"]
    assert files and all(os.path.exists(f["path"]) for f in files)
    text = "\n".join(open(f["path"], encoding="utf-8").read() for f in files)
    for t in ("T1 M06", "T2 M06", "T3 M06", "T4 M06", "G43", "M30"):
        assert t in text, t
    assert "G81" in text and "G83" in text          # plain drill for D16, peck drill for D4.9
    assert "H00" not in text                         # length offsets follow tool numbers
