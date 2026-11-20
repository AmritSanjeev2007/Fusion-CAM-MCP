"""Document ops: import_step (into a new scratch design), close_scratch, screenshot.

Runs on Fusion's main thread. All sizes in mm. The add-in only ever closes documents it
created itself (guards.mark_scratch) that were never saved.
"""

import os
import time

import adsk.core
import adsk.fusion

from . import geomcalc, geometry, guards

STEP_EXT = (".step", ".stp")

_HERE = os.path.dirname(os.path.realpath(__file__))           # realpath: the add-in is a junction
REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))


def _app():
    return adsk.core.Application.get()


def test_output_dir() -> str:
    """<repo>/test_output when running from the repo junction, else %APPDATA%/FusionMCP/output."""
    if os.path.exists(os.path.join(REPO, "pyproject.toml")):
        d = os.path.join(REPO, "test_output")
    else:
        from ..bridge import protocol
        d = os.path.join(protocol.state_dir(), "output")
    os.makedirs(d, exist_ok=True)
    return d


def _orientation():
    try:
        o = _app().preferences.generalPreferences.defaultModelingOrientation
        return {adsk.core.DefaultModelingOrientations.YUpModelingOrientation: "Y-up",
                adsk.core.DefaultModelingOrientations.ZUpModelingOrientation: "Z-up"}.get(o, str(o))
    except Exception:
        return None


def import_step(args: dict):
    path = args.get("path")
    if not path or not isinstance(path, str):
        raise guards.OpError("import_step needs 'path' (absolute path to a .step/.stp file)")
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    if os.path.splitext(path)[1].lower() not in STEP_EXT:
        raise guards.OpError(f"not a STEP file (need .step or .stp): {path}")
    if not os.path.isfile(path):
        raise guards.OpError(f"file not found: {path}")
    try:
        exp_size = geomcalc.validate_expected_size(args.get("expected_size_mm"))
        exp_vol = args.get("expected_volume_mm3")
        exp_vol = None if exp_vol is None else float(exp_vol)
        tol = float(args.get("tolerance_pct", 0.1))
    except (TypeError, ValueError) as e:
        raise guards.OpError(f"bad argument: {e}") from None
    if exp_vol is not None and exp_vol <= 0:
        raise guards.OpError("expected_volume_mm3 must be positive")
    if tol <= 0:
        raise guards.OpError("tolerance_pct must be positive")
    guards.require_idle("import_step")

    app = _app()
    t0 = time.perf_counter()
    im = app.importManager
    opts = im.createSTEPImportOptions(path)
    doc = im.importToNewDocument(opts)
    if doc is None:
        raise guards.OpError(f"Fusion could not import {path} (importToNewDocument returned None)")
    import_s = time.perf_counter() - t0
    sid = guards.mark_scratch(doc, source=path)
    design = guards.design_of(doc)
    summ = geometry.summarize(design)
    overall_box, vol = summ.pop("_overall_box"), summ.pop("_volume")
    size = geomcalc.box_size(overall_box) if overall_box else (0.0, 0.0, 0.0)
    out = {
        "document": doc.name,
        "scratch_id": sid,
        "source": path,
        "file_bytes": os.path.getsize(path),
        "import_s": round(import_s, 2),
        "units": "mm",
        "design_type": ("direct" if design.designType == adsk.fusion.DesignTypes.DirectDesignType
                        else "parametric"),
        "modeling_orientation": _orientation(),
        **summ,
    }
    if summ["body_count"] == 0:
        out["warning"] = "the STEP file imported but contains no B-rep bodies (surfaces/mesh only?)"
    chk = geomcalc.import_check(size, vol, exp_size, exp_vol, tol)
    if chk is not None:
        out["check"] = chk
    return out


def close_scratch(args: dict):
    """Close (without saving) documents import_step created that were never saved.

    name=None closes every such document; otherwise only documents with that name, and refuses
    if a document with that name is not scratch or has been saved."""
    name = args.get("name")
    guards.require_idle("close_scratch")
    app = _app()
    docs = [app.documents.item(i) for i in range(app.documents.count)]
    targets, refused = [], []
    for d in docs:
        if name is not None and d.name != name:
            continue
        if not guards.is_scratch(d):
            refused.append({"document": d.name, "reason": "not created by import_step (no FusionMCP scratch "
                                                         "attribute); the add-in never closes the user's documents"})
        elif not guards.never_saved(d):
            refused.append({"document": d.name, "reason": "has been saved (it has a data file); close it "
                                                         "yourself in Fusion if you want it closed"})
        else:
            targets.append(d)
    if name is not None and not targets:
        if refused:
            raise guards.OpError(f"Refusing to close '{name}': {refused[0]['reason']}.")
        raise guards.OpError(f"No open document named '{name}'. Open: {[d.name for d in docs]}")
    closed, failed = [], []
    for d in targets:
        n = d.name
        try:
            ok = d.close(False)             # False = do not save
        except Exception as e:
            ok = False
            failed.append({"document": n, "error": str(e)})
        if ok:
            closed.append(n)
        elif not any(f["document"] == n for f in failed):
            failed.append({"document": n, "error": "close returned False"})
    from . import state
    state.scratch_docs[:] = [d for d in state.scratch_docs if _alive(d)]
    out = {"closed": closed}
    if failed:
        out["failed"] = failed
    if name is not None and refused:
        out["refused"] = refused
    elif refused:
        out["left_open"] = len(refused)         # the user's own documents, untouched
    return out


def _alive(doc) -> bool:
    try:
        return bool(doc.isValid)
    except Exception:
        return False


_VIEWS = {
    "iso": "IsoTopRightViewOrientation",
    "top": "TopViewOrientation",
    "front": "FrontViewOrientation",
    "right": "RightViewOrientation",
}


def screenshot(args: dict):
    view = (args.get("view") or "iso").lower()
    if view not in _VIEWS and view != "current":
        raise guards.OpError(f"view must be one of {sorted(_VIEWS) + ['current']}, got '{view}'")
    try:
        w = int(args.get("width") or 1600)
        h = int(args.get("height") or 1000)
    except (TypeError, ValueError):
        raise guards.OpError("width and height must be integers") from None
    if not (16 <= w <= 8000 and 16 <= h <= 8000):
        raise guards.OpError("width and height must be between 16 and 8000 px")
    path = args.get("path")
    if path:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
        if not path.lower().endswith(".png"):
            path += ".png"
    else:
        path = os.path.join(test_output_dir(), f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}_{view}.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    app = _app()
    vp = app.activeViewport
    if vp is None:
        raise guards.OpError("no active viewport (is a document open?)")
    if view != "current":
        cam = vp.camera
        cam.isSmoothTransition = False
        cam.viewOrientation = getattr(adsk.core.ViewOrientations, _VIEWS[view])
        cam.isFitView = True
        vp.camera = cam
        vp.refresh()
    if not vp.saveAsImageFile(path, w, h) or not os.path.exists(path):
        raise guards.OpError(f"saveAsImageFile failed for {path}")
    doc = app.activeDocument
    return {"path": path, "bytes": os.path.getsize(path), "width": w, "height": h, "view": view,
            "document": doc.name if doc else None}
