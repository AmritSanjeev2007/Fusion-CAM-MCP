"""Unit conversion between the tool layer (mm, mm^2, mm^3, degrees) and Fusion's API (cm, rad).

Pure Python, no adsk import, so it is unit-testable outside Fusion. Every op takes and
returns millimetres; convert at the API boundary with these helpers only.
"""

import math

MM_PER_CM = 10.0


def mm_to_cm(v: float) -> float:
    return v / MM_PER_CM


def cm_to_mm(v: float) -> float:
    return v * MM_PER_CM


def cm2_to_mm2(v: float) -> float:
    return v * MM_PER_CM ** 2


def cm3_to_mm3(v: float) -> float:
    return v * MM_PER_CM ** 3


def deg_to_rad(v: float) -> float:
    return math.radians(v)


def rad_to_deg(v: float) -> float:
    return math.degrees(v)


def pt_mm(p) -> tuple:
    """(x, y, z) in mm from an adsk Point3D/Vector3D-like object (cm) or an (x, y, z) sequence in cm."""
    if hasattr(p, "x"):
        return (cm_to_mm(p.x), cm_to_mm(p.y), cm_to_mm(p.z))
    return tuple(cm_to_mm(c) for c in p)


def rnd(v, nd: int = 4):
    """Round a float or a nested list/tuple of floats for reporting (-0.0 -> 0.0)."""
    if isinstance(v, (list, tuple)):
        return [rnd(x, nd) for x in v]
    r = round(float(v), nd)
    return 0.0 if r == 0 else r
