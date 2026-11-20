"""CAM ops: setups, operations, generation (started here, polled by the server), checks,
machining time and posting. Runs on Fusion's main thread. Tool-layer units: mm, s, mm/min.

Safety rules (Phase 0: a bad adsk.cam call can kill Fusion with no Python exception):
- every request is wrapped in BEGIN/END breadcrumbs (ops.handle), and risky calls add a
  trace.step() line first, so a crash report names the exact call;
- ops that modify a document require Fusion to be idle and the active document to be a
  scratch document from import_step, unless allow_user_document=true is passed;
- tools come from JSON (ops/tools.py); the cloud library is opt-in;
- generation is started here and never waited for: the server polls cam_generate_poll with
  short main-thread calls.
"""

import json
import os
import shutil
import time
import uuid

import adsk.cam
import adsk.core

from . import camcalc, docs, geomcalc, geometry, guards, state, tools, trace, units

DEFAULT_NC_DIRS = (os.path.join("~", "OneDrive", "Documents", "Fusion 360", "NC Programs"),
                   os.path.join("~", "Documents", "Fusion 360", "NC Programs"))


def _jobs() -> dict:
    # state.py is never reloaded, so generation futures survive reload_api
    return vars(state).setdefault("cam_jobs", {})


def _app():
    return adsk.core.Application.get()


# ---------------------------------------------------------------- document / product
def _target_doc(args: dict, what: str, modify: bool):
    doc, design = guards.active_design(what)
    if modify:
        guards.require_idle(what)
        if not guards.is_scratch(doc) and not args.get("allow_user_document"):
            raise guards.OpError(
                f"Refusing to run {what} on '{doc.name}': it is not a scratch document from import_step. "
                "Import the part with import_step first, or pass allow_user_document=true if the user "
                "explicitly asked to work on this document.")
    return doc, design


def _cam(doc, create: bool = False):
    try:
        c = adsk.cam.CAM.cast(doc.products.itemByProductType("CAMProductType"))
    except Exception:
        c = None
    if c is None and create:
        # Verified: the CAM product only appears once the Manufacture workspace is activated
        trace.step("activate CAMEnvironment workspace")
        _app().userInterface.workspaces.itemById("CAMEnvironment").activate()
        c = adsk.cam.CAM.cast(doc.products.itemByProductType("CAMProductType"))
    if c is None:
        raise guards.OpError(f"'{doc.name}' has no CAM data yet. Call cam_create_setup first.")
    return c


def _setup(cam, name=None):
    setups = [cam.setups.item(i) for i in range(cam.setups.count)]
    if not setups:
        raise guards.OpError("no setups. Call cam_create_setup first.")
    if name is None:
        return setups[-1]
    for s in setups:
        if s.name == name:
            return s
    raise guards.OpError(f"no setup named '{name}'. Setups: {[s.name for s in setups]}")


def _all_ops(cam):
    out = []
    for i in range(cam.setups.count):
        s = cam.setups.item(i)
        out += [(s, s.operations.item(j)) for j in range(s.operations.count)]
    return out


def _resolve_targets(cam, targets, setup_name=None):
    """Setup/operation names -> list of operations (in setup order)."""
    if isinstance(targets, str):
        targets = [targets]
    if not targets:
        s = _setup(cam, setup_name)
        return [s.operations.item(j) for j in range(s.operations.count)]
    allops = _all_ops(cam)
    out = []
    for t in targets:
        hit = [o for s, o in allops if s.name == t] or [o for s, o in allops if o.name == t]
        if not hit:
            raise guards.OpError(f"no setup or operation named '{t}'. Operations: {[o.name for _, o in allops]}")
        out += [o for o in hit if o not in out]
    return out


# ---------------------------------------------------------------- parameter helpers
def _param(obj, name):
    try:
        return obj.parameters.itemByName(name)
    except Exception:
        return None


def _expr(obj, name):
    p = _param(obj, name)
    try:
        return p.expression if p is not None else None
    except Exception:
        return None


def _num(obj, name):
    p = _param(obj, name)
    try:
        return p.value.value if p is not None else None
    except Exception:
        return None


