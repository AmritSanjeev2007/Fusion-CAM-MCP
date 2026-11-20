"""Cutting tools from JSON libraries. Runs on Fusion's main thread.

Sources:
- "samples" (default): Fusion's bundled libraries, <install>/Applications/CAM360/Data/libraries/
  Samples/*.json. No library API, no cloud: the safe path (Phase 0 run 1 crashed in the
  cloud-library path; run 3 machined with a tool made from this JSON).
- a path to a .json tool library exported from Fusion (same format: {"data": [tool, ...]}).
- "cloud": Fusion's online library query. Opt-in only: slow (7.7-8.3 s in Phase 0) and
  implicated in the run 1 crash. Results are cached per session.

A tool's id is its library guid. cam_add_operation looks the id up in the index built here.
"""

import json
import os
import sys

import adsk.cam
import adsk.core

from . import camcalc, guards, trace

_index: dict = {}           # guid -> (library name, raw entry)
_loaded: set = set()        # library files already indexed


def samples_dir() -> str:
    # sys.executable is Fusion360.exe in the install root (Phase 0 finding), not python.exe
    return os.path.join(os.path.dirname(sys.executable), "Applications", "CAM360", "Data", "libraries", "Samples")


def _load_file(path: str, label: str | None = None) -> list:
    path = os.path.abspath(path)
    label = label or os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entries = data.get("data") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise guards.OpError(f"{path} is not a Fusion tool library (expected {{\"data\": [...]}})")
    out = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("guid") or e.get("type") in ("holder", None):
            continue
        _index[e["guid"]] = (label, e)
        out.append(camcalc.tool_summary(e, label))
    _loaded.add(path)
    return out


def sample_libraries() -> list:
    d = samples_dir()
    if not os.path.isdir(d):
        raise guards.OpError(f"Fusion's sample tool libraries were not found at {d}")
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


def ensure_samples(library: str | None = None) -> list:
    """Index the sample libraries (all, or one by name) and return their tool summaries."""
    libs = sample_libraries()
    if library:
        match = [l for l in libs if l.lower() == library.lower()] or [l for l in libs if library.lower() in l.lower()]
        if not match:
            raise guards.OpError(f"no sample library matching '{library}'. Available: {libs}")
        libs = match
    out = []
    for l in libs:
        out += _load_file(os.path.join(samples_dir(), l + ".json"), l)
    return out


def _cloud(query_text, tool_type, dmin, dmax) -> list:
    libs = adsk.cam.CAMManager.get().libraryManager.toolLibraries
    trace.step("toolLibraries.createQuery(cloud)")
    q = libs.createQuery(adsk.cam.LibraryLocations.Fusion360LibraryLocation)
    if tool_type:
        q.criteria.add("tool_type", adsk.core.ValueInput.createByString(tool_type))
    trace.step("toolLibraries query.execute")
    hits = q.execute()
    out = []
    for h in hits:
        tool = h.tool if hasattr(h, "tool") else h
        try:
            e = json.loads(tool.toJson())
        except Exception:
            continue
        if e.get("guid"):
            _index[e["guid"]] = ("cloud", e)
            out.append(camcalc.tool_summary(e, "cloud"))
    return out


def cam_list_tools(args: dict):
    source = args.get("source") or "samples"
    tool_type = args.get("type")
    dmin, dmax = args.get("diameter_min_mm"), args.get("diameter_max_mm")
    text = args.get("text")
    limit = int(args.get("limit") or 40)
    if source == "samples":
        if not args.get("library") and not (tool_type or dmin is not None or dmax is not None or text):
            return {"source": samples_dir(), "libraries": sample_libraries(),
                    "hint": "pass library and/or type, diameter_min_mm/diameter_max_mm, text to list tools"}
        tools = ensure_samples(args.get("library"))
    elif source == "cloud":
        tools = _cloud(text, tool_type, dmin, dmax)
    else:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(source)))
        if not os.path.isfile(path):
            raise guards.OpError(f"source must be 'samples', 'cloud' or a tool library .json path; not found: {path}")
        tools = _load_file(path)
    hits = camcalc.filter_tools(tools, tool_type, dmin, dmax, text)
    out = {"source": source, "count": len(hits), "tools": hits[:limit], "units": "mm"}
    if len(hits) > limit:
        out["truncated"] = f"showing {limit} of {len(hits)}; narrow with type/diameter/text or raise limit"
    return out


def find_tool(tool_id: str):
    """(library, raw entry) for a tool id; indexes the sample libraries on first use."""
    if tool_id not in _index:
        ensure_samples()
    if tool_id not in _index:
        raise guards.OpError(f"unknown tool_id '{tool_id}'. List tools first with cam_list_tools "
                             "(samples, a library .json path, or source='cloud').")
    return _index[tool_id]


def make_tool(tool_id: str, number: int):
    lib, entry = find_tool(tool_id)
    trace.step(f"Tool.createFromJson {tool_id} T{number}")
    tool = adsk.cam.Tool.createFromJson(json.dumps(camcalc.prepare_tool_json(entry, number)))
    if tool is None:
        raise guards.OpError(f"Fusion could not create tool {tool_id} from JSON")
    return tool, lib, entry
