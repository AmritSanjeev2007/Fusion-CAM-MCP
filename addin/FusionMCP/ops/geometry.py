"""inspect: bodies, bounding boxes, volume, top-face Z, horizontal planar faces and holes.

Runs on Fusion's main thread. Reads geometry from the active design (bodies in the root
component and occurrence proxies, so everything is in world coordinates), converts it to mm
(ops/units.py) and leaves the maths to ops/geomcalc.py (pure, unit-tested).
"""

import math

import adsk.core
import adsk.fusion

from . import geomcalc, guards, units

PARALLEL = 1e-6
MAX_PLANAR_GROUPS = 40


def _app():
    return adsk.core.Application.get()


def iter_bodies(design):
    """(name, body) for root bodies and every occurrence's bodies (proxies: world coordinates)."""
    root = design.rootComponent
    for b in root.bRepBodies:
        yield b.name, b
    for occ in root.allOccurrences:
        for b in occ.bRepBodies:
            yield f"{occ.fullPathName}/{b.name}", b


def body_bbox_mm(body):
    """Tight world-axis bounding box ((min), (max)) in mm.

    BRepBody.boundingBox may be loose on curved geometry, so use the measure manager's oriented
    box aligned to world X/Y (length = X, width = Y, height = Z); fall back to boundingBox."""
    try:
        obb = _app().measureManager.getOrientedBoundingBox(
            body, adsk.core.Vector3D.create(1, 0, 0), adsk.core.Vector3D.create(0, 1, 0))
        if obb is not None:
            c = units.pt_mm(obb.centerPoint)
            half = (units.cm_to_mm(obb.length) / 2, units.cm_to_mm(obb.width) / 2, units.cm_to_mm(obb.height) / 2)
            return (geomcalc.sub(c, half), geomcalc.add(c, half))
    except Exception:
        pass
    bb = body.boundingBox
    return (units.pt_mm(bb.minPoint), units.pt_mm(bb.maxPoint))


def box_report(bbox) -> dict:
    return {"min": units.rnd(bbox[0]), "max": units.rnd(bbox[1]), "size": units.rnd(geomcalc.box_size(bbox))}


def body_volume_mm3(body) -> float:
    try:
        return units.cm3_to_mm3(body.physicalProperties.volume)
    except Exception:
        return units.cm3_to_mm3(body.volume)


def summarize(design) -> dict:
    """Bodies with per-body and overall bbox (mm) and volume (mm^3). Shared with import_step."""
    bodies, boxes, vol = [], [], 0.0
    for name, b in iter_bodies(design):
        bb = body_bbox_mm(b)
        v = body_volume_mm3(b) if b.isSolid else 0.0
        boxes.append(bb)
        vol += v
        bodies.append({"name": name, "solid": b.isSolid, "visible": b.isVisible, "faces": b.faces.count,
                       "bbox_mm": box_report(bb), "volume_mm3": units.rnd(v, 3)})
    overall = geomcalc.union_boxes(boxes)
    return {"body_count": len(bodies), "bodies": bodies,
            "overall": {"bbox_mm": box_report(overall) if overall else None, "volume_mm3": units.rnd(vol, 3)},
            "_overall_box": overall, "_volume": vol}


def _normal_at(face):
    p = face.pointOnFace
    ok, n = face.evaluator.getNormalAtPoint(p)
    if not ok:
        return p, None
    return p, (n.x, n.y, n.z)


def _surface(face):
    return face.geometry.surfaceType


def _axis_aligned(axis) -> bool:
    return any(abs(abs(c) - 1.0) <= PARALLEL for c in geomcalc.unit(axis))


def _is_cap(other, axis, origin_mm, radius_mm) -> bool | None:
    """Does `other` (the face across a hole's circular end edge) close the hole?

    A floor or drill-point cone lies entirely within the hole's radius; an opening onto a top
    face, a counterbore floor or a countersink reaches beyond it. Uses the face's bounding box,
    so it is only decided for holes along a world axis (None otherwise)."""
    if not _axis_aligned(axis):
        return None
    bb = other.boundingBox
    box = (units.pt_mm(bb.minPoint), units.pt_mm(bb.maxPoint))
    reach = max(geomcalc.dist_point_line(c, origin_mm, axis) for c in geomcalc.box_corners(box))
    return reach <= radius_mm * math.sqrt(2) + geomcalc.LEN_TOL