def _set_params(obj, exprs: dict) -> dict:
    """Set parameters by expression; returns {name: error} for any that failed."""
    errors = {}
    for name, expr in exprs.items():
        p = _param(obj, name)
        if p is None:
            errors[name] = "no such parameter"
            continue
        try:
            p.expression = expr
            err = getattr(p, "error", "")
            if err:
                errors[name] = err
        except Exception as e:
            errors[name] = str(e)
    return errors


def _tool_info(op) -> dict:
    try:
        t = op.tool
    except Exception:
        return {}
    if t is None:
        return {}
    info = {"number": None, "description": None, "type": None, "diameter_mm": None}
    for key, pname in (("number", "tool_number"), ("description", "tool_description"), ("type", "tool_type")):
        p = _param(t, pname)
        try:
            info[key] = p.value.value if p is not None else None
        except Exception:
            pass
    d = _num(t, "tool_diameter")
    if d is not None:
        info["diameter_mm"] = round(units.cm_to_mm(d), 4)
    try:
        info["id"] = json.loads(t.toJson()).get("guid")
    except Exception:
        pass
    if isinstance(info["number"], float):
        info["number"] = int(info["number"])
    return info


def _op_info(cam, op, check: bool = False) -> dict:
    info = {"name": op.name, "strategy": getattr(op, "strategy", None), "tool": _tool_info(op),
            "has_toolpath": op.hasToolpath, "toolpath_valid": op.isToolpathValid}
    for attr in ("isSuppressed",):           # isGenerating stays True after completion on 2706.1.17
        try:
            v = getattr(op, attr)
            if v:
                info[attr] = v
        except Exception:
            pass
    try:
        if op.hasError:
            info["error"] = op.error
        if op.hasWarning:
            info["warning"] = op.warning
    except Exception:
        pass
    if check and op.hasToolpath:
        trace.step(f"checkToolpath {op.name}")
        info["check_ok"] = bool(cam.checkToolpath(op))
    return info


_STOCK = ("stockXLow", "stockXHigh", "stockYLow", "stockYHigh", "stockZLow", "stockZHigh")
_MODEL = ("job_modelInfoDimensionX", "job_modelInfoDimensionY", "job_modelInfoDimensionZ")


def _setup_info(cam, s, check=False) -> dict:
    st = {k: _num(s, k) for k in _STOCK}
    info = {"name": s.name,
            "wcs": {"origin_mode": _expr(s, "wcs_origin_mode"), "box_point": _expr(s, "wcs_origin_boxPoint")},
            "stock_mode": _expr(s, "job_stockMode"),
            "stock_offsets": {k: _expr(s, "job_stockOffset" + k.capitalize()) for k in ("sides", "top", "bottom")},
            "operations": [_op_info(cam, s.operations.item(j), check) for j in range(s.operations.count)]}
    if all(v is not None for v in st.values()):
        lo = [units.cm_to_mm(st[k]) for k in ("stockXLow", "stockYLow", "stockZLow")]
        hi = [units.cm_to_mm(st[k]) for k in ("stockXHigh", "stockYHigh", "stockZHigh")]
        info["stock_in_wcs_mm"] = {"min": units.rnd(lo, 3), "max": units.rnd(hi, 3),
                                   "size": units.rnd([h - l for h, l in zip(hi, lo)], 3)}
    dims = [_num(s, k) for k in _MODEL]
    if all(d is not None for d in dims):
        info["model_size_mm"] = units.rnd([units.cm_to_mm(d) for d in dims], 3)
    return info


# ---------------------------------------------------------------- ops
def cam_status(args: dict):
    doc, _ = guards.active_design("cam_status")
    try:
        cam = adsk.cam.CAM.cast(doc.products.itemByProductType("CAMProductType"))
    except Exception:
        cam = None
    out = {"document": doc.name, "scratch": guards.is_scratch(doc), "units": "mm, s",
           "last_crash": state.last_crash}
    if cam is None:
        out["setups"] = []
        out["hint"] = "no CAM data yet: cam_create_setup"
        return out
    check = bool(args.get("check", True))
    out["setups"] = [_setup_info(cam, cam.setups.item(i), check) for i in range(cam.setups.count)]
    out["nc_programs"] = [cam.ncPrograms.item(i).name for i in range(cam.ncPrograms.count)]
    jobs = _jobs()
    if jobs:
        out["generation_jobs"] = sorted(jobs)
    return out


