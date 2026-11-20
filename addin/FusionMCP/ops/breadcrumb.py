"""Crash forensics. Stdlib only.

Before every main-thread op the add-in writes `BEGIN <op> <id>` and fsyncs it; afterwards
`END <op> <id> ok|fail`. If Fusion dies mid-call (Phase 0 run 1: an adsk.cam call killed the
process with no Python exception), the next add-in start finds a BEGIN with no END and
`status` reports which call crashed Fusion, so it is not blindly retried.
"""

import os
import time

from ..bridge import protocol

MAX_BYTES = 512 * 1024


def _path() -> str:
    return os.path.join(protocol.state_dir(), "breadcrumb.log")


def _write(line: str) -> None:
    with open(_path(), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        f.flush()
        os.fsync(f.fileno())


def begin(op: str, rid, summary: str = "") -> None:
    _write(f"BEGIN {op} {rid} {summary}".rstrip())


def end(op: str, rid, ok: bool) -> None:
    _write(f"END {op} {rid} {'ok' if ok else 'fail'}")


def startup() -> dict | None:
    """Call once when the add-in starts. Returns the crashed call, if the last run died in one."""
    p = _path()
    crashed = None
    if os.path.exists(p):
        open_calls: dict[str, str] = {}
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split(" ", 5)
                if len(parts) < 5:
                    continue
                kind, op, rid = parts[2], parts[3], parts[4]
                if kind == "BEGIN":
                    open_calls[rid] = line.strip()
                elif kind == "END":
                    open_calls.pop(rid, None)
                elif kind == "STARTUP":
                    open_calls.clear()
        if open_calls:
            last = list(open_calls.values())[-1]
            parts = last.split(" ", 5)
            crashed = {"when": f"{parts[0]} {parts[1]}", "op": parts[3], "id": parts[4],
                       "summary": parts[5] if len(parts) > 5 else ""}
        if os.path.getsize(p) > MAX_BYTES:
            os.replace(p, p + ".old")
    _write("STARTUP - -")
    return crashed
