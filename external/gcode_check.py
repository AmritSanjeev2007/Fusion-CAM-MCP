"""G-code check outside Fusion (Phase 4). Stdlib only: runs in the MCP server or as a CLI.

Fusion has no stock-simulation API and checkToolpath only validates the toolpath, so posted
programs are checked here before they go near a machine:

1. Parse Fanuc-style G-code (what fanuc.cps writes): modal G0/G1/G2/G3 (G17/G18/G19 planes,
   I/J/K or R arcs), canned cycles G81/G82/G83/G73/G85/G86 with G98/G99, R and Z, G90/G91,
   G20/G21, G43 H, T/M06, S with M03/M04/M05, F, G28, comments. Several files (cam_post writes one
   per tool on a Personal Use licence) are simulated in order on one stock.
2. Rule checks, reported by file and line:
   RAPID_INTO_STOCK   error    a rapid (G0, or a canned-cycle rapid) removes material
   RAPID_BELOW_TOP    warning  a rapid below stock top + clearance inside the stock footprint
                               (but through air: material already removed there)
   FEED_MISSING       error    G1/G2/G3 or a cycle before any F word
   SPINDLE_OFF        error    cutting move while the spindle is stopped
   NO_TOOL            error    cutting move before any T.. M06
   TOOL_UNEXPECTED    error    T number not in the expected list (from the setup)
   H_MISMATCH         warning  G43 H differs from the loaded tool number
   ENVELOPE           error    position outside the given axis limits (WCS mm)
   ARC_RADIUS         error    arc start/end radii differ by more than 0.01 mm
   BELOW_STOCK        info     tool tip below the stock bottom (normal for through features)
   NO_END             warning  file does not end with M30/M02
   UNKNOWN_CODE       info     G/M code this checker does not model
3. Material removal: a Z height-map (dexel) simulation of the stock with flat, bull-nose,
   ball and drill profiles read from the post's tool comments, e.g.
   "(T3 D=16. CR=0. TAPER=118DEG - ZMIN=-16.807 - DRILL)". Reports removed volume and is what
   detects rapids through material.
4. CAMotics: when camsim (CAMotics 1.2) is installed and a stock box is given, the same programs
   are also simulated there and both removed volumes are reported side by side (SIM_DISAGREE
   warning if they differ by more than 5 %). camsim skips G83/G73, so those lines are simulated
   as G81 in a temporary copy; it reports no collisions, but its warnings (e.g. arc radii) are
   returned with file and line. Found via CAMOTICS_HOME, PATH or the CAMotics folder under Program Files (x86).

All coordinates are WCS mm (the posted program's frame). For a top-centre WCS the stock top
is Z=0. CLI:
    python external/gcode_check.py --stock -41,-26,-11,41,26,0 [--no-camotics] file1.nc [file2.nc ...]
"""

import argparse
import json
import math
import os
import re
import shutil
import sys

INCH = 25.4
_WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_TOOL_COMMENT = re.compile(r"\(T(\d+)\s+D=([\d.]+)(?:\s+CR=([\d.]+))?(?:\s+TAPER=([\d.]+)DEG)?")
CYCLES = {81, 82, 83, 73, 85, 86, 84, 74, 76, 89}
KNOWN_G = {0, 1, 2, 3, 4, 17, 18, 19, 20, 21, 28, 40, 41, 42, 43, 49, 53, 54, 55, 56, 57, 58, 59,
           80, 90, 91, 94, 95, 98, 99} | CYCLES
KNOWN_M = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 30}