def _cylinder_record(face, body_idx) -> dict:
    cyl = face.geometry
    axis = (cyl.axis.x, cyl.axis.y, cyl.axis.z)
    origin = units.pt_mm(cyl.origin)
    r = units.cm_to_mm(cyl.radius)
    p, n = _normal_at(face)
    concave = geomcalc.is_concave(units.pt_mm(p), n, origin, axis) if n else True
    edges = []
    for e in face.edges:
        g = e.geometry
        if g.curveType not in (adsk.core.Curve3DTypes.Circle3DCurveType, adsk.core.Curve3DTypes.Arc3DCurveType):
            continue
        if abs(units.cm_to_mm(g.radius) - r) > geomcalc.LEN_TOL:
            continue
        closed = None
        for other in e.faces:
            if other == face:
                continue
            try:
                if (_surface(other) == adsk.core.SurfaceTypes.CylinderSurfaceType
                        and abs(units.cm_to_mm(other.geometry.radius) - r) <= geomcalc.LEN_TOL):
                    continue            # the other half of a split hole wall
                closed = _is_cap(other, axis, origin, r)
            except Exception:
                closed = None
        edges.append({"centre": units.pt_mm(g.center), "closed": closed})
    bb = face.boundingBox
    return {"radius": r, "axis": axis, "origin": origin, "concave": concave, "edges": edges,
            "bbox": (units.pt_mm(bb.minPoint), units.pt_mm(bb.maxPoint)), "body": body_idx,
            "area": units.cm2_to_mm2(face.area)}


def inspect(args: dict):
    """Report on the active design: bodies, bbox, volume, top-face Z, planar faces, holes (mm)."""
    doc, design = guards.active_design("inspect")
    summ = summarize(design)
    planar, cyls, bosses = [], [], 0
    body_boxes = {}
    for idx, (name, body) in enumerate(iter_bodies(design)):
        body_boxes[idx] = body_bbox_mm(body)
        for face in body.faces:
            st = _surface(face)
            if st == adsk.core.SurfaceTypes.PlaneSurfaceType:
                p, n = _normal_at(face)
                if n and abs(abs(n[2]) - 1.0) <= PARALLEL:
                    planar.append({"z": units.cm_to_mm(p.z), "area": units.cm2_to_mm2(face.area),
                                   "facing": "up" if n[2] > 0 else "down", "body": idx})
            elif st == adsk.core.SurfaceTypes.CylinderSurfaceType:
                rec = _cylinder_record(face, idx)
                if rec["concave"]:
                    cyls.append(rec)
                else:
                    bosses += 1
    holes = geomcalc.holes_from_faces(cyls, body_boxes)
    grouped = geomcalc.group_holes(holes)
    top = geomcalc.top_face_z(planar)
    planar_groups = geomcalc.group_planar(planar)
    out = {
        "document": doc.name,
        "units": "mm",
        "scratch": guards.is_scratch(doc),
        "body_count": summ["body_count"],
        "bodies": summ["bodies"],
        "overall": summ["overall"],
        "top_face_z_mm": units.rnd(top) if top is not None else None,
        "horizontal_planar_faces": planar_groups[:MAX_PLANAR_GROUPS],
        "holes": grouped,
        "hole_count": len(holes),
        "convex_cylinder_faces": bosses,
        "notes": ("Z is world Z. holes: concave cylinders merged across split faces; depth is the "
                  "cylindrical wall only (excludes drill points/counterbores); through_basis 'topology' "
                  "= decided from the end faces, 'extent' = depth vs body extent along the axis."),
    }
    if len(planar_groups) > MAX_PLANAR_GROUPS:
        out["horizontal_planar_faces_truncated"] = len(planar_groups)
    return out
