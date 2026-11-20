"""Unit tests for the pure geometry helpers behind import_step and inspect (no Fusion)."""

import math
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "addin"))

for name in ("adsk", "adsk.core", "adsk.fusion", "adsk.cam"):
    sys.modules.setdefault(name, types.ModuleType(name))

from FusionMCP.ops import geomcalc as g  # noqa: E402
from FusionMCP.ops import units  # noqa: E402

Z = (0.0, 0.0, 1.0)
PLATE_BOX = ((-40.0, -25.0, 0.0), (40.0, 25.0, 10.0))       # 80 x 50 x 10 test plate


def half_cyl(cx, cy, r, z0, z1, closed_bottom=None, body=0, axis=Z, concave=True):
    """One half of a vertical hole wall: two arc edges (top and bottom) on the axis."""
    return {"radius": r, "axis": axis, "origin": (cx, cy, 0.0), "concave": concave, "body": body,
            "edges": [{"centre": (cx, cy, z1), "closed": False},
                      {"centre": (cx, cy, z0), "closed": closed_bottom}],
            "bbox": ((cx - r, cy - r, z0), (cx + r, cy + r, z1))}


# ---- units
def test_units_cm_to_mm():
    assert units.cm_to_mm(1.0) == 10.0
    assert units.mm_to_cm(80.0) == 8.0
    assert units.cm3_to_mm3(3.7395) == pytest.approx(3739.5)
    assert units.cm2_to_mm2(1.0) == 100.0
    assert units.pt_mm((1.0, -2.0, 0.5)) == (10.0, -20.0, 5.0)
    assert units.rnd([-0.00001, 1.23456], 3) == [0.0, 1.235]


# ---- concavity
def test_concave_vs_convex():
    # point on a radius-5 cylinder at +X; hole wall's outward normal points into the hole (-X)
    assert g.is_concave((5, 0, 3), (-1, 0, 0), (0, 0, 0), Z)
    assert not g.is_concave((5, 0, 3), (1, 0, 0), (0, 0, 0), Z)       # boss/shaft


# ---- merging
def test_two_half_cylinders_merge_into_one_hole():
    a = half_cyl(10, 5, 8.0, 0, 10, closed_bottom=False)
    b = half_cyl(10, 5, 8.0, 0, 10, closed_bottom=False)
    b["origin"] = (10, 5, 7.5)            # different point on the same axis
    b["axis"] = (0, 0, -1)                 # opposite sense, still collinear
    holes = g.holes_from_faces([a, b], {0: PLATE_BOX})
    assert len(holes) == 1
    h = holes[0]
    assert h["faces"] == 2
    assert h["diameter_mm"] == pytest.approx(16.0)
    assert h["depth_mm"] == pytest.approx(10.0)
    assert h["through"] and h["through_basis"] == "topology"
    assert h["centre_mm"] == pytest.approx((10, 5, 5))
    assert h["ends_mm"][0] == pytest.approx((10, 5, 10))      # entry = top
    assert h["axis"] == pytest.approx(Z)


def test_not_merged_different_radius_or_offset_axis_or_body():
    base = half_cyl(0, 0, 3.0, 0, 10)
    assert len(g.merge_cylinders([base, half_cyl(0, 0, 3.5, 0, 10)])) == 2
    assert len(g.merge_cylinders([base, half_cyl(0.01, 0, 3.0, 0, 10)])) == 2
    assert len(g.merge_cylinders([base, half_cyl(0, 0, 3.0, 0, 10, axis=(1, 0, 0))])) == 2
    assert len(g.merge_cylinders([base, half_cyl(0, 0, 3.0, 0, 10, body=1)])) == 2
    assert len(g.merge_cylinders([base, half_cyl(0, 0, 3.0 + 5e-4, 0, 10)])) == 1


def test_convex_cylinders_are_not_holes():
    assert g.holes_from_faces([half_cyl(0, 0, 3, 0, 10, concave=False)], {0: PLATE_BOX}) == []


# ---- through vs blind
def test_blind_hole_by_topology():
    h = g.holes_from_faces([half_cyl(0, 0, 2.5, 4, 10, closed_bottom=True),
                            half_cyl(0, 0, 2.5, 4, 10, closed_bottom=True)], {0: PLATE_BOX})[0]
    assert not h["through"] and h["through_basis"] == "topology"
    assert h["depth_mm"] == pytest.approx(6.0)


def test_through_vs_blind_by_extent_when_topology_unknown():
    through = half_cyl(0, 0, 2.5, 0, 10, closed_bottom=None)
    through["edges"][0]["closed"] = None
    blind = half_cyl(20, 0, 2.5, 4, 10, closed_bottom=None)
    blind["edges"][0]["closed"] = None
    holes = g.holes_from_faces([through, blind], {0: PLATE_BOX})
    by_x = {round(h["centre_mm"][0]): h for h in holes}
    assert by_x[0]["through"] and by_x[0]["through_basis"] == "extent"
    assert not by_x[20]["through"] and by_x[20]["through_basis"] == "extent"
    assert by_x[0]["body_extent_mm"] == pytest.approx(10.0)


def test_depth_falls_back_to_bbox_without_two_edges():
    f = half_cyl(0, 0, 3, 2, 10)
    f["edges"] = []
    h = g.hole_from_group([f], PLATE_BOX)
    assert h["depth_basis"] == "bbox" and h["depth_mm"] == pytest.approx(8.0)
    assert not h["through"]