def _bodies_for(design, body):
    allb = list(geometry.iter_bodies(design))
    solids = [(n, b) for n, b in allb if b.isSolid]
    if body is None:
        if not solids:
            raise guards.OpError("the design has no solid bodies")
        return solids
    wanted = body if isinstance(body, list) else [body]
    out = []
    for w in wanted:
        if isinstance(w, int):
            if not 0 <= w < len(allb):
                raise guards.OpError(f"body index {w} out of range (0..{len(allb) - 1})")
            out.append(allb[w])
            continue
        hit = [(n, b) for n, b in allb if n == w or n.split("/")[-1] == w]
        if not hit:
            raise guards.OpError(f"no body named '{w}'. Bodies: {[n for n, _ in allb]}")
        out += hit
    return out


def cam_create_setup(args: dict):
    doc, design = _target_doc(args, "cam_create_setup", modify=True)
    try:
        wcs_p = camcalc.wcs_params(args.get("wcs") or "top-centre")
        stock_p = camcalc.stock_params(args.get("stock_offset_mm", 1.0))
    except ValueError as e:
        raise guards.OpError(str(e)) from None
    bodies = _bodies_for(design, args.get("body"))
    cam = _cam(doc, create=True)
    trace.step("setups.createInput milling")
    si = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
    si.models = [b for _, b in bodies]
    trace.step("setups.add")
    s = cam.setups.add(si)
    if args.get("name"):
        s.name = args["name"]
    errors = _set_params(s, {**stock_p, **wcs_p})
    model_box = geomcalc.union_boxes([geometry.body_bbox_mm(b) for _, b in bodies])
    stock = camcalc.stock_box(model_box, args.get("stock_offset_mm", 1.0))
    wcs = args.get("wcs") or "top-centre"
    origin_box = model_box if wcs.lower().startswith("model-") else stock
    out = {"setup": s.name, "bodies": [n for n, _ in bodies], "units": "mm",
           "model_box_world_mm": {"min": units.rnd(model_box[0], 3), "max": units.rnd(model_box[1], 3)},
           "stock_box_world_mm": {"min": units.rnd(stock[0], 3), "max": units.rnd(stock[1], 3),
                                  "size": units.rnd(geomcalc.box_size(stock), 3)},
           **_setup_info(cam, s)}
    if wcs.lower() != "model-origin":
        out["wcs_origin_world_mm"] = units.rnd(camcalc.box_point(origin_box, wcs), 3)
    if errors:
        out["parameter_errors"] = errors
    return out


def cam_list_strategies(args: dict):
    doc, _ = guards.active_design("cam_list_strategies")
    cam = _cam(doc)
    s = _setup(cam, args.get("setup"))
    strategies = s.operations.compatibleStrategies
    out = []
    for st in strategies:
        e = {"name": st.name}
        for a in ("title", "isGenerationAllowed"):
            try:
                e[a] = getattr(st, a)
            except Exception:
                pass
        out.append(e)
    return {"setup": s.name, "count": len(out), "strategies": out}


def _used_tool_numbers(cam) -> dict:
    used = {}
    for _, o in _all_ops(cam):
        ti = _tool_info(o)
        if ti.get("number") is not None:
            used.setdefault(int(ti["number"]), ti.get("id"))
    return used


def _setup_bodies(s):
    try:
        models = s.models
        return [models.item(i) for i in range(models.count)] if hasattr(models, "count") else list(models)
    except Exception:
        return []


def _hole_faces(bodies, spec) -> list:
    if spec == "all":
        dia, tol = None, 0.01
    elif isinstance(spec, dict):
        dia, tol = spec.get("diameter_mm"), float(spec.get("tol_mm", 0.01))
    else:
        dia, tol = float(spec), 0.01
    faces = []
    for b in bodies:
        for f in getattr(b, "faces", []):
            if f.geometry.surfaceType != adsk.core.SurfaceTypes.CylinderSurfaceType:
                continue
            rec = geometry._cylinder_record(f, 0)
            if not rec["concave"]:
                continue
            if dia is not None and abs(2 * rec["radius"] - float(dia)) > tol:
                continue
            faces.append(f)
    if not faces:
        raise guards.OpError(f"no hole faces match {spec!r} in the setup's model (see inspect for diameters)")
    return faces