# ---------------------------------------------------------------- tools
class Tool:
    """Rotationally symmetric cutter profile: z offset above the tip at radius rho."""

    def __init__(self, number, diameter, corner_radius=0.0, taper_deg=None):
        self.number = number
        self.d = float(diameter)
        self.r = self.d / 2
        self.cr = min(float(corner_radius or 0.0), self.r)
        self.taper = float(taper_deg) if taper_deg else None

    def zoff(self, rho):
        if rho > self.r + 1e-9:
            return None
        if self.taper:                          # drill / spot drill point
            return rho / math.tan(math.radians(self.taper / 2))
        flat = self.r - self.cr
        if rho <= flat or self.cr <= 0:
            return 0.0
        dx = rho - flat
        return self.cr - math.sqrt(max(self.cr ** 2 - dx ** 2, 0.0))

    def kind(self):
        if self.taper:
            return f"drill {self.taper:g} deg"
        if self.cr >= self.r - 1e-9:
            return "ball"
        return "bull nose" if self.cr > 0 else "flat"

    def as_dict(self):
        return {"number": self.number, "diameter_mm": self.d, "corner_radius_mm": self.cr,
                "tip_angle_deg": self.taper, "profile": self.kind()}


def tools_from_comments(lines):
    out = {}
    for line in lines:
        m = _TOOL_COMMENT.search(line.upper())
        if m:
            n = int(m.group(1))
            out[n] = Tool(n, float(m.group(2)), float(m.group(3) or 0), m.group(4))
    return out


# ---------------------------------------------------------------- height map
class HeightMap:
    def __init__(self, stock, res=0.5):
        (self.x0, self.y0, self.z0), (self.x1, self.y1, self.z1) = stock
        if not (self.x1 > self.x0 and self.y1 > self.y0 and self.z1 > self.z0):
            raise ValueError("stock box must be (min, max) with positive size")
        self.res = res
        self.nx = max(1, int(math.ceil((self.x1 - self.x0) / res)))
        self.ny = max(1, int(math.ceil((self.y1 - self.y0) / res)))
        self.h = [self.z1] * (self.nx * self.ny)
        self.removed = 0.0
        self.cell_area = res * res
        self._kernels = {}

    def kernel(self, tool):
        k = self._kernels.get(tool.number)
        if k is None:
            n = int(math.ceil(tool.r / self.res))
            k = []
            for di in range(-n, n + 1):
                for dj in range(-n, n + 1):
                    z = tool.zoff(math.hypot(di * self.res, dj * self.res))
                    if z is not None:
                        k.append((di, dj, z))
            self._kernels[tool.number] = k
        return k

    def plunge(self, tool, x, y, z):
        """Tool tip at (x, y, z): lower the map under the cutter (exact distance from the tool axis to
        each cell centre). Returns the deepest material hit."""
        if z >= self.z1:
            return 0.0
        res, nx, ny, h = self.res, self.nx, self.ny, self.h
        fx, fy = (x - self.x0) / res - 0.5, (y - self.y0) / res - 0.5      # tool axis in cell-centre units
        n = tool.r / res
        i0, i1 = max(0, int(math.ceil(fx - n))), min(nx - 1, int(math.floor(fx + n)))
        j0, j1 = max(0, int(math.ceil(fy - n))), min(ny - 1, int(math.floor(fy + n)))
        if i0 > i1 or j0 > j1:
            return 0.0
        hit, area, z0 = 0.0, self.cell_area, self.z0
        flat = (tool.taper is None and tool.cr <= 0)
        r2 = n * n
        for j in range(j0, j1 + 1):
            dy = j - fy
            row = j * nx
            for i in range(i0, i1 + 1):
                dx = i - fx
                d2 = dx * dx + dy * dy
                if d2 > r2:
                    continue
                zc = z if flat else z + tool.zoff(math.sqrt(d2) * res)
                k = row + i
                cur = h[k]
                if zc < cur:
                    zn = zc if zc > z0 else z0
                    depth = cur - zn
                    if depth > 0:
                        self.removed += depth * area
                        if depth > hit:
                            hit = depth
                        h[k] = zn
        return hit


# ---------------------------------------------------------------- interpreter
class Problem(dict):
    pass


