"""Phase 0 spike for the custom Fusion MCP (run 2: crash-safe).

Run from Fusion: Shift+S -> Scripts -> FusionMCP_Phase0 -> Run.

It creates its own scratch designs, never edits or saves documents you already had open,
closes everything it created, and writes:
  docs/phase0-results.json   results, rewritten after every step
  docs/phase0-trace.log      BEGIN/END line per step, flushed to disk before each API call

Crash safety: if Fusion dies, the trace shows which step was running (BEGIN with no END).
On the next run that step is skipped automatically (and any step that needs its output),
so each rerun gets further. Delete docs/phase0-trace.log to retry everything.

Run 1 crashed the main thread inside an adsk.cam call (log: CAMXLayer10 tool-library
code, after a burst of cloud library downloads). So the CAM pipeline now uses a tool built
from Fusion's bundled sample-library JSON file and a post looked up by URL, and the
cloud tool-library query/scan run last, after everything else has been saved.
"""

import adsk.core
import adsk.fusion
import adsk.cam
import json
import os
import socket
import socketserver
import sys
import threading
import time
import traceback

REPO = r"C:\Fusion MCP"
OUT_DIR = os.path.join(REPO, "docs")
RESULTS = os.path.join(OUT_DIR, "phase0-results.json")
TRACE = os.path.join(OUT_DIR, "phase0-trace.log")
TRACE_PREV = os.path.join(OUT_DIR, "phase0-trace.prev.log")
NC_DIR = os.path.join(REPO, "test_output", "phase0_nc")
PORT = 27190
EVENT_ID = "FusionMCP_Phase0_Event"
# Inside Fusion sys.executable is Fusion360.exe (in the install root), not Python\python.exe.
INSTALL = next(d for d in (os.path.dirname(sys.executable), os.path.dirname(os.path.dirname(sys.executable)))
               if os.path.isdir(os.path.join(d, "Applications", "CAM360")))
SAMPLE_TOOLS = os.path.join(INSTALL, "Applications", "CAM360", "Data", "libraries", "Samples",
                            "Milling Tools (Metric).json")

app = adsk.core.Application.get()
ui = app.userInterface

results = {"run_started": time.strftime("%Y-%m-%d %H:%M:%S"), "checks": {}}
ctx = {}                # outputs passed between steps
_handlers = []
_created_docs = []
_crashed_before = set()


