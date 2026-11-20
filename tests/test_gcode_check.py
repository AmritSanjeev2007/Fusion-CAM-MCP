"""Phase 4: G-code checker tests (no Fusion). Uses the Fanuc programs Fusion posted for the
Inventor test plate in Phase 3 (tests/data/plate) plus small synthetic programs."""

import importlib.util
import os
import shutil

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("gcode_check", os.path.join(ROOT, "external", "gcode_check.py"))
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)

DATA = os.path.join(ROOT, "tests", "data", "plate")
PLATE = [os.path.join(DATA, f) for f in sorted(os.listdir(DATA))]
STOCK = (-41, -26, -11, 41, 26, 0)          # 82 x 52 x 11 stock, top-centre WCS (from cam_status)
PART_VOLUME = 37395.0


def codes(r, severity=None):
    return [p["code"] for p in r["problems"] if severity is None or p["severity"] == severity]


def write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


HEADER = "%\nO1001\n(T2 D=10. CR=0. - ZMIN=-5. - FLAT END MILL)\nN10 G90 G94 G17 G49 G40 G80\nN15 G21\n"


# ---- the real posted job
def test_posted_plate_job_is_clean_and_removal_matches_cad():
    r = gc.check(PLATE, STOCK, expected_tools=[1, 2, 3, 4], res=0.5)
    assert r["ok"] and r["counts"]["error"] == 0 and r["counts"]["warning"] == 0, r["problems"]
    assert set(codes(r)) == {"BELOW_STOCK"}                       # through-cuts only (info)
    assert r["stock_volume_mm3"] == 46904.0
    # remaining stock after face + contour + drills = the CAD part within 0.2 %
    assert abs(r["remaining_volume_mm3"] - PART_VOLUME) / PART_VOLUME < 0.002
    assert r["per_tool"]["T1"]["removed_mm3"] == pytest.approx(82 * 52 * 1, rel=1e-3)   # 1 mm facing
    assert r["tools"]["T3"]["profile"] == "drill 118 deg" and r["tools"]["T3"]["diameter_mm"] == 16.0


def test_done_criterion_rapid_through_stock_flagged_at_right_line(tmp_path):
    """Plan Phase 4 'done when': a deliberately bad program (a rapid through the stock) is flagged
    at the right line."""
    src = os.path.join(DATA, "mcp_plate_02_T2.nc")
    lines = open(src, encoding="utf-8").read().splitlines()
    i = lines.index("N65 G01 Z5. F1000.")
    lines[i:i] = ["N62 G00 Z-5.", "N63 G00 X36. Y-33."]   # plunge outside, then rapid across the plate
    bad = write(tmp_path, "bad.nc", "\n".join(lines) + "\n")
    r = gc.check([os.path.join(DATA, "mcp_plate_01_T1.nc"), bad], STOCK)
    assert not r["ok"]
    hits = [p for p in r["problems"] if p["code"] == "RAPID_INTO_STOCK"]
    assert len(hits) == 1
    assert hits[0]["file"] == "bad.nc" and hits[0]["line"] == i + 2 and hits[0]["n"] == 63
    assert "N63 G00 X36. Y-33." in hits[0]["text"]


def test_rapid_into_stock_when_tool_starts_low(tmp_path):
    p = write(tmp_path, "a.nc", HEADER + "N20 T2 M06\nN25 S5000 M03\nN30 G00 X0. Y0. Z15.\nN35 G00 Z-2.\nN40 M30\n%\n")
    r = gc.check(p, STOCK)
    hit = [x for x in r["problems"] if x["code"] == "RAPID_INTO_STOCK"]
    assert hit and hit[0]["line"] == 9 and hit[0]["n"] == 35


def test_rapid_below_top_through_air_is_a_warning(tmp_path):
    body = (HEADER + "N20 T2 M06\nN25 S5000 M03\nN30 G00 X0. Y0. Z5.\nN35 G01 Z-3. F300.\n"
            "N40 G00 Z-1.\nN45 G00 Z15.\nN50 M30\n%\n")
    r = gc.check(write(tmp_path, "b.nc", body), STOCK)
    assert "RAPID_INTO_STOCK" not in codes(r)
    w = [x for x in r["problems"] if x["code"] == "RAPID_BELOW_TOP"]
    assert w and w[0]["n"] == 40 and r["ok"]
    r2 = gc.check(write(tmp_path, "b2.nc", body), STOCK, clearance=-3.5)    # allow rapids above Z-3.5
    assert "RAPID_BELOW_TOP" not in codes(r2)


# ---- rule checks
def test_feed_spindle_tool_rules(tmp_path):
    body = HEADER + "N20 G00 X50. Y0. Z5.\nN25 G01 Z-1.\nN30 T2 M06\nN35 G01 X60. F200.\nN40 S5000 M03\nN45 M30\n%\n"
    r = gc.check(write(tmp_path, "c.nc", body), STOCK)
    by = {(p["code"], p["n"]) for p in r["problems"]}
    assert ("FEED_MISSING", 25) in by and ("SPINDLE_OFF", 25) in by and ("NO_TOOL", 25) in by
    assert ("SPINDLE_OFF", 35) in by and ("FEED_MISSING", 35) not in by