class Checker:
    def __init__(self, stock=None, limits=None, expected_tools=None, clearance=0.0, res=0.5,
                 collision_tol=0.05, tools=None):
        self.stock = stock
        self.limits = limits            # ((xmin, ymin, zmin), (xmax, ymax, zmax)) or None
        self.expected = set(int(t) for t in expected_tools) if expected_tools else None
        self.clearance = float(clearance)
        self.collision_tol = collision_tol
        self.hm = HeightMap(stock, res) if stock else None
        self.tools = dict(tools or {})
        self.problems = []
        self.stats = {}
        self._seen = set()

    # -- reporting
    def problem(self, severity, code, msg, once_key=None):
        key = (code, self.file, self.lineno) if once_key is None else (code, once_key)
        if key in self._seen:
            return
        self._seen.add(key)
        self.problems.append(Problem(severity=severity, code=code, file=os.path.basename(self.file),
                                     line=self.lineno, n=self.nword, text=self.raw.strip()[:120], message=msg))

    # -- state per file
    def reset_file(self, path):
        self.file = path
        self.pos = [None, None, None]
        self.mode = 0
        self.abs = True
        self.plane = 17
        self.unit = 1.0
        self.feed = None
        self.spindle = False
        self.tool = None
        self.pending_tool = None
        self.cycle = None
        self.retract_mode = 98
        self.cycle_r = None
        self.cycle_z = None
        self.init_z = None
        self.ended = False

    def run(self, paths):
        for p in paths:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            for n, t in tools_from_comments(lines).items():
                self.tools.setdefault(n, t)
            self.reset_file(p)
            for i, raw in enumerate(lines, 1):
                self.lineno, self.raw = i, raw
                self.line(raw)
            if not self.ended:
                self.lineno, self.raw, self.nword = len(lines), lines[-1] if lines else "", None
                self.problem("warning", "NO_END", "program does not end with M30/M02")
        return self.report(paths)

    def line(self, raw):
        code = re.sub(r"\([^)]*\)", "", raw.upper()).split(";")[0].strip()
        self.nword = None
        if not code or code in ("%",):
            return
        words = _WORD.findall(code)
        if not words:
            return
        if code.startswith("O"):
            return
        gs, ms, val = [], [], {}
        for letter, num in words:
            if letter == "G":
                gs.append(int(float(num)))
            elif letter == "M":
                ms.append(int(float(num)))
            elif letter == "N":
                self.nword = int(float(num))
            else:
                val[letter] = float(num)
        for g in gs:
            if g not in KNOWN_G:
                self.problem("info", "UNKNOWN_CODE", f"G{g} not modelled", once_key=f"G{g}")
        for m in ms:
            if m not in KNOWN_M:
                self.problem("info", "UNKNOWN_CODE", f"M{m} not modelled", once_key=f"M{m}")
        # modal state
        for g in gs:
            if g in (17, 18, 19):
                self.plane = g
            elif g == 20:
                self.unit = INCH
            elif g == 21:
                self.unit = 1.0
            elif g == 90:
                self.abs = True
            elif g == 91:
                self.abs = False
            elif g in (98, 99):
                self.retract_mode = g
        if "F" in val:
            self.feed = val["F"]
        if "T" in val:
            self.pending_tool = int(val["T"])
        for m in ms:
            if m == 6:
                self.tool = self.pending_tool
                self.cycle = None
                if self.tool is not None and self.expected is not None and self.tool not in self.expected:
                    self.problem("error", "TOOL_UNEXPECTED",
                                 f"T{self.tool} is not one of the setup's tools {sorted(self.expected)}")
            elif m in (3, 4):
                self.spindle = True
            elif m == 5:
                self.spindle = False
            elif m in (30, 2):
                self.ended = True
        if 43 in gs and "H" in val and self.tool is not None and int(val["H"]) != self.tool:
            self.problem("warning", "H_MISMATCH", f"G43 H{int(val['H'])} with T{self.tool} loaded")
        if 28 in gs:                        # reference return: moves away from the part
            self.pos = [None if "X" in val or "Y" in val else self.pos[0],
                        None if "X" in val or "Y" in val else self.pos[1], None]
            self.cycle = None
            return
        if 80 in gs:
            self.cycle = None
        cyc = [g for g in gs if g in CYCLES]
        if cyc:
            self.cycle = cyc[-1]
            self.init_z = self.pos[2]
        for g in gs:
            if g in (0, 1, 2, 3):
                self.mode = g
                self.cycle = None
        target = list(self.pos)
        moved = False
        for k, axis in enumerate("XYZ"):
            if axis in val:
                v = val[axis] * self.unit
                if self.abs or target[k] is None:
                    target[k] = v
                else:
                    target[k] += v
                moved = True
        if self.cycle is not None:
            if "R" in val:
                self.cycle_r = val["R"] * self.unit
            if "Z" in val:
                self.cycle_z = val["Z"] * self.unit
            if ("X" in val or "Y" in val or cyc) and target[0] is not None:
                self.do_cycle(target[0], target[1])
            return
        if not moved:
            return
        if self.mode == 0:
            self.move(target, rapid=True)
        elif self.mode == 1:
            self.cut_checks()
            self.move(target, rapid=False)
        elif self.mode in (2, 3):
            self.cut_checks()
            self.arc(target, val)

    # -- moves
    def cut_checks(self):
        if self.feed is None:
            self.problem("error", "FEED_MISSING", "cutting move with no F word set")
        if not self.spindle:
            self.problem("error", "SPINDLE_OFF", "cutting move with the spindle stopped (no M03/M04)")
        if self.tool is None:
            self.problem("error", "NO_TOOL", "cutting move before any tool change (T.. M06)")

    def do_cycle(self, x, y):
        self.cut_checks()
        z0 = self.pos[2] if self.pos[2] is not None else self.init_z
        r, zb = self.cycle_r, self.cycle_z
        if r is None or zb is None:
            self.problem("error", "CYCLE_INCOMPLETE", f"G{self.cycle} without R and Z")
            return
        self.move([x, y, z0], rapid=True)
        self.move([x, y, r], rapid=True)
        self.move([x, y, zb], rapid=False)
        self.move([x, y, r], rapid=True, retract=True)
        if self.retract_mode == 98 and z0 is not None:
            self.move([x, y, z0], rapid=True, retract=True)

    def move(self, target, rapid, retract=False):
        start = list(self.pos)
        self.pos = list(target)
        self.check_limits(target)
        if self.hm is None or None in target:
            return
        if None in start:
            start = list(target)
        dx, dy, dz = (target[i] - start[i] for i in range(3))
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        steps = max(1, int(math.ceil(dist / (self.hm.res * 0.5))))
        pts = [(start[0] + dx * s / steps, start[1] + dy * s / steps, start[2] + dz * s / steps)
               for s in range(steps + 1)]
        self.sweep(pts, rapid, retract)

    def arc(self, target, val):
        start = list(self.pos)
        if None in start or None in target:
            self.pos = target
            return
        a, b, c = {17: (0, 1, 2), 18: (2, 0, 1), 19: (1, 2, 0)}[self.plane]
        # (a, b) are right-handed in each plane: XY (G17), ZX (G18), YZ (G19), so G02 (clockwise seen
        # from the positive third axis) is always a negative sweep
        off = {0: val.get("I", 0.0) * self.unit, 1: val.get("J", 0.0) * self.unit, 2: val.get("K", 0.0) * self.unit}
        cw = self.mode == 2
        if "R" in val:
            rr = val["R"] * self.unit
            p, q = start[a], start[b]
            e, f = target[a], target[b]
            mx, my = (p + e) / 2, (q + f) / 2
            d = math.hypot(e - p, f - q)
            if d == 0 or abs(rr) < d / 2 - 1e-6:
                self.problem("error", "ARC_RADIUS", "R arc radius too small for its chord")
                self.pos = target
                return
            hgt = math.sqrt(max(rr * rr - (d / 2) ** 2, 0.0))
            ux, uy = (e - p) / d, (f - q) / d
            sgn = (1 if cw else -1) * (1 if rr > 0 else -1)
            ca, cb = mx + sgn * hgt * uy, my - sgn * hgt * ux
        else:
            ca, cb = start[a] + off[a], start[b] + off[b]
        r0 = math.hypot(start[a] - ca, start[b] - cb)
        r1 = math.hypot(target[a] - ca, target[b] - cb)
        if abs(r0 - r1) > 0.01:
            self.problem("error", "ARC_RADIUS", f"arc radii differ: start {r0:.4f}, end {r1:.4f} mm")
        t0 = math.atan2(start[b] - cb, start[a] - ca)
        t1 = math.atan2(target[b] - cb, target[a] - ca)
        sweep = t1 - t0
        if cw and sweep >= 0:
            sweep -= 2 * math.pi
        elif not cw and sweep <= 0:
            sweep += 2 * math.pi
        length = abs(sweep) * max(r0, 1e-9)
        steps = max(4, int(math.ceil(length / (self.hm.res * 0.5 if self.hm else 0.5))))
        pts = []
        for s in range(steps + 1):
            t = t0 + sweep * s / steps
            rr = r0 + (r1 - r0) * s / steps
            p = [0.0, 0.0, 0.0]
            p[a] = ca + rr * math.cos(t)
            p[b] = cb + rr * math.sin(t)
            p[c] = start[c] + (target[c] - start[c]) * s / steps
            pts.append(tuple(p))
            self.check_limits(p)
        self.pos = list(target)
        if self.hm is not None:
            self.sweep(pts, rapid=False, retract=False)

    def sweep(self, pts, rapid, retract):
        tool = self.tools.get(self.tool) if self.tool is not None else None
        st = self.stats.setdefault(self.tool, {"min_z": None, "cut_mm": 0.0, "rapid_mm": 0.0, "removed_mm3": 0.0})
        for i, (x, y, z) in enumerate(pts):
            st["min_z"] = z if st["min_z"] is None else min(st["min_z"], z)
            if i:
                seg = math.dist(pts[i - 1], (x, y, z))
                st["rapid_mm" if rapid else "cut_mm"] += seg
        if tool is None:
            if self.tool is not None:
                self.problem("warning", "TOOL_GEOMETRY", f"no diameter known for T{self.tool} (no tool comment); "
                             "material removal not simulated for it", once_key=f"T{self.tool}")
            return
        before = self.hm.removed
        worst = 0.0
        (sx0, sy0, _), (sx1, sy1, stop) = self.stock
        for x, y, z in pts:
            hit = self.hm.plunge(tool, x, y, z)
            worst = max(worst, hit)
            if (rapid and not retract and hit <= self.collision_tol and z < stop + self.clearance
                    and sx0 - tool.r < x < sx1 + tool.r and sy0 - tool.r < y < sy1 + tool.r):
                self.problem("warning", "RAPID_BELOW_TOP",
                             f"rapid at Z{z:.3f} below stock top {stop:g} + clearance {self.clearance:g} "
                             "inside the stock footprint (through air, material already removed)")
        st["removed_mm3"] += self.hm.removed - before
        if rapid and worst > self.collision_tol:
            self.problem("error", "RAPID_INTO_STOCK",
                         f"rapid move cuts {worst:.3f} mm into material with T{self.tool} (D{tool.d:g})")
        if self.stock and pts:
            zmin = min(p[2] for p in pts)
            if zmin < self.stock[0][2] - 1e-6 and not rapid:
                self.problem("info", "BELOW_STOCK", f"T{self.tool} tip reaches Z{zmin:.3f}, "
                             f"{self.stock[0][2] - zmin:.3f} mm below the stock bottom (fixture/spoilboard)",
                             once_key=f"below-T{self.tool}")

    def check_limits(self, p):
        if not self.limits or None in p:
            return
        lo, hi = self.limits
        for k, axis in enumerate("XYZ"):
            if not lo[k] - 1e-6 <= p[k] <= hi[k] + 1e-6:
                self.problem("error", "ENVELOPE", f"{axis}{p[k]:.3f} outside the limits [{lo[k]:g}, {hi[k]:g}]")
                return

    def report(self, paths):
        order = {"error": 0, "warning": 1, "info": 2}
        into = {(p["file"], p["line"]) for p in self.problems if p["code"] == "RAPID_INTO_STOCK"}
        probs = [p for p in self.problems if not (p["code"] == "RAPID_BELOW_TOP" and (p["file"], p["line"]) in into)]
        probs = sorted(probs, key=lambda p: (order[p["severity"]], p["file"], p["line"]))
        counts = {s: sum(1 for p in probs if p["severity"] == s) for s in order}
        out = {"files": [os.path.abspath(p) for p in paths], "ok": counts["error"] == 0, "counts": counts,
               "problems": probs, "units": "mm (WCS)",
               "tools": {f"T{n}": t.as_dict() for n, t in sorted(self.tools.items())},
               "per_tool": {f"T{n}": {k: round(v, 3) if isinstance(v, float) else v for k, v in s.items()}
                            for n, s in self.stats.items() if n is not None}}
        if self.hm:
            (x0, y0, z0), (x1, y1, z1) = self.stock
            out["stock_wcs_mm"] = {"min": [x0, y0, z0], "max": [x1, y1, z1]}
            out["stock_volume_mm3"] = round((x1 - x0) * (y1 - y0) * (z1 - z0), 1)
            out["removed_volume_mm3"] = round(self.hm.removed, 1)
            out["remaining_volume_mm3"] = round(out["stock_volume_mm3"] - self.hm.removed, 1)
            out["simulation"] = f"height map, {self.hm.res:g} mm cells ({self.hm.nx}x{self.hm.ny})"
        return out


