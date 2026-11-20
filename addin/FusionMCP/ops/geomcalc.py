"""Pure geometry helpers for inspect/import_step. No adsk import: unit-tested outside Fusion.

Everything here is in millimetres. ops/geometry.py reads faces and edges from Fusion,
converts them to plain tuples/dicts in mm (ops/units.py), and hands them to these functions.

Cylinder face record (input to merge_cylinders / holes_from_faces):
    {"radius": float, "axis": (x, y, z), "origin": (x, y, z), "concave": bool,
     "edges": [{"centre": (x, y, z), "closed": bool | None}, ...],   # circular/arc edges only
     "bbox": ((minx, miny, minz), (maxx, maxy, maxz)) | None,
     "body": int}
`closed` says whether the face on the other side of that edge caps the hole (a floor no
bigger than the hole's disc, or a drill-point cone); None if it could not be determined.
"""

import math

LEN_TOL = 1e-3          # mm: radius match, collinearity, same-Z grouping
DIR_TOL = 1e-6          # 1 - |cos| for parallel axes


# ---------------------------------------------------------------- vectors
def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    return math.sqrt(dot(a, a))


def unit(a):
    n = norm(a)
    if n == 0:
        raise ValueError("zero-length vector")
    return (a[0] / n, a[1] / n, a[2] / n)


def canonical_dir(a):
    """Unit vector with its sign fixed (first non-negligible component positive), so +Z and -Z
    axes compare equal."""
    u = unit(a)
    for c in u:
        if abs(c) > 1e-9:
            return u if c > 0 else scale(u, -1.0)
    return u


def parallel(a, b, tol=DIR_TOL):
    return 1.0 - abs(dot(unit(a), unit(b))) <= tol


def dist_point_line(p, origin, axis):
    """Distance from point p to the infinite line through origin along axis."""
    return norm(cross(sub(p, origin), unit(axis)))


def is_concave(point, normal, axis_origin, axis_dir) -> bool:
    """A cylindrical face is a hole wall (concave) when its outward normal at a surface point
    points toward the cylinder axis; a boss/shaft (convex) has it pointing away."""
    u = unit(axis_dir)
    rel = sub(axis_origin, point)
    to_axis = sub(rel, scale(u, dot(rel, u)))          # perpendicular component, point -> axis
    return dot(normal, to_axis) > 0


def box_corners(bbox):
    (x0, y0, z0), (x1, y1, z1) = bbox
    return [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]


def union_boxes(boxes):
    """Overall ((min), (max)) of a list of ((min), (max)) boxes; None if empty."""
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    lo = tuple(min(b[0][i] for b in boxes) for i in range(3))
    hi = tuple(max(b[1][i] for b in boxes) for i in range(3))
    return (lo, hi)


def box_size(bbox):
    return tuple(bbox[1][i] - bbox[0][i] for i in range(3))


def project_range(points, origin, axis):
    u = unit(axis)
    ts = [dot(sub(p, origin), u) for p in points]
    return (min(ts), max(ts))


# ---------------------------------------------------------------- holes
def same_cylinder(a: dict, b: dict, tol=LEN_TOL) -> bool:
    """Two cylinder faces belong to one hole if same body, same radius and collinear axes
    (Fusion/STEP often split a hole wall into two half-cylinder faces)."""
    return (a.get("body") == b.get("body")
            and a.get("concave", True) == b.get("concave", True)
            and abs(a["radius"] - b["radius"]) <= tol
            and parallel(a["axis"], b["axis"])
            and dist_point_line(b["origin"], a["origin"], a["axis"]) <= tol)