# ---------------------------------------------------------------- trace / results
def trace(line):
    with open(TRACE, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        f.flush()
        os.fsync(f.fileno())


def load_previous_crash():
    """Steps that BEGAN but never ENDED in the previous run crashed Fusion."""
    if not os.path.exists(TRACE):
        return set()
    begun, ended, crashed = [], set(), set()
    for line in open(TRACE, encoding="utf-8"):
        parts = line.strip().split(" ", 2)
        if len(parts) < 3:
            continue
        if parts[1] == "BEGIN":
            begun.append(parts[2])
        elif parts[1] == "END":
            ended.add(parts[2].split(" ")[0])
        elif parts[1] == "SKIP-CRASHED":
            crashed.add(parts[2])
    crashed |= {s for s in begun if s not in ended}
    os.replace(TRACE, TRACE_PREV)
    return crashed


def write_results():
    os.makedirs(OUT_DIR, exist_ok=True)
    results["last_write"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = RESULTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    os.replace(tmp, RESULTS)


def step(key, fn, needs=()):
    """Run one check: skip if it crashed Fusion last time or a prerequisite is missing."""
    if key in _crashed_before:
        trace(f"SKIP-CRASHED {key}")
        results["checks"][key] = {"ok": False, "skipped": "crashed Fusion on a previous run"}
        write_results()
        return False
    missing = [n for n in needs if n not in ctx]
    if missing:
        results["checks"][key] = {"ok": False, "skipped": f"needs {missing}"}
        write_results()
        return False
    trace(f"BEGIN {key}")
    try:
        detail = fn() or {}
        results["checks"][key] = {"ok": True, **detail}
        trace(f"END {key} ok")
        ok = True
    except Exception as e:
        results["checks"][key] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
        trace(f"END {key} fail: {e}")
        ok = False
    print(f"[Phase0] {key}: {'OK' if ok else 'FAIL'}")
    write_results()
    return ok


def new_design():
    doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    _created_docs.append(doc)
    design = adsk.fusion.Design.cast(app.activeProduct)
    design.designType = adsk.fusion.DesignTypes.ParametricDesignType
    design.fusionUnitsManager.distanceDisplayUnits = adsk.fusion.DistanceUnits.MillimeterDistanceUnits
    return doc, design


def box(root, x_cm, y_cm, h_expr):
    sk = root.sketches.add(root.xYConstructionPlane)
    P = adsk.core.Point3D.create
    sk.sketchCurves.sketchLines.addTwoPointRectangle(P(0, 0, 0), P(x_cm, y_cm, 0))
    ext = root.features.extrudeFeatures.addSimple(
        sk.profiles.item(0), adsk.core.ValueInput.createByString(h_expr),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    return ext.bodies.item(0)


# ---------------------------------------------------------------- basic checks
def c_versions():
    return {"python_version": sys.version, "python_executable": sys.executable,
            "fusion_version": app.version, "install_dir": INSTALL,
            "open_documents_before": [app.documents.item(i).name for i in range(app.documents.count)],
            "skipped_because_crashed_before": sorted(_crashed_before)}


def c_units():
    doc, design = new_design()
    ctx["units_doc"] = doc
    body = box(design.rootComponent, 1.0, 1.0, "10 mm")
    vol = body.physicalProperties.volume
    if abs(vol - 1.0) > 1e-6:
        raise RuntimeError(f"volume {vol} != 1.0 cm^3")
    return {"volume_api_cm3": vol}


def c_screenshot():
    app.activeViewport.fit()
    path = os.path.join(OUT_DIR, "phase0-screenshot.png")
    if not app.activeViewport.saveAsImageFile(path, 800, 600) or not os.path.exists(path):
        raise RuntimeError("saveAsImageFile failed")
    return {"path": path, "bytes": os.path.getsize(path)}


# ---------------------------------------------------------------- CAM pipeline (no cloud libraries)
STEP_PATH = os.path.join(REPO, "test_output", "phase0_block.step")


def c_step_export():
    """Stand-in for the Inventor side: build a 50x40x20 mm block and export it to STEP."""
    doc, design = new_design()
    box(design.rootComponent, 5.0, 4.0, "20 mm")
    os.makedirs(os.path.dirname(STEP_PATH), exist_ok=True)
    if os.path.exists(STEP_PATH):
        os.remove(STEP_PATH)
    em = design.exportManager
    ok = em.execute(em.createSTEPExportOptions(STEP_PATH, design.rootComponent))
    if not ok or not os.path.exists(STEP_PATH):
        raise RuntimeError("STEP export failed")
    ctx["step_file"] = STEP_PATH
    return {"path": STEP_PATH, "bytes": os.path.getsize(STEP_PATH)}


def c_cam_doc():
    """The real workflow: import the STEP into a NEW document and machine the imported body."""
    opts = app.importManager.createSTEPImportOptions(ctx["step_file"])
    doc = app.importManager.importToNewDocument(opts)
    if doc is None:
        raise RuntimeError("importToNewDocument returned None")
    _created_docs.append(doc)
    ctx["cam_doc"] = doc
    design = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
    bodies = [b for occ_bodies in [design.rootComponent.bRepBodies] +
              [o.bRepBodies for o in design.rootComponent.allOccurrences] for b in occ_bodies]
    if not bodies:
        raise RuntimeError("imported document has no bodies")
    body = bodies[0]
    ctx["cam_body"] = body
    bb = body.boundingBox
    size_mm = [round((bb.maxPoint.x - bb.minPoint.x) * 10, 4), round((bb.maxPoint.y - bb.minPoint.y) * 10, 4),
               round((bb.maxPoint.z - bb.minPoint.z) * 10, 4)]
    vol_mm3 = round(body.physicalProperties.volume * 1000, 3)
    if abs(vol_mm3 - 40000) > 1:
        raise RuntimeError(f"imported volume {vol_mm3} mm^3 != 40000")
    return {"bodies": len(bodies), "size_mm": size_mm, "volume_mm3": vol_mm3,
            "design_type": "direct" if design.designType == adsk.fusion.DesignTypes.DirectDesignType else "parametric"}


def c_cam_workspace():
    ws = ui.workspaces.itemById("CAMEnvironment")
    ids = None
    if ws is None:
        ids = [ui.workspaces.item(i).id for i in range(ui.workspaces.count)]
        ws = next((ui.workspaces.itemById(i) for i in ids if "cam" in i.lower()), None)
        if ws is None:
            raise RuntimeError(f"no CAM workspace among {ids}")
    ws.activate()
    cam = adsk.cam.CAM.cast(ctx["cam_doc"].products.itemByProductType("CAMProductType"))
    if cam is None:
        raise RuntimeError("no CAM product after activating the workspace")
    ctx["cam"] = cam
    return {"workspace_id": ws.id, "all_ids": ids}


def c_cam_setup():
    cam = ctx["cam"]
    si = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
    si.models = [ctx["cam_body"]]
    ctx["setup"] = cam.setups.add(si)
    return {"setup": ctx["setup"].name}


def c_cam_strategies():
    names = [s.name for s in ctx["setup"].operations.compatibleStrategies]
    ctx["strategies"] = names
    return {"count": len(names), "strategies": names}


def c_cam_tool_from_json():
    """Tool from Fusion's bundled sample library FILE: no library API, no cloud."""
    data = json.load(open(SAMPLE_TOOLS, encoding="utf-8"))["data"]
    mills = [t for t in data if t.get("type") == "flat end mill" and t.get("unit") == "millimeters"]
    t = min(mills, key=lambda t: abs(t["geometry"].get("DC", 0) - 6))
    tool = adsk.cam.Tool.createFromJson(json.dumps(t))
    ctx["tool"] = tool
    return {"source": SAMPLE_TOOLS, "description": t.get("description"), "DC_mm": t["geometry"].get("DC")}


def c_cam_operation():
    strategy = "face" if "face" in ctx["strategies"] else ctx["strategies"][0]
    oi = ctx["setup"].operations.createInput(strategy)
    oi.tool = ctx["tool"]
    ctx["op"] = ctx["setup"].operations.add(oi)
    return {"strategy": strategy, "name": ctx["op"].name}


def c_cam_generate():
    t0 = time.time()
    fut = ctx["cam"].generateToolpath(ctx["op"])
    while not fut.isGenerationCompleted and time.time() - t0 < 120:
        adsk.doEvents()
        time.sleep(0.05)
    op = ctx["op"]
    if not fut.isGenerationCompleted:
        raise RuntimeError("generation not complete after 120 s")
    ctx["generated"] = True
    return {"seconds": round(time.time() - t0, 2), "has_toolpath": op.hasToolpath,
            "is_valid": op.isToolpathValid}


def c_cam_check():
    return {"checkToolpath": ctx["cam"].checkToolpath(ctx["op"])}


def c_cam_time():
    mt = ctx["cam"].getMachiningTime(ctx["op"], 100, 5000 / 600, 15)    # rapid 5000 mm/min in cm/s
    return {"total_s": mt.machiningTime, "feed_dist_cm": mt.feedDistance,
            "rapid_dist_cm": mt.rapidDistance, "tool_changes": mt.toolChangeCount}


def c_cam_post_lookup():
    """One post by URL (fanuc.cps ships with Fusion), not a library enumeration."""
    pl = adsk.cam.CAMManager.get().libraryManager.postLibrary
    base = pl.urlByLocation(adsk.cam.LibraryLocations.Fusion360LibraryLocation)
    url = base.join("fanuc.cps")
    post = pl.postConfigurationAtURL(url)
    if post is None:
        raise RuntimeError(f"no post at {url.toString()}")
    ctx["post"] = post
    return {"url": url.toString(), "description": post.description, "vendor": post.vendor}


def c_cam_post():
    os.makedirs(NC_DIR, exist_ok=True)
    ni = ctx["cam"].ncPrograms.createInput()
    ni.displayName = "Phase0 NC"
    found = {}
    for name, val in (("nc_program_filename", "phase0"), ("nc_program_output_folder", NC_DIR),
                      ("nc_program_openInEditor", False)):
        p = ni.parameters.itemByName(name)
        found[name] = p is not None
        if p:
            p.value.value = val
    ni.operations = [ctx["op"]]
    nc = ctx["cam"].ncPrograms.add(ni)
    nc.postConfiguration = ctx["post"]

    # Run 3: the filename was honoured but the folder was not (file went to
    # Documents\Fusion 360\NC Programs). Read back what Fusion holds, then set it again on the
    # created NC program and record which form sticks.
    def readback():
        out = {}
        for n in ("nc_program_output_folder", "nc_program_default_output_folder",
                  "nc_program_outputPathState", "nc_program_filename"):
            p = nc.parameters.itemByName(n)
            if p is not None:
                try:
                    out[n] = {"value": str(p.value.value), "expression": p.expression,
                              "editable": p.isEditable, "enabled": p.isEnabled, "error": p.error}
                except Exception as e:
                    out[n] = {"error": str(e)}
        return out
    before = readback()
    p = nc.parameters.itemByName("nc_program_output_folder")
    set_attempts = {}
    for label, fn in (("value", lambda: setattr(p.value, "value", NC_DIR)),
                      ("expression", lambda: setattr(p, "expression", "'" + NC_DIR.replace("\\", "/") + "'"))):
        try:
            fn()
            set_attempts[label] = str(p.value.value)
        except Exception as e:
            set_attempts[label] = f"error: {e}"
    after = readback()

    ok = nc.postProcess(adsk.cam.NCProgramPostProcessOptions.create())
    files = os.listdir(NC_DIR)
    # where did it actually go? (the add-in will locate and move the file if the folder isn't honoured)
    default_dir = os.path.join(os.path.expanduser("~"), "OneDrive", "Documents", "Fusion 360", "NC Programs")
    elsewhere = [os.path.join(d, f) for d in (default_dir, after.get("nc_program_output_folder", {}).get("value", ""))
                 if d and os.path.isdir(d) for f in os.listdir(d) if f.startswith("phase0")]
    detail = {"returned": ok, "params_found": found, "readback_before": before,
              "set_attempts": set_attempts, "readback_after": after, "files_in_target": files,
              "files_elsewhere": elsewhere}
    if not files:
        raise RuntimeError(f"postProcess returned {ok} but no file in {NC_DIR}: {json.dumps(detail, default=str)}")
    return detail


# ---------------------------------------------------------------- cloud tool library (riskiest, last)
def c_lib_query():
    libs = adsk.cam.CAMManager.get().libraryManager.toolLibraries
    q = libs.createQuery(adsk.cam.LibraryLocations.Fusion360LibraryLocation)
    q.criteria.add("tool_type", adsk.core.ValueInput.createByString("flat end mill"))
    t0 = time.time()
    hits = q.execute()
    return {"hits": len(hits), "seconds": round(time.time() - t0, 1)}


def c_lib_scan():
    libs = adsk.cam.CAMManager.get().libraryManager.toolLibraries
    url = libs.urlByLocation(adsk.cam.LibraryLocations.Fusion360LibraryLocation)
    t0 = time.time()
    children = libs.childAssetURLs(url)
    first = libs.toolLibraryAtURL(children[0]) if children else None
    return {"libraries": [c.toString() for c in children][:20],
            "first_library_tools": len(first) if first is not None else None,
            "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- socket + custom event
class _Echo(socketserver.StreamRequestHandler):
    def handle(self):
        msg = json.loads(self.rfile.readline())
        self.wfile.write(json.dumps({"echo": msg, "thread": threading.current_thread().name}).encode() + b"\n")


def socket_test():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), _Echo)
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, name="phase0-socket", daemon=True)
    th.start()
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as c:
        c.sendall(b'{"op": "ping"}\n')
        reply = c.makefile().readline()
    srv.shutdown()
    srv.server_close()
    th.join(5)
    return {"reply": json.loads(reply), "server_thread_stopped": not th.is_alive()}


class _Handler(adsk.core.CustomEventHandler):
    def notify(self, args):
        def c_event():
            info = json.loads(args.additionalInfo)
            on_main = threading.get_ident() == ctx["main_ident"]
            design = adsk.fusion.Design.cast(ctx["units_doc"].products.itemByProductType("DesignProductType"))
            root = design.rootComponent
            n = root.sketches.count
            root.sketches.add(root.xZConstructionPlane)
            if not on_main:
                raise RuntimeError("handler was NOT on the main thread")
            return {"fired_from": info["thread"], "handler_on_main_thread": on_main,
                    "latency_ms": round((time.time() - info["t"]) * 1000, 1),
                    "sketch_created": root.sketches.count == n + 1}
        try:
            step("2_custom_event", c_event, needs=("units_doc",))
        finally:
            ctx["event_done"].set()
            finish()


def worker():
    # background thread: never touches adsk.* objects except fireCustomEvent
    try:
        results["checks"]["3_socket_thread"] = {"ok": True, **socket_test()}
    except Exception as e:
        results["checks"]["3_socket_thread"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
    trace(f"END 3_socket_thread {'ok' if results['checks']['3_socket_thread']['ok'] else 'fail'}")
    app.fireCustomEvent(EVENT_ID, json.dumps({"thread": threading.current_thread().name, "t": time.time()}))
    if not ctx["event_done"].wait(20):
        results["checks"]["2_custom_event"] = {"ok": False, "error": "handler did not run within 20 s"}
        write_results()


# ---------------------------------------------------------------- finish (main thread)
def finish():
    try:
        # riskiest checks last: everything above is already on disk if these crash Fusion
        step("7_lib_query", c_lib_query)
        step("8_lib_scan", c_lib_scan)
        try:
            ui.workspaces.itemById("FusionSolidEnvironment").activate()
        except Exception:
            pass
        for d in reversed(_created_docs):
            try:
                d.close(False)          # WITHOUT saving; only documents this script created
            except Exception as e:
                results.setdefault("cleanup_errors", []).append(str(e))
        if ctx.get("orig_doc") is not None:
            try:
                ctx["orig_doc"].activate()
            except Exception:
                pass
        app.unregisterCustomEvent(EVENT_ID)
    finally:
        results["run_finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_results()
        trace("RUN COMPLETE")
        ok = sum(1 for c in results["checks"].values() if c.get("ok"))
        ui.messageBox(f"Phase 0 finished: {ok}/{len(results['checks'])} checks passed.\n\n{RESULTS}",
                      "FusionMCP Phase 0")
        adsk.terminate()


# ---------------------------------------------------------------- entry point
def run(context):
    global _crashed_before
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        _crashed_before = load_previous_crash()
        trace("RUN START")
        ctx["main_ident"] = threading.get_ident()
        ctx["event_done"] = threading.Event()
        ctx["orig_doc"] = app.activeDocument

        step("1_versions", c_versions)
        step("4_units", c_units)
        step("6_screenshot", c_screenshot)
        step("5_step_export", c_step_export)
        step("5a_cam_doc", c_cam_doc, needs=("step_file",))
        step("5b_cam_workspace", c_cam_workspace, needs=("cam_doc",))
        step("5c_cam_setup", c_cam_setup, needs=("cam",))
        step("5d_cam_strategies", c_cam_strategies, needs=("setup",))
        step("5e_cam_tool_from_json", c_cam_tool_from_json)
        step("5f_cam_operation", c_cam_operation, needs=("setup", "strategies", "tool"))
        step("5g_cam_generate", c_cam_generate, needs=("op",))
        step("5h_cam_check", c_cam_check, needs=("generated",))
        step("5i_cam_time", c_cam_time, needs=("generated",))
        step("5j_cam_post_lookup", c_cam_post_lookup)
        step("5k_cam_post", c_cam_post, needs=("generated", "post"))

        ev = app.registerCustomEvent(EVENT_ID)
        h = _Handler()
        ev.add(h)
        _handlers.append(h)
        adsk.autoTerminate(False)           # stay loaded until the custom event is handled
        trace("BEGIN 3_socket_thread")
        threading.Thread(target=worker, name="phase0-worker", daemon=True).start()
    except Exception:
        results["fatal"] = traceback.format_exc()
        write_results()
        ui.messageBox("Phase 0 crashed:\n" + results["fatal"], "FusionMCP Phase 0")