def find_camotics():
    """Path to camsim (CAMotics' command-line simulator), or None. CAMOTICS_HOME overrides."""
    cands = []
    home = os.environ.get("CAMOTICS_HOME")
    if home:
        cands += [os.path.join(home, "camsim.exe"), os.path.join(home, "camsim")]
    cands.append(shutil.which("camsim"))
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        if base:
            cands.append(os.path.join(base, "CAMotics", "camsim.exe"))
    return next((c for c in cands if c and os.path.isfile(c)), None)


def camotics_version(camsim):
    """camsim --version prints 0.0 on 1.2.0, so read the version from the install's CHANGELOG."""
    try:
        with open(os.path.join(os.path.dirname(camsim), "CHANGELOG.md"), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"##\s*v?([\d.]+)", line.strip())
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def camotics_status() -> dict:
    camsim = find_camotics()
    if camsim:
        return {"installed": True, "path": camsim, "version": camotics_version(camsim)}
    return {"installed": False, "note": "CAMotics not found (install it, or set CAMOTICS_HOME); "
                                        "only the built-in height-map simulation was run"}


def stl_volume(path) -> float:
    """Volume of a closed binary or ASCII STL (divergence theorem)."""
    import struct
    with open(path, "rb") as f:
        data = f.read()
    if data[:5] == b"solid" and b"facet" in data[:512]:
        verts = [tuple(float(v) for v in m.groups())
                 for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
        tris = [verts[i:i + 3] for i in range(0, len(verts) - 2, 3)]
    else:
        n = struct.unpack_from("<I", data, 80)[0]
        tris = []
        for k in range(n):
            f = struct.unpack_from("<12f", data, 84 + 50 * k)
            tris.append((f[3:6], f[6:9], f[9:12]))
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
    return abs(v)


def _camotics_tool(t) -> dict:
    # Bull-nose corners and drill points are modelled as cylinders (slight over-cut at the tip;
    # through holes and profiles are unaffected).
    shape = "ballnose" if t.kind() == "ball" else "cylindrical"
    return {"units": "metric", "shape": shape, "diameter": t.d, "length": max(30.0, 4 * t.d),
            "description": f"T{t.number} {t.kind()}"}


_PECK = re.compile(r"\bG0?(83|73)\b")
_QWORD = re.compile(r"\s*Q\s*[-+]?(?:\d+\.?\d*|\.\d+)")


def camotics_copy(src, dst) -> int:
    """camsim 1.2.0 skips G83/G73 ('Not implemented: G83 (Peck Drilling)'). Write G81 instead (same
    material removed) and drop Q. Line numbers are kept, so its warnings point at the original lines."""
    changed = 0
    with open(src, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    out = []
    for line in lines:
        if _PECK.search(re.sub(r"\([^)]*\)", "", line).upper()):
            line = _QWORD.sub("", _PECK.sub("G81", line.upper()))
            changed += 1
        out.append(line)
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return changed


def run_camotics(paths, stock, tools, res=0.25, timeout=300) -> dict:
    """Simulate the programs in CAMotics (camsim) on the stock box; return its removed volume.

    camsim writes the cut workpiece as an STL whose surface sits slightly outside the nominal box
    (82x52x11 measures 48398 mm3 uncut at 0.25 mm), so removal is measured against an empty program
    on the same box and resolution, run in parallel. camsim reports no collisions; its warnings
    (file:line) are returned."""
    import subprocess
    import tempfile
    import time as _time
    camsim = find_camotics()
    if not camsim:
        return camotics_status()
    tdir = tempfile.mkdtemp(prefix="fusionmcp_camotics_")
    try:
        files, rewritten, names = [], 0, {}
        for i, p in enumerate(paths):
            dst = os.path.join(tdir, f"{i:02d}_{os.path.basename(p)}")
            rewritten += camotics_copy(p, dst)
            files.append(dst)
            names[os.path.basename(dst)] = os.path.basename(p)
        empty = os.path.join(tdir, "empty.nc")
        with open(empty, "w") as f:
            f.write("%\nG90 G21\nM30\n%\n")
        (x0, y0, z0), (x1, y1, z1) = stock
        tool_table = {str(n): _camotics_tool(t) for n, t in sorted(tools.items())} or \
            {"1": {"units": "metric", "shape": "cylindrical", "diameter": 1.0, "length": 10.0}}

        def project(name, flist):
            proj = {"units": "metric", "resolution-mode": "manual", "resolution": res, "tools": tool_table,
                    "workpiece": {"automatic": False, "margin": 0,
                                  "bounds": {"min": [x0, y0, z0], "max": [x1, y1, z1]}},
                    "files": [f.replace("\\", "/") for f in flist]}
            pp = os.path.join(tdir, name + ".camotics")
            with open(pp, "w") as f:
                json.dump(proj, f)
            return pp

        t0 = _time.time()
        procs = {}
        for name, flist in (("job", files), ("empty", [empty])):
            stl = os.path.join(tdir, name + ".stl")
            procs[name] = (subprocess.Popen([camsim, "--binary", project(name, flist), stl],
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)), stl)
        logs, vols = {}, {}
        for name, (pr, stl) in procs.items():
            try:
                out, _ = pr.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                for q, _s in procs.values():
                    q.kill()
                return {"installed": True, "path": camsim, "ran": False, "error": f"camsim timed out after {timeout}s"}
            logs[name] = out or ""
            if pr.returncode != 0 or not os.path.exists(stl):
                return {"installed": True, "path": camsim, "ran": False,
                        "error": f"camsim exit {pr.returncode}: {(out or '')[-500:]}"}
            vols[name] = stl_volume(stl)
        warnings, offsets = [], 0
        for line in logs["job"].splitlines():
            m = re.match(r"WARNING:(.*):(\d+):(.*)", line.strip())
            if not m:
                continue
            if "Tool Length Offset not supported" in m.group(3):
                offsets += 1
                continue
            base = os.path.basename(m.group(1).replace("/", os.sep))
            warnings.append({"file": names.get(base, base), "line": int(m.group(2)), "message": m.group(3).strip()})
        removed = vols["empty"] - vols["job"]
        stock_vol = (x1 - x0) * (y1 - y0) * (z1 - z0)
        out = {"installed": True, "path": camsim, "version": camotics_version(camsim), "ran": True,
               "resolution_mm": res, "seconds": round(_time.time() - t0, 1),
               "removed_volume_mm3": round(removed, 1), "remaining_volume_mm3": round(stock_vol - removed, 1),
               "warnings": warnings,
               "notes": ["CAMotics reports no collisions: RAPID_INTO_STOCK comes from the built-in simulation",
                         "drill points and bull-nose corners are modelled as cylinders in CAMotics"]}
        if rewritten:
            out["notes"].append(f"{rewritten} G83/G73 peck line(s) simulated as G81 (camsim 1.2 skips G83)")
        if offsets:
            out["notes"].append(f"{offsets} 'Tool Length Offset not supported' warning(s) ignored")
        return out
    finally:
        shutil.rmtree(tdir, ignore_errors=True)


def check(paths, stock=None, limits=None, expected_tools=None, clearance=0.0, res=0.5, tools=None,
          camotics=True, camotics_res=0.25) -> dict:
    """paths: .nc files in machining order. stock / limits: ((xmin, ymin, zmin), (xmax, ymax, zmax)) in WCS mm.
    tools: optional {number: {"diameter_mm", "corner_radius_mm", "tip_angle_deg"}} overriding the post comments.
    camotics: also simulate in CAMotics when it is installed and a stock box is given."""
    if isinstance(paths, str):
        paths = [paths]
    for p in paths:
        if not os.path.isfile(p):
            raise FileNotFoundError(p)
    tl = {}
    for n, t in (tools or {}).items():
        tl[int(n)] = Tool(int(n), t["diameter_mm"], t.get("corner_radius_mm", 0.0), t.get("tip_angle_deg"))
    ck = Checker(_box(stock), _box(limits), expected_tools, clearance, res, tools=tl)
    r = ck.run(paths)
    if not camotics or ck.stock is None:
        return r
    if not find_camotics():
        r["camotics"] = camotics_status()
        return r
    cr = run_camotics(paths, ck.stock, ck.tools, camotics_res)
    r["camotics"] = cr
    if cr.get("ran") and r.get("removed_volume_mm3"):
        b, c = r["removed_volume_mm3"], cr["removed_volume_mm3"]
        diff = round((c - b) / b * 100, 2)
        r["simulation_comparison"] = {"builtin_removed_mm3": b, "camotics_removed_mm3": c,
                                      "difference_pct": diff, "agree_within_5pct": abs(diff) <= 5}
        if abs(diff) > 5:
            r["problems"].append({"severity": "warning", "code": "SIM_DISAGREE", "file": "", "line": 0, "n": None,
                                  "text": "", "message": f"built-in and CAMotics removed volumes differ by {diff} %"})
            r["counts"]["warning"] += 1
    return r


def _box(b):
    if b is None:
        return None
    if isinstance(b, dict):
        b = (b["min"], b["max"])
    if len(b) == 6:
        b = (b[:3], b[3:])
    lo, hi = (tuple(float(v) for v in b[0]), tuple(float(v) for v in b[1]))
    if len(lo) != 3 or len(hi) != 3:
        raise ValueError("boxes need 3 min and 3 max values")
    return (lo, hi)


def _floats(s):
    return [float(v) for v in s.split(",")]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+", help=".nc files in machining order")
    ap.add_argument("--stock", type=_floats, help="xmin,ymin,zmin,xmax,ymax,zmax in WCS mm")
    ap.add_argument("--limits", type=_floats, help="axis limits xmin,ymin,zmin,xmax,ymax,zmax in WCS mm")
    ap.add_argument("--tools", type=lambda s: [int(v) for v in s.split(",")], help="expected tool numbers")
    ap.add_argument("--clearance", type=float, default=0.0)
    ap.add_argument("--res", type=float, default=0.5)
    ap.add_argument("--no-camotics", action="store_true", help="skip CAMotics even if it is installed")
    a = ap.parse_args(argv)
    r = check(a.files, a.stock, a.limits, a.tools, a.clearance, a.res, camotics=not a.no_camotics)
    print(json.dumps(r, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