def merge_cylinders(faces: list[dict], tol=LEN_TOL) -> list[list[dict]]:
    """Group cylinder faces that form one cylinder (union-find). Preserves first-seen order."""
    parent = list(range(len(faces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(faces)):
        for j in range(i + 1, len(faces)):
            if find(i) != find(j) and same_cylinder(faces[i], faces[j], tol):
                parent[find(j)] = find(i)
    groups: dict[int, list] = {}
    for i, f in enumerate(faces):
        groups.setdefault(find(i), []).append(f)
    return list(groups.values())


def classify_through(depth, body_extent, closed_ends=None, tol=LEN_TOL):
    """Return (through: bool, basis: str).

    Topology wins when known: a capped end (floor or drill point) means blind; both ends
    known open means through. Otherwise compare the hole depth with the body's extent along
    the hole axis (exact for prismatic plates, may call a hole in a recessed region blind)."""
    if closed_ends:
        if any(c is True for c in closed_ends):
            return False, "topology"
        if len(closed_ends) >= 2 and all(c is False for c in closed_ends):
            return True, "topology"
    if body_extent is None:
        return False, "unknown"
    return depth >= body_extent - tol, "extent"


def hole_from_group(group: list[dict], body_bbox=None, tol=LEN_TOL) -> dict:
    """Diameter, depth, axis, centre and through/blind of one merged cylinder (mm)."""
    f0 = group[0]
    axis = canonical_dir(f0["axis"])
    origin = f0["origin"]
    edges = [e for f in group for e in f.get("edges", [])]
    ts = sorted(dot(sub(e["centre"], origin), axis) for e in edges)
    closed = None
    if ts and ts[-1] - ts[0] > tol:
        tmin, tmax = ts[0], ts[-1]
        basis = "edges"
        closed = []
        for t_end in (tmin, tmax):
            flags = [e.get("closed") for e in edges
                     if abs(dot(sub(e["centre"], origin), axis) - t_end) <= tol]
            closed.append(True if True in flags else (False if flags and all(x is False for x in flags) else None))
    else:
        boxes = [f["bbox"] for f in group if f.get("bbox")]
        if not boxes:
            raise ValueError("cylinder group has neither two circular edges nor a bounding box")
        tmin, tmax = project_range([c for b in boxes for c in box_corners(b)], origin, axis)
        basis = "bbox"
    depth = tmax - tmin
    body_extent = None
    if body_bbox:
        bmin, bmax = project_range(box_corners(body_bbox), origin, axis)
        body_extent = bmax - bmin
    through, through_basis = classify_through(depth, body_extent, closed, tol)
    p_min = add(origin, scale(axis, tmin))
    p_max = add(origin, scale(axis, tmax))
    # entry end first: the one higher in world Z (the side a Z-down drill would start from)
    ends = [p_max, p_min] if (p_max[2], tmax) >= (p_min[2], tmin) else [p_min, p_max]
    return {
        "diameter_mm": 2 * f0["radius"],
        "depth_mm": depth,
        "axis": axis,
        "centre_mm": add(origin, scale(axis, (tmin + tmax) / 2)),
        "ends_mm": ends,
        "through": through,
        "through_basis": through_basis,
        "depth_basis": basis,
        "body_extent_mm": body_extent,
        "faces": len(group),
        "body": f0.get("body"),
    }


def holes_from_faces(faces: list[dict], body_bboxes: dict | None = None, tol=LEN_TOL) -> list[dict]:
    """Concave cylinder faces -> list of holes. body_bboxes maps body index -> bbox."""
    concave = [f for f in faces if f.get("concave", True)]
    body_bboxes = body_bboxes or {}
    return [hole_from_group(g, body_bboxes.get(g[0].get("body")), tol) for g in merge_cylinders(concave, tol)]


def _r(v, nd=3):
    if isinstance(v, (list, tuple)):
        return [_r(x, nd) for x in v]
    r = round(float(v), nd)
    return 0.0 if r == 0 else r


def group_holes(holes: list[dict], nd: int = 3) -> list[dict]:
    """Identical holes (diameter, depth, through, axis) -> one entry with a count and centres.
    Sorted by diameter, largest first."""
    groups: dict[tuple, dict] = {}
    for h in holes:
        key = (_r(h["diameter_mm"], nd), _r(h["depth_mm"], nd), bool(h["through"]), tuple(_r(h["axis"], 4)))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"diameter_mm": key[0], "depth_mm": key[1], "through": key[2],
                               "axis": list(key[3]), "through_basis": h.get("through_basis"), "count": 0, "centres_mm": [], "entry_mm": [],
                               "bodies": []}
        g["count"] += 1
        g["centres_mm"].append(_r(h["centre_mm"], nd))
        g["entry_mm"].append(_r(h["ends_mm"][0], nd))
        if h.get("body") is not None and h["body"] not in g["bodies"]:
            g["bodies"].append(h["body"])
    return sorted(groups.values(), key=lambda g: (-g["diameter_mm"], g["depth_mm"]))


# ---------------------------------------------------------------- planar faces
def group_planar(faces: list[dict], tol=LEN_TOL) -> list[dict]:
    """Horizontal planar faces {"z", "area", "facing": "up"|"down"} grouped by Z and facing.
    Sorted top to bottom."""
    out: list[dict] = []
    for f in sorted(faces, key=lambda f: -f["z"]):
        for g in out:
            if g["facing"] == f["facing"] and abs(g["z_mm"] - f["z"]) <= tol:
                g["area_mm2"] += f["area"]
                g["count"] += 1
                break
        else:
            out.append({"z_mm": f["z"], "facing": f["facing"], "area_mm2": f["area"], "count": 1})
    for g in out:
        g["z_mm"] = _r(g["z_mm"])
        g["area_mm2"] = _r(g["area_mm2"])
    return out


def top_face_z(faces: list[dict]):
    """Highest Z among up-facing horizontal planar faces, or None."""
    ups = [f["z"] for f in faces if f["facing"] == "up"]
    return max(ups) if ups else None


# ---------------------------------------------------------------- import checks
def within(actual, expected, tol_pct) -> bool:
    return abs(actual - expected) <= abs(expected) * tol_pct / 100.0 + 1e-9


def pct_diff(actual, expected):
    return 0.0 if expected == 0 else (actual - expected) / expected * 100.0


def compare_bbox(actual, expected, tol_pct: float = 0.1) -> dict:
    """Compare bounding-box sizes axis-agnostically (sorted), so a Y-up export of an 80x50x10
    plate (80, 10, 50) still matches. Warns when the per-axis order differs."""
    if len(actual) != 3 or len(expected) != 3:
        raise ValueError("bounding boxes need 3 dimensions")
    sa, se = sorted(actual), sorted(expected)
    ok = all(within(a, e, tol_pct) for a, e in zip(sa, se))
    same_axes = all(within(a, e, tol_pct) for a, e in zip(actual, expected))
    res = {"ok": ok, "actual_mm": _r(list(actual)), "expected_mm": _r(list(expected)),
           "sorted_diff_pct": [_r(pct_diff(a, e), 4) for a, e in zip(sa, se)],
           "axes_match": same_axes}
    if ok and not same_axes:
        res["warning"] = (f"sizes match but axes differ: got X,Y,Z = {_r(list(actual))}, expected "
                          f"{_r(list(expected))}. The part is rotated (e.g. Y-up vs Z-up export); "
                          "orient the CAM setup/WCS accordingly.")
    return res


def compare_volume(actual, expected, tol_pct: float = 0.1) -> dict:
    return {"ok": within(actual, expected, tol_pct), "actual_mm3": _r(actual), "expected_mm3": _r(expected),
            "diff_pct": _r(pct_diff(actual, expected), 4)}


def import_check(size, volume, expected_size=None, expected_volume=None, tol_pct: float = 0.1):
    """Check block for import_step; None if nothing was expected."""
    if expected_size is None and expected_volume is None:
        return None
    chk = {"tolerance_pct": tol_pct}
    warnings = []
    oks = []
    if expected_size is not None:
        chk["bbox"] = compare_bbox(size, expected_size, tol_pct)
        oks.append(chk["bbox"]["ok"])
        if "warning" in chk["bbox"]:
            warnings.append(chk["bbox"]["warning"])
    if expected_volume is not None:
        chk["volume"] = compare_volume(volume, expected_volume, tol_pct)
        oks.append(chk["volume"]["ok"])
    chk["ok"] = all(oks)
    chk["warnings"] = warnings
    return chk


def validate_expected_size(v):
    if v is None:
        return None
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        raise ValueError("expected_size_mm must be a list of 3 numbers, e.g. [80, 50, 10]")
    out = [float(x) for x in v]
    if any(x <= 0 for x in out):
        raise ValueError("expected_size_mm values must be positive")
    return out