def _outline_loop(bodies):
    """Outer loop of the lowest downward-facing planar face (the part's silhouette at its base)."""
    best = None
    for b in bodies:
        for f in getattr(b, "faces", []):
            if f.geometry.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
                continue
            ok, n = f.evaluator.getNormalAtPoint(f.pointOnFace)
            if ok and n.z < -0.999999:
                z = f.pointOnFace.z
                if best is None or z < best[0]:
                    best = (z, f)
    if best is None:
        raise guards.OpError("no downward-facing planar face found for the outline contour")
    return next(l for l in best[1].loops if l.isOuter)


def _apply_geometry(op, bodies, geom: dict) -> dict:
    applied = {}
    if not geom:
        return applied
    unknown = set(geom) - {"holes", "contour", "side"}
    if unknown:
        raise guards.OpError(f"unknown geometry keys {sorted(unknown)}; use holes / contour (+ side)")
    if "holes" in geom:
        p = _param(op, "holeFaces")
        if p is None:
            raise guards.OpError(f"strategy '{op.strategy}' has no holeFaces parameter")
        faces = _hole_faces(bodies, geom["holes"])
        trace.step(f"set holeFaces x{len(faces)}")
        p.value.value = faces
        applied["hole_faces"] = len(faces)
    if "contour" in geom:
        if geom["contour"] != "outline":
            raise guards.OpError("geometry.contour supports 'outline' (the part's base silhouette)")
        side = geom.get("side", "outside")
        if side not in ("outside", "inside"):
            raise guards.OpError("geometry.side must be 'outside' or 'inside'")
        p = _param(op, "contours")
        if p is None:
            raise guards.OpError(f"strategy '{op.strategy}' has no contours parameter")
        loop = _outline_loop(bodies)
        trace.step("set contours chain")
        sels = p.value.getCurveSelections()
        ch = sels.createNewChainSelection()
        ch.inputGeometry = [loop.edges.item(0)]
        # Verified live: on a downward-facing face's outer loop the default side is inside;
        # reverting the chain puts a left-compensated (climb) cut on the outside.
        ch.isReverted = side == "outside"
        p.value.applyCurveSelections(sels)
        applied["contour"] = {"outline_edges": loop.edges.count, "side": side}
    return applied


def cam_add_operation(args: dict):
    doc, _ = _target_doc(args, "cam_add_operation", modify=True)
    cam = _cam(doc)
    s = _setup(cam, args.get("setup"))
    strategy = args.get("strategy")
    names = [st.name for st in s.operations.compatibleStrategies]
    if strategy not in names:
        raise guards.OpError(f"unknown strategy '{strategy}'. Call cam_list_strategies; e.g. {names[:15]}")
    tool_id = args.get("tool_id")
    if not tool_id:
        raise guards.OpError("tool_id is required (from cam_list_tools)")
    try:
        params = camcalc.parse_params(args.get("params", args.get("params_json")))
        number = camcalc.pick_tool_number(_used_tool_numbers(cam), tool_id, args.get("tool_number"))
    except ValueError as e:
        raise guards.OpError(str(e)) from None
    tool, lib, entry = tools.make_tool(tool_id, number)
    trace.step(f"operations.createInput {strategy}")
    oi = s.operations.createInput(strategy)
    oi.tool = tool
    trace.step(f"operations.add {strategy}")
    op = s.operations.add(oi)
    if args.get("name"):
        op.name = args["name"]
    try:
        applied = _apply_geometry(op, _setup_bodies(s), args.get("geometry") or {})
    except Exception:
        op.deleteMe()
        raise
    errors = _set_params(op, params)
    out = {"setup": s.name, "operation": op.name, "strategy": strategy, "tool": _tool_info(op),
           "tool_library": lib, "geometry": applied, "parameters_set": sorted(set(params) - set(errors)),
           "next": "cam_generate"}
    if errors:
        out["parameter_errors"] = errors
    return out


def cam_generate_start(args: dict):
    doc, _ = _target_doc(args, "cam_generate", modify=True)
    cam = _cam(doc)
    ops = _resolve_targets(cam, args.get("targets"), args.get("setup"))
    if not ops:
        raise guards.OpError("nothing to generate (no operations)")
    futures = []
    for op in ops:
        trace.step(f"generateToolpath {op.name}")
        futures.append(cam.generateToolpath(op))
    job = uuid.uuid4().hex[:8]
    _jobs()[job] = {"futures": futures, "ops": ops, "started": time.time(), "doc": doc.name}
    return {"job": job, "operations": [o.name for o in ops], "started": True}


