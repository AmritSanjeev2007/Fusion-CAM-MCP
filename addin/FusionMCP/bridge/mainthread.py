"""Hand requests from socket threads to Fusion's main thread. Stdlib only.

The add-in passes in `fire(req_id)` (which calls app.fireCustomEvent) and calls
`MainThreadQueue.run(req_id, handle)` from its CustomEventHandler on the main thread.
Kept separate from adsk so the queue logic can be unit-tested without Fusion.

Timeout semantics: if a request times out *before* the main thread picks it up, it is
marked cancelled and never runs (no ghost execution). If it times out *while running*,
the caller gets a timeout error and the eventual result is discarded; the response says so,
so Claude knows to call status/inspect before retrying.
"""

import threading
import traceback
import uuid

from . import protocol


class _Slot:
    __slots__ = ("req", "done", "result", "state")

    def __init__(self, req):
        self.req = req
        self.done = threading.Event()
        self.result = None
        self.state = "queued"           # queued -> running -> finished | cancelled


class MainThreadQueue:
    def __init__(self, fire):
        self._fire = fire
        self._slots: dict[str, _Slot] = {}
        self._lock = threading.Lock()

    # socket thread
    def submit(self, req: dict, timeout: float) -> dict:
        key = uuid.uuid4().hex
        slot = _Slot(req)
        with self._lock:
            self._slots[key] = slot
        try:
            self._fire(key)
        except Exception as e:
            with self._lock:
                self._slots.pop(key, None)
            return protocol.error(req.get("id"), "exception", f"could not reach Fusion's main thread: {e}",
                                  traceback.format_exc())
        finished = slot.done.wait(timeout)
        with self._lock:
            if not finished:
                if slot.state == "queued":
                    slot.state = "cancelled"
                    msg = (f"timed out after {timeout:g}s before Fusion's main thread picked it up; "
                           "it was cancelled and did NOT run. Fusion is busy or a modal dialog/command is open.")
                else:
                    msg = (f"timed out after {timeout:g}s while running on Fusion's main thread; it may still "
                           "finish. Call status (and inspect) before retrying.")
                self._slots.pop(key, None)
                return protocol.error(req.get("id"), "timeout", msg)
            self._slots.pop(key, None)
        return slot.result

    # main thread
    def run(self, key: str, handle) -> None:
        with self._lock:
            slot = self._slots.get(key)
            if slot is None or slot.state == "cancelled":
                return
            slot.state = "running"
        rid = slot.req.get("id")
        try:
            result = protocol.ok(rid, handle(slot.req))
        except Exception as e:
            if getattr(e, "plain", False):          # user-code error already formatted by the op
                result = protocol.error(rid, "exception", str(e), None)
            else:
                result = protocol.error(rid, "exception", f"{type(e).__name__}: {e}", traceback.format_exc())
        with self._lock:
            slot.result = result
            slot.state = "finished"
        slot.done.set()

    def pending(self) -> int:
        with self._lock:
            return len(self._slots)
