"""Unit tests for the CAM helpers (no Fusion): units, WCS/stock, params, tools, posting, polling,
and crash breadcrumbs with steps."""

import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "addin"))
sys.path.insert(0, ROOT)

for name in ("adsk", "adsk.core", "adsk.fusion", "adsk.cam"):
    sys.modules.setdefault(name, types.ModuleType(name))

from FusionMCP.ops import breadcrumb, camcalc as c, trace  # noqa: E402
from src.polling import poll_generation  # noqa: E402

PLATE = ((-40.0, -25.0, 0.0), (40.0, 25.0, 10.0))


# ---- WCS and stock
def test_wcs_params_stock_and_model_points():
    assert c.wcs_params("top-centre") == {"wcs_origin_mode": "'stockPoint'", "wcs_origin_boxPoint": "'top center'"}
    assert c.wcs_params("top-center")["wcs_origin_boxPoint"] == "'top center'"          # US spelling ok
    assert c.wcs_params("model-top-corner-1") == {"wcs_origin_mode": "'modelPoint'", "wcs_origin_boxPoint": "'top 1'"}
    assert c.wcs_params("model-origin") == {"wcs_origin_mode": "'modelOrigin'"}
    with pytest.raises(ValueError):
        c.wcs_params("left-ish")


def test_stock_params_number_and_dict():
    p = c.stock_params(1)
    assert p["job_stockMode"] == "'default'" and p["job_stockOffsetMode"] == "'simple'"
    assert (p["job_stockOffsetSides"], p["job_stockOffsetTop"], p["job_stockOffsetBottom"]) == ("1 mm", "1 mm", "0 mm")
    p = c.stock_params({"sides": 2.5, "top": 0.5, "bottom": 1})
    assert (p["job_stockOffsetSides"], p["job_stockOffsetTop"], p["job_stockOffsetBottom"]) == ("2.5 mm", "0.5 mm", "1 mm")
    for bad in (-1, {"side": 1}, "1mm", {"top": -0.1}):
        with pytest.raises(ValueError):
            c.stock_params(bad)


def test_stock_box_matches_fusion_live_readback():
    # live: 1 mm offset on the 80x50x10 plate -> stock 82 x 52 x 11 (stockZ -11..0 in a top-centre WCS)
    box = c.stock_box(PLATE, 1)
    assert box == ((-41.0, -26.0, 0.0), (41.0, 26.0, 11.0))


def test_wcs_origin_from_box_and_corner_numbering():
    stock = c.stock_box(PLATE, 1)
    assert c.box_point(stock, "top-centre") == (0.0, 0.0, 11.0)
    assert c.box_point(PLATE, "model-top-centre") == (0.0, 0.0, 10.0)
    assert c.box_point(stock, "centre") == (0.0, 0.0, 5.5)
    # corner numbering read live: 1=-X-Y, 2=+X-Y, 3=-X+Y, 4=+X+Y
    assert c.box_point(stock, "top-corner-1") == (-41.0, -26.0, 11.0)
    assert c.box_point(stock, "top-corner-2") == (41.0, -26.0, 11.0)
    assert c.box_point(stock, "top-corner-3") == (-41.0, 26.0, 11.0)
    assert c.box_point(stock, "bottom-corner-4") == (41.0, 26.0, 0.0)


# ---- parameters
def test_to_expression_and_parse_params():
    assert c.to_expression(True) == "true" and c.to_expression(False) == "false"
    assert c.to_expression(3) == "3" and c.to_expression(2.5) == "2.5"
    assert c.to_expression("-0.5 mm") == "-0.5 mm"
    p = c.parse_params('{"bottomHeight_offset": "-0.5 mm", "doMultipleDepths": true, "numberOfStepdowns": 3}')
    assert p == {"bottomHeight_offset": "-0.5 mm", "doMultipleDepths": "true", "numberOfStepdowns": "3"}
    assert c.parse_params(None) == {} and c.parse_params("") == {}
    for bad in ("{not json", "[1, 2]", {"bad name": 1}, {"x": None}, {"x": ""}, {"x": float("nan")}):
        with pytest.raises(ValueError):
            c.parse_params(bad)


# ---- machining time
def test_time_report_converts_cm_to_mm():
    raw = {"machiningTime": 151.23, "feedDistance": 129.4, "rapidDistance": 70.29, "toolChangeCount": 3,
           "totalFeedTime": 104.8, "totalRapidTime": 1.4, "totalToolChangeTime": 45.0}
    r = c.time_report(raw)
    assert r["total_s"] == 151.2 and r["feed_distance_mm"] == 1294.0 and r["rapid_distance_mm"] == 702.9
    assert r["tool_changes"] == 3 and r["tool_change_time_s"] == 45.0 and r["total_min_s"] == "2:31.2"
    assert c.mm_min_to_cm_s(5000) == pytest.approx(8.3333, rel=1e-4)


# ---- tools
SAMPLE = {"guid": "g-10", "type": "flat end mill", "description": "10mm Flat Endmill", "unit": "millimeters",
          "geometry": {"DC": 10.0, "NOF": 3.0, "LCF": 22.0},
          "post-process": {"number": 0.0, "length-offset": 0.0, "diameter-offset": 0.0, "comment": ""}}


