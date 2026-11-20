"""Pure helpers for the CAM ops (no adsk import: unit-tested outside Fusion).

Parameter names and choice values below were read live from Fusion 2706.1.17
(docs/phase3-results.md). Everything the tool layer exposes is mm, s and mm/min.
"""

import json
import math
import re

# ---------------------------------------------------------------- WCS and stock
_BOX_POINTS = {
    "top-centre": "top center", "centre": "center", "bottom-centre": "bottom center",
    "top-corner-1": "top 1", "top-corner-2": "top 2", "top-corner-3": "top 3", "top-corner-4": "top 4",
    "bottom-corner-1": "bottom 1", "bottom-corner-2": "bottom 2", "bottom-corner-3": "bottom 3",
    "bottom-corner-4": "bottom 4",
}
WCS_CHOICES = (sorted(_BOX_POINTS) + ["model-" + k for k in sorted(_BOX_POINTS)] + ["model-origin"])


def wcs_params(wcs: str) -> dict:
    """WCS name -> setup parameter expressions.

    'top-centre' etc. are points of the STOCK box (Fusion's default 'Stock box point');
    prefix 'model-' for the same point on the model box; 'model-origin' is the design origin."""
    w = (wcs or "top-centre").strip().lower().replace("_", "-").replace("center", "centre")
    if w == "model-origin":
        return {"wcs_origin_mode": "'modelOrigin'"}
    mode = "stockPoint"
    if w.startswith("model-"):
        mode, w = "modelPoint", w[len("model-"):]
    if w not in _BOX_POINTS:
        raise ValueError(f"unknown wcs '{wcs}'. Use one of: {', '.join(WCS_CHOICES)}")
    return {"wcs_origin_mode": f"'{mode}'", "wcs_origin_boxPoint": f"'{_BOX_POINTS[w]}'"}


def stock_params(offset) -> dict:
    """Relative-box stock from an offset in mm: a number (sides and top, bottom 0) or
    {"sides": mm, "top": mm, "bottom": mm}."""
    if offset is None:
        offset = 1.0
    if isinstance(offset, (int, float)) and not isinstance(offset, bool):
        sides, top, bottom = float(offset), float(offset), 0.0
    elif isinstance(offset, dict):
        unknown = set(offset) - {"sides", "top", "bottom"}
        if unknown:
            raise ValueError(f"stock_offset_mm keys must be sides/top/bottom, got {sorted(unknown)}")
        sides = float(offset.get("sides", 0.0))
        top = float(offset.get("top", 0.0))
        bottom = float(offset.get("bottom", 0.0))
    else:
        raise ValueError("stock_offset_mm must be a number or {sides, top, bottom}")
    if min(sides, top, bottom) < 0:
        raise ValueError("stock offsets must be >= 0")
    return {"job_stockMode": "'default'", "job_stockOffsetMode": "'simple'",
            "job_stockOffsetSides": f"{sides:g} mm", "job_stockOffsetTop": f"{top:g} mm",
            "job_stockOffsetBottom": f"{bottom:g} mm"}


def stock_box(model_box, offset) -> tuple:
    """Expected stock box ((min), (max)) in mm for a relative box around the model box."""
    p = stock_params(offset)
    s, t, b = (float(p[k].split()[0]) for k in ("job_stockOffsetSides", "job_stockOffsetTop", "job_stockOffsetBottom"))
    (x0, y0, z0), (x1, y1, z1) = model_box
    return ((x0 - s, y0 - s, z0 - b), (x1 + s, y1 + s, z1 + t))