def cam_generate_poll(args: dict):
    job = args.get("job")
    jobs = _jobs()
    j = jobs.get(job)
    if j is None:
        raise guards.OpError(f"unknown generation job '{job}' (finished jobs are forgotten; call cam_status)")
    done = []
    for f in j["futures"]:
        try:
            done.append(bool(f.isGenerationCompleted))
        except Exception:
            done.append(True)           # future invalidated (document closed?) -> stop polling
    elapsed = round(time.time() - j["started"], 1)
    out = {"job": job, "completed": all(done), "elapsed_s": elapsed,
           "done": sum(done), "total": len(done)}
    if out["completed"]:
        cam = None
        try:
            cam = _cam(_app().activeDocument)
        except Exception:
            pass
        ops = []
        for op in j["ops"]:
            try:
                ops.append(_op_info(cam, op, check=cam is not None))
            except Exception as e:
                ops.append({"error": f"operation no longer valid: {e}"})
        out["operations"] = ops
        out["all_valid"] = all(o.get("toolpath_valid") and o.get("check_ok", True) for o in ops)
        jobs.pop(job, None)
    return out


def cam_machining_time(args: dict):
    doc, _ = guards.active_design("cam_machining_time")
    cam = _cam(doc)
    rapid = float(args.get("rapid_mm_min", 5000))
    tc = float(args.get("tool_change_s", 15))
    scale = float(args.get("feed_scale_pct", 100))
    if rapid <= 0 or tc < 0 or scale <= 0:
        raise guards.OpError("rapid_mm_min and feed_scale_pct must be > 0, tool_change_s >= 0")
    target = args.get("target")
    allops = _all_ops(cam)
    setups = [cam.setups.item(i) for i in range(cam.setups.count)]
    if target is None:
        obj, ops = _setup(cam), None
    else:
        obj = next((s for s in setups if s.name == target), None) or next((o for _, o in allops if o.name == target), None)
        if obj is None:
            raise guards.OpError(f"no setup or operation named '{target}'")
    ops = [obj.operations.item(j) for j in range(obj.operations.count)] if hasattr(obj, "operations") else [obj]
    missing = [o.name for o in ops if not o.hasToolpath]
    if missing:
        raise guards.OpError(f"no toolpath yet for {missing}: run cam_generate first")

    def raw(o):
        trace.step(f"getMachiningTime {o.name}")
        mt = cam.getMachiningTime(o, scale, camcalc.mm_min_to_cm_s(rapid), tc)
        return {k: getattr(mt, k, None) for k in ("machiningTime", "feedDistance", "rapidDistance", "toolChangeCount",
                                                    "totalFeedTime", "totalRapidTime", "totalToolChangeTime")}
    out = {"target": obj.name, "assumptions": {"rapid_mm_min": rapid, "tool_change_s": tc, "feed_scale_pct": scale},
           **camcalc.time_report(raw(obj))}
    if len(ops) > 1:
        out["per_operation"] = [{"operation": o.name, **camcalc.time_report(raw(o))} for o in ops]
    out["note"] = ("Fusion's estimate. On a Personal Use licence the posted G-code also slows rapids to "
                   "the cutting feed, so the real machine takes longer than rapid_mm_min suggests.")
    return out


# ---------------------------------------------------------------- posting
def _set_nc_params(ni, filename, folder, program_name):
    errs = _set_params(ni, {"nc_program_filename": f"'{filename}'",
                            "nc_program_output_folder": "'" + folder.replace("\\", "/") + "'",
                            "nc_program_openInEditor": "false"})
    if program_name:
        errs.update(_set_params(ni, {"nc_program_name": f"'{program_name}'"}))
    return errs


def _find_output(filename, folder, since):
    """Locate a posted file: the requested folder first, then Fusion's default NC folder (Phase 0
    run 3 found the folder parameter ignored on the input); move it into `folder` if needed."""
    for d in [folder] + [os.path.expanduser(p) for p in DEFAULT_NC_DIRS]:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            full = os.path.join(d, f)
            if os.path.splitext(f)[0] == filename and os.path.getmtime(full) >= since - 2:
                if d != folder:
                    dest = os.path.join(folder, f)
                    shutil.move(full, dest)
                    return dest, d
                return full, None
    return None, None