def test_expected_tools_h_mismatch_and_end(tmp_path):
    body = HEADER + "N20 T7 M06\nN25 S1000 M03\nN30 G43 Z15. H02\nN35 G00 X0. Y0.\n"
    r = gc.check(write(tmp_path, "d.nc", body), STOCK, expected_tools=[1, 2])
    c = codes(r)
    assert "TOOL_UNEXPECTED" in c and "H_MISMATCH" in c and "NO_END" in c


def test_envelope(tmp_path):
    body = HEADER + "N20 T2 M06\nN25 S5000 M03\nN30 G00 X0. Y0. Z15.\nN35 G00 X400.\nN40 M30\n%\n"
    r = gc.check(write(tmp_path, "e.nc", body), STOCK, limits=(-300, -200, -50, 300, 200, 100))
    env = [p for p in r["problems"] if p["code"] == "ENVELOPE"]
    assert env and env[0]["n"] == 35 and "X400.000" in env[0]["message"]


def test_arc_radius_and_g18_direction(tmp_path):
    good = HEADER + ("N20 T2 M06\nN25 S5000 M03\nN30 G00 X32. Y0. Z5.\nN35 G01 Z-10. F300.\n"
                     "N40 G18 G02 X33. Z-11. I1.\nN45 G17 G00 Z15.\nN50 M30\n%\n")
    r = gc.check(write(tmp_path, "f.nc", good), STOCK)
    assert "ARC_RADIUS" not in codes(r)
    # a G18 quarter arc must not sweep 270 deg (which would dip to Z-12, below Z-11)
    assert r["per_tool"]["T2"]["min_z"] == pytest.approx(-11.0, abs=1e-6)
    bad = HEADER + "N20 T2 M06\nN25 S5000 M03\nN30 G00 X0. Y0. Z5.\nN35 G02 X10. Y0. I3. J0. F300.\nN40 M30\n%\n"
    r = gc.check(write(tmp_path, "g.nc", bad), STOCK)
    assert [p["n"] for p in r["problems"] if p["code"] == "ARC_RADIUS"] == [35]


def test_incremental_and_inch_units(tmp_path):
    body = ("%\n(T2 D=10. CR=0. - FLAT END MILL)\nN10 G20 G90\nN15 T2 M06\nN20 S5000 M03\n"
            "N25 G00 X0. Y0. Z1.\nN30 G91 G00 X1.\nN35 G90 M30\n%\n")
    r = gc.check(write(tmp_path, "h.nc", body), None, limits=(-30, -30, -30, 30, 30, 30))
    assert "ENVELOPE" not in codes(r)                    # Z1 in = 25.4 mm and X+1 in = 25.4 mm: inside 30
    r = gc.check(write(tmp_path, "h2.nc", body), None, limits=(-20, -20, -30, 20, 20, 30))
    assert [p["n"] for p in r["problems"] if p["code"] == "ENVELOPE"] == [30]


def test_canned_cycle_needs_r_and_z_and_drills_material(tmp_path):
    body = ("%\n(T3 D=16. CR=0. TAPER=118DEG - DRILL)\nN10 G90 G21\nN15 T3 M06\nN20 S800 M03\n"
            "N25 G00 X0. Y0. Z15.\nN30 G98 G81 X0. Y0. Z-16.807 R4. F100.\nN35 G80\nN40 M30\n%\n")
    r = gc.check(write(tmp_path, "i.nc", body), STOCK)
    assert r["ok"] and r["per_tool"]["T3"]["min_z"] == pytest.approx(-16.807)
    hole = 3.14159 * 8 ** 2 * 11
    assert r["removed_volume_mm3"] == pytest.approx(hole, rel=0.03)
    r = gc.check(write(tmp_path, "j.nc", body.replace(" R4.", "")), STOCK)
    assert "CYCLE_INCOMPLETE" in codes(r)


def test_tool_profiles():
    assert gc.Tool(1, 10).zoff(5) == 0.0 and gc.Tool(1, 10).zoff(5.1) is None
    ball = gc.Tool(2, 10, 5)
    assert ball.kind() == "ball" and ball.zoff(5) == pytest.approx(5.0) and ball.zoff(0) == 0.0
    drill = gc.Tool(3, 16, 0, 118)
    assert drill.zoff(8) == pytest.approx(8 / 1.6643, rel=1e-3)          # 4.807 mm point length
    t = gc.tools_from_comments(["(T3 D=16. CR=0. TAPER=118DEG - ZMIN=-16.807 - DRILL)",
                                "(T1 D=50. CR=0. - ZMIN=-1. - FACE MILL)"])
    assert t[3].taper == 118.0 and t[1].d == 50.0


def test_camotics_status_and_missing_file():
    st = gc.camotics_status()
    assert "installed" in st and "note" in st
    with pytest.raises(FileNotFoundError):
        gc.check(["no_such.nc"], STOCK)


def test_cli_exit_code(tmp_path, capsys):
    assert gc.main(["--stock", ",".join(map(str, STOCK))] + PLATE) == 0
    bad = write(tmp_path, "k.nc", HEADER + "N20 T2 M06\nN25 S5000 M03\nN30 G00 X0. Y0. Z-3.\nN35 M30\n%\n")
    assert gc.main(["--stock", ",".join(map(str, STOCK)), bad]) == 1