def test_classify_through_rules():
    assert g.classify_through(10, 10, [False, False]) == (True, "topology")
    assert g.classify_through(10, 10, [False, True]) == (False, "topology")
    assert g.classify_through(10, 10, [False, None]) == (True, "extent")
    assert g.classify_through(6, 10, None) == (False, "extent")
    assert g.classify_through(6, None, None) == (False, "unknown")


# ---- grouping
def test_group_identical_holes_with_count():
    m6a = [half_cyl(-30, 0, 2.4585, 0, 10, False), half_cyl(-30, 0, 2.4585, 0, 10, False)]
    m6b = [half_cyl(30, 0, 2.4585, 0, 10, False), half_cyl(30, 0, 2.4585, 0, 10, False)]
    big = [half_cyl(0, 0, 8, 0, 10, False), half_cyl(0, 0, 8, 0, 10, False)]
    holes = g.holes_from_faces(m6a + m6b + big, {0: PLATE_BOX})
    assert len(holes) == 3
    grouped = g.group_holes(holes)
    assert [x["count"] for x in grouped] == [1, 2]                 # largest diameter first
    assert grouped[0]["diameter_mm"] == 16.0
    assert grouped[1]["diameter_mm"] == pytest.approx(4.917)
    assert sorted(c[0] for c in grouped[1]["centres_mm"]) == [-30, 30]
    assert all(x["through"] for x in grouped)


def test_group_separates_different_depths():
    holes = g.holes_from_faces([half_cyl(0, 0, 3, 0, 10, False), half_cyl(10, 0, 3, 4, 10, True)],
                               {0: PLATE_BOX})
    assert len(g.group_holes(holes)) == 2


# ---- planar faces
def test_group_planar_and_top_face():
    faces = [{"z": 10.0, "area": 3000.0, "facing": "up"}, {"z": 10.0004, "area": 500.0, "facing": "up"},
             {"z": 0.0, "area": 3500.0, "facing": "down"}, {"z": 5.0, "area": 100.0, "facing": "up"}]
    out = g.group_planar(faces)
    assert [(x["z_mm"], x["facing"], x["count"]) for x in out] == [(10.0, "up", 2), (5.0, "up", 1), (0.0, "down", 1)]
    assert out[0]["area_mm2"] == 3500.0
    assert g.top_face_z(faces) == pytest.approx(10.0004)
    assert g.top_face_z([{"z": 0, "area": 1, "facing": "down"}]) is None


# ---- import checks (the Phase 2 acceptance plate: 80x50x10 mm, 37395 mm^3, +-0.1 %)
def plate_volume():
    corners = 4 * (5 * 5 - math.pi * 5 ** 2 / 4) * 10        # R5 corners
    big = math.pi * 8 ** 2 * 10                                # Ø16 through
    m6 = 2 * math.pi * (4.917 / 2) ** 2 * 10                   # 2x M6 at tap-drill diameter
    return 80 * 50 * 10 - corners - big - m6


def test_test_plate_volume_model_matches_plan():
    assert g.within(plate_volume(), 37395, 0.1)


def test_compare_bbox_exact_and_within_tolerance():
    r = g.compare_bbox((80.0, 50.0, 10.0), [80, 50, 10])
    assert r["ok"] and r["axes_match"] and "warning" not in r
    assert g.compare_bbox((80.07, 50.04, 10.009), [80, 50, 10], 0.1)["ok"]
    assert not g.compare_bbox((80.1, 50, 10), [80, 50, 10], 0.1)["ok"]      # 0.125 %


def test_compare_bbox_axis_agnostic_with_warning():
    r = g.compare_bbox((80.0, 10.0, 50.0), [80, 50, 10])         # Y-up export
    assert r["ok"] and not r["axes_match"]
    assert "axes differ" in r["warning"]


def test_import_check_test_plate():
    chk = g.import_check((80.0, 50.0, 10.0), 37400.0, [80, 50, 10], 37395, 0.1)
    assert chk["ok"] and chk["volume"]["ok"] and chk["bbox"]["ok"] and chk["warnings"] == []
    assert chk["volume"]["diff_pct"] == pytest.approx(0.0134, abs=1e-4)
    bad = g.import_check((80.0, 50.0, 10.0), 37440.0, [80, 50, 10], 37395, 0.1)   # +0.12 %
    assert not bad["ok"] and not bad["volume"]["ok"] and bad["bbox"]["ok"]
    rot = g.import_check((50.0, 10.0, 80.0), 37395.0, [80, 50, 10], 37395, 0.1)
    assert rot["ok"] and len(rot["warnings"]) == 1
    assert g.import_check((1, 2, 3), 6.0) is None


def test_validate_expected_size():
    assert g.validate_expected_size([80, 50, 10]) == [80.0, 50.0, 10.0]
    assert g.validate_expected_size(None) is None
    for bad in ([80, 50], [80, 50, -1], "80x50x10"):
        with pytest.raises(ValueError):
            g.validate_expected_size(bad)


def test_union_boxes():
    u = g.union_boxes([((0, 0, 0), (1, 1, 1)), ((-1, 2, 0), (0.5, 3, 4)), None])
    assert u == ((-1, 0, 0), (1, 3, 4))
    assert g.box_size(u) == (2, 3, 4)
    assert g.union_boxes([]) is None
