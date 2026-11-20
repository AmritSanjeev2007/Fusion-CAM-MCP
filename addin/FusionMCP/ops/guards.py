"""Shared guards for ops that modify documents. Runs on Fusion's main thread.

- OpError: a user-facing failure. The bridge reports its message as-is (no traceback), the
  same way exec reports errors in the user's code.
- require_idle(): refuse to modify documents while the user is mid-command (Phase 1 found
  requests still run while e.g. the Extrude dialog is open).
- Scratch documents: import_step marks the documents it creates with a design attribute
  (FusionMCP/scratch). The attribute lives in the document, so it survives reload_api and an
  add-in restart. close_scratch only closes documents that carry it AND were never saved.
"""

import json
import time
import uuid

import adsk.core
import adsk.fusion

from . import state

ATTR_GROUP = "FusionMCP"
ATTR_SCRATCH = "scratch"


class OpError(RuntimeError):
    plain = True            # mainthread: report the message as-is


def active_command():
    try:
        return adsk.core.Application.get().userInterface.activeCommand
    except Exception:
        return None


def is_idle() -> bool:
    return active_command() in (None, "SelectCommand")


def require_idle(what: str = "this") -> None:
    cmd = active_command()
    if cmd not in (None, "SelectCommand"):
        raise OpError(f"Refusing to run {what}: Fusion is not idle (active command: {cmd}). "
                      "Ask the user to finish or cancel the command in Fusion, then retry.")


def design_of(doc):
    if doc is None:
        return None
    try:
        return adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
    except Exception:
        return None


def active_design(what: str = "this"):
    app = adsk.core.Application.get()
    doc = app.activeDocument
    design = design_of(doc)
    if design is None:
        raise OpError(f"Cannot run {what}: the active document has no design "
                      f"({doc.name if doc else 'no document open'}). Run import_step first.")
    return doc, design


def mark_scratch(doc, source: str = "") -> str:
    sid = uuid.uuid4().hex[:12]
    design = design_of(doc)
    if design is None:
        raise OpError("imported document has no design product; cannot mark it as scratch")
    design.attributes.add(ATTR_GROUP, ATTR_SCRATCH,
                          json.dumps({"id": sid, "source": source, "created": time.strftime("%Y-%m-%d %H:%M:%S")}))
    state.scratch_docs.append(doc)
    return sid


def scratch_info(doc) -> dict | None:
    """The scratch attribute's payload, or None if the add-in did not create this document."""
    design = design_of(doc)
    if design is None:
        return None
    try:
        attr = design.attributes.itemByName(ATTR_GROUP, ATTR_SCRATCH)
    except Exception:
        attr = None
    if attr is None:
        return None
    try:
        return json.loads(attr.value)
    except Exception:
        return {"id": attr.value}


def is_scratch(doc) -> bool:
    return scratch_info(doc) is not None


def never_saved(doc) -> bool:
    """True only if the document has no cloud data file and has never been saved."""
    try:
        df = doc.dataFile
    except Exception:
        df = None
    try:
        saved = bool(doc.isSaved)
    except Exception:
        saved = True                    # unknown: treat as saved, so it is never closed
    return df is None and not saved