def _post_program(cam, post, ops, filename, folder, program_name, keep):
    ni = cam.ncPrograms.createInput()
    ni.displayName = filename
    errs = _set_nc_params(ni, filename, folder, program_name)
    ni.operations = ops
    trace.step(f"ncPrograms.add {filename}")
    nc = cam.ncPrograms.add(ni)
    nc.postConfiguration = post
    since = time.time()
    trace.step(f"postProcess {filename}")
    ok = nc.postProcess(adsk.cam.NCProgramPostProcessOptions.create())
    err = nc.error if nc.hasError else ""
    path, moved_from = _find_output(filename, folder, since) if ok else (None, None)
    if not keep:
        nc.deleteMe()
    return {"ok": bool(ok) and path is not None, "returned": bool(ok), "error": err, "path": path,
            "moved_from": moved_from, "param_errors": errs}


def _file_report(path, head_lines):
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return {"path": path, "bytes": os.path.getsize(path), "lines": len(lines), "head": lines[:head_lines],
            "personal_use_rapids": any("PERSONAL USE" in l.upper() for l in lines[:40])}


def cam_post(args: dict):
    doc, _ = _target_doc(args, "cam_post", modify=True)
    cam = _cam(doc)
    ops = _resolve_targets(cam, args.get("targets") or args.get("target"), args.get("setup"))
    bad = [o.name for o in ops if not (o.hasToolpath and o.isToolpathValid)]
    if bad:
        raise guards.OpError(f"operations without a valid toolpath: {bad}. Run cam_generate first.")
    url = camcalc.post_url(args.get("post") or "fanuc.cps")
    trace.step(f"postConfigurationAtURL {url}")
    post = adsk.cam.CAMManager.get().libraryManager.postLibrary.postConfigurationAtURL(adsk.core.URL.create(url))
    if post is None:
        raise guards.OpError(f"no post processor at {url} (try 'fanuc.cps' or a full system:// URL)")
    folder = args.get("output_dir")
    folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder))) if folder \
        else os.path.join(docs.test_output_dir(), "nc")
    os.makedirs(folder, exist_ok=True)
    try:
        default_name = ops[0].parentSetup.name
    except Exception:
        default_name = "program"
    base = camcalc.safe_filename(args.get("filename") or default_name)
    program_name = str(args.get("program_name") or "1001")
    split = args.get("split", "auto")
    if split not in ("auto", "none", "tool"):
        raise guards.OpError("split must be 'auto', 'none' or 'tool'")
    keep = bool(args.get("keep_programs", False))
    head = int(args.get("head_lines", 20))
    results, note = [], None
    if split in ("auto", "none"):
        r = _post_program(cam, post, ops, base, folder, program_name, keep)
        if r["ok"]:
            results.append({"operations": [o.name for o in ops], **r})
        elif split == "auto" and camcalc.is_tool_change_restriction(r["error"]):
            note = ("Fusion for Personal Use cannot post tool changes in one NC program, so the job was split "
                    "into one program per tool (run them in order).")
            split = "tool"
        else:
            raise guards.OpError(f"post failed for {base}: {r['error'] or 'postProcess returned False / no file'}")
    if split == "tool":
        by_name = {o.name: o for o in ops}
        groups = camcalc.tool_groups([(o.name, _tool_info(o).get("number")) for o in ops])
        for i, g in enumerate(groups, 1):
            tn = _tool_info(by_name[g[0]]).get("number")
            fname = f"{base}_{i:02d}_T{tn}"
            r = _post_program(cam, post, [by_name[n] for n in g], fname, folder, program_name, keep)
            if not r["ok"]:
                raise guards.OpError(f"post failed for {fname} ({g}): {r['error'] or 'no file written'}")
            results.append({"operations": g, **r})
    files = []
    for r in results:
        rep = _file_report(r["path"], head)
        rep["operations"] = r["operations"]
        if r.get("moved_from"):
            rep["moved_from"] = r["moved_from"]
        files.append(rep)
    out = {"post": url, "post_description": post.description, "output_dir": folder, "files": files}
    if note:
        out["note"] = note
    if any(f["personal_use_rapids"] for f in files):
        out["warning"] = ("Personal Use licence: rapids are posted at the cutting feed rate. Check feeds/speeds "
                          "(the sample tools carry generic presets) before running on a machine.")
    return out