def test_tool_summary_and_filter():
    inch = dict(SAMPLE, guid="g-inch", unit="inches", geometry={"DC": 0.25})
    drill = dict(SAMPLE, guid="g-d", type="drill", description="", geometry={"DC": 4.9, "SIG": 118.0})
    s = [c.tool_summary(t, "lib") for t in (SAMPLE, inch, drill)]
    assert s[0]["diameter_mm"] == 10.0 and s[1]["diameter_mm"] == 6.35 and s[2]["tip_angle_deg"] == 118.0
    assert [t["id"] for t in c.filter_tools(s, "drill")] == ["g-d"]
    assert [t["id"] for t in c.filter_tools(s, None, 6, 10)] == ["g-inch", "g-10"]
    assert [t["id"] for t in c.filter_tools(s, text="flat")] == ["g-inch", "g-10"]


def test_prepare_tool_json_sets_number_and_offsets_without_mutating():
    t = c.prepare_tool_json(SAMPLE, 2)
    assert t["post-process"]["number"] == 2.0
    assert t["post-process"]["length-offset"] == 2.0 and t["post-process"]["diameter-offset"] == 2.0
    assert SAMPLE["post-process"]["number"] == 0.0                     # original untouched
    keep = c.prepare_tool_json(dict(SAMPLE, **{"post-process": {"number": 0, "length-offset": 7}}), 3)
    assert keep["post-process"]["length-offset"] == 7                  # explicit offsets are kept


def test_pick_tool_number():
    used = {1: "face", 2: "em10"}
    assert c.pick_tool_number(used, "em10") == 2                        # same tool -> same number
    assert c.pick_tool_number(used, "drill") == 3                       # next free
    assert c.pick_tool_number({}, "x") == 1
    assert c.pick_tool_number(used, "drill", 7) == 7
    with pytest.raises(ValueError):
        c.pick_tool_number(used, "drill", 1)                            # taken by another tool


# ---- posting
def test_post_url_filename_groups_and_restriction():
    assert c.post_url("fanuc.cps") == "system://fanuc.cps"
    assert c.post_url("fanuc") == "system://fanuc.cps"
    assert c.post_url("system://haas.cps") == "system://haas.cps"
    assert c.safe_filename("Setup 1/plate: v2") == "Setup_1_plate_v2"
    assert c.tool_groups([("Face1", 1), ("Contour", 2), ("Contour2", 2), ("Drill", 3), ("Face2", 1)]) == \
        [["Face1"], ["Contour", "Contour2"], ["Drill"], ["Face2"]]
    msg = ("When using Fusion for Personal Use, you cannot post process operations that contain tool changes "
           "in a single NC program.")
    assert c.is_tool_change_restriction(msg)
    assert not c.is_tool_change_restriction("post processor not found")


# ---- polling (fake client)
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_poll_until_completed():
    clock = FakeClock()
    answers = iter([{"completed": False}, {"completed": False}, {"completed": True, "all_valid": True}])
    calls = []

    def call(op, args, timeout):
        calls.append((op, args["job"], timeout))
        return next(answers)
    r = poll_generation(call, "j1", timeout_s=10, interval_s=0.5, sleep=clock.sleep, clock=clock)
    assert r["completed"] and r["polls"] == 3 and "timed_out" not in r
    assert calls[0] == ("cam_generate_poll", "j1", 15)
    assert clock.t == pytest.approx(1.0)


def test_poll_times_out_but_keeps_job():
    clock = FakeClock()
    r = poll_generation(lambda *a: {"completed": False, "done": 1, "total": 4}, "j2", timeout_s=2,
                        interval_s=0.5, sleep=clock.sleep, clock=clock)
    assert r["timed_out"] and "j2" in r["hint"] and r["polls"] == 5
    with pytest.raises(ValueError):
        poll_generation(lambda *a: {}, "j", timeout_s=0)


# ---- breadcrumbs with steps
def test_breadcrumb_reports_crashed_cam_step(tmp_path, monkeypatch):
    monkeypatch.setattr(breadcrumb, "_path", lambda: str(tmp_path / "breadcrumb.log"))
    breadcrumb.startup()
    breadcrumb.begin("cam_status", "r1")
    trace.current.update(op="cam_status", id="r1")
    trace.step("checkToolpath Face1")
    breadcrumb.end("cam_status", "r1", True)
    breadcrumb.begin("cam_generate_start", "r2", "targets=None")
    trace.current.update(op="cam_generate_start", id="r2")
    trace.step("generateToolpath Face1")
    trace.step("generateToolpath 2D Contour1")
    crashed = breadcrumb.startup()                                      # Fusion "restarted" mid-call
    assert crashed["op"] == "cam_generate_start" and crashed["id"] == "r2"
    assert crashed["step"] == "generateToolpath 2D Contour1"
    assert breadcrumb.startup() is None                                 # reported once


def test_breadcrumb_clean_run_has_no_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(breadcrumb, "_path", lambda: str(tmp_path / "breadcrumb.log"))
    breadcrumb.startup()
    breadcrumb.begin("cam_post", "r9")
    trace.current.update(op="cam_post", id="r9")
    trace.step("postProcess plate")
    breadcrumb.end("cam_post", "r9", False)
    assert breadcrumb.startup() is None