def box_point(box, wcs: str) -> tuple:
    """World position (mm) of a WCS box point on a box ((min), (max))."""
    w = (wcs or "top-centre").lower().replace("model-", "").replace("center", "centre")
    (x0, y0, z0), (x1, y1, z1) = box
    cx, cy, cz = (x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2
    z = z1 if w.startswith("top") else (z0 if w.startswith("bottom") else cz)
    if w.endswith("centre"):
        return (cx, cy, z)
    # Fusion's corner numbering (read live from stockX/YLow/High per corner, 2706.1.17):
    # 1 = (-X,-Y), 2 = (+X,-Y), 3 = (-X,+Y), 4 = (+X,+Y)
    corner = {"1": (x0, y0), "2": (x1, y0), "3": (x0, y1), "4": (x1, y1)}[w[-1]]
    return (corner[0], corner[1], z)


# ---------------------------------------------------------------- parameters
def to_expression(v) -> str:
    """Python value -> Fusion CAM parameter expression.

    Strings are passed through (give lengths WITH units: '1 mm', '0.5mm'); bools become
    true/false; numbers are unitless (counts, percentages, rpm)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError(f"non-finite parameter value {v}")
        return f"{v:g}"
    if isinstance(v, str):
        if not v.strip():
            raise ValueError("empty parameter expression")
        return v
    raise ValueError(f"parameter values must be str, number or bool, got {type(v).__name__}")


def parse_params(params) -> dict:
    """dict or JSON object string -> {name: expression}."""
    if params is None or params == "":
        return {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError as e:
            raise ValueError(f"params_json is not valid JSON: {e}") from None
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object of {parameter_name: value}")
    out = {}
    for k, v in params.items():
        if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise ValueError(f"bad parameter name {k!r}")
        out[k] = to_expression(v)
    return out


# ---------------------------------------------------------------- machining time
def mm_min_to_cm_s(v: float) -> float:
    return v / 600.0


def time_report(raw: dict) -> dict:
    """getMachiningTime values (cm, s) -> mm and s."""
    out = {"total_s": round(raw["machiningTime"], 1),
           "feed_distance_mm": round(raw["feedDistance"] * 10, 1),
           "rapid_distance_mm": round(raw["rapidDistance"] * 10, 1),
           "tool_changes": int(raw["toolChangeCount"])}
    for src, dst in (("totalFeedTime", "feed_time_s"), ("totalRapidTime", "rapid_time_s"),
                     ("totalToolChangeTime", "tool_change_time_s")):
        if raw.get(src) is not None:
            out[dst] = round(raw[src], 1)
    out["total_min_s"] = f"{int(out['total_s'] // 60)}:{out['total_s'] % 60:04.1f}"
    return out


# ---------------------------------------------------------------- tools
def tool_summary(entry: dict, library: str) -> dict:
    g = entry.get("geometry") or {}
    unit = entry.get("unit", "millimeters")
    k = 25.4 if unit == "inches" else 1.0
    dc = g.get("DC")
    return {"id": entry.get("guid"), "library": library, "type": entry.get("type"),
            "description": entry.get("description") or "",
            "diameter_mm": round(dc * k, 4) if isinstance(dc, (int, float)) else None,
            "flutes": g.get("NOF"), "flute_length_mm": round(g["LCF"] * k, 3) if g.get("LCF") else None,
            "tip_angle_deg": g.get("SIG"), "unit": unit}


def filter_tools(summaries: list, tool_type=None, diameter_min=None, diameter_max=None, text=None) -> list:
    out = []
    for s in summaries:
        if tool_type and (s["type"] or "").lower() != tool_type.lower():
            continue
        d = s["diameter_mm"]
        if diameter_min is not None and (d is None or d < diameter_min - 1e-9):
            continue
        if diameter_max is not None and (d is None or d > diameter_max + 1e-9):
            continue
        if text and text.lower() not in (s["description"] + " " + (s["type"] or "")).lower():
            continue
        out.append(s)
    return sorted(out, key=lambda s: (s["type"] or "", s["diameter_mm"] if s["diameter_mm"] is not None else 1e9))


def prepare_tool_json(entry: dict, number: int) -> dict:
    """Copy of a library tool with its tool number set; length/diameter offsets follow the number
    (the bundled samples have 0, which posts as H00)."""
    t = json.loads(json.dumps(entry))
    pp = dict(t.get("post-process") or {})
    pp["number"] = float(number)
    if not pp.get("length-offset"):
        pp["length-offset"] = float(number)
    if not pp.get("diameter-offset"):
        pp["diameter-offset"] = float(number)
    t["post-process"] = pp
    return t


def pick_tool_number(used: dict, guid: str, requested=None) -> int:
    """used: {tool_number: guid} already in the document. Same tool -> same number; otherwise the
    requested number (if free) or the next free one."""
    if requested is not None:
        requested = int(requested)
        if requested < 1:
            raise ValueError("tool_number must be >= 1")
        if requested in used and used[requested] != guid:
            raise ValueError(f"tool number T{requested} is already used by another tool")
        return requested
    for n, g in sorted(used.items()):
        if g == guid:
            return n
    return max(used, default=0) + 1


# ---------------------------------------------------------------- posting
def post_url(post: str) -> str:
    p = (post or "fanuc.cps").strip()
    if "://" in p:
        return p
    if not p.lower().endswith(".cps"):
        p += ".cps"
    return "system://" + p


def safe_filename(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s.strip()).strip("._")
    return s or "program"


def tool_groups(ops: list) -> list:
    """[(op_name, tool_number), ...] -> consecutive runs that share a tool: [[names], ...]."""
    groups, last = [], object()
    for name, tn in ops:
        if groups and tn == last:
            groups[-1].append(name)
        else:
            groups.append([name])
        last = tn
    return groups


def is_tool_change_restriction(msg: str) -> bool:
    """Fusion for Personal Use refuses NC programs with tool changes (verified 2026-11-20)."""
    m = (msg or "").lower()
    return "personal use" in m and "tool change" in m
