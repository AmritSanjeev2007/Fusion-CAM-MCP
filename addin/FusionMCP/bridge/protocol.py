"""Wire protocol shared by the Fusion add-in and the MCP server. Stdlib only.

One JSON object per line, UTF-8.
  request : {"id": str, "token": str, "op": str, "args": {...}, "timeout": float}
  response: {"id": str, "ok": true,  "result": any}
          | {"id": str, "ok": false, "error": str, "traceback": str | None, "kind": str}

`kind` on errors: "auth", "bad_request", "unknown_op", "timeout", "exception", "skipped".

This file is imported by the add-in (inside Fusion) and copied by path by the MCP server,
so it must not import adsk or anything outside the standard library.
"""

import json
import os
import secrets

DEFAULT_PORT = 27190
HOST = "127.0.0.1"
MAX_LINE = 16 * 1024 * 1024          # 16 MB per message
DEFAULT_TIMEOUT = 60.0


def state_dir() -> str:
    """%APPDATA%\\FusionMCP (token, breadcrumbs, add-in log)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "FusionMCP")
    os.makedirs(d, exist_ok=True)
    return d


def port() -> int:
    try:
        return int(os.environ.get("FUSION_MCP_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def token_path() -> str:
    return os.path.join(state_dir(), "token")


def ensure_token() -> str:
    """Create the shared token on first use (add-in side). Returns it."""
    p = token_path()
    if os.path.exists(p):
        tok = open(p, encoding="ascii").read().strip()
        if tok:
            return tok
    tok = secrets.token_hex(24)
    with open(p, "w", encoding="ascii") as f:
        f.write(tok)
    return tok


def read_token() -> str | None:
    """Read the shared token (server side). None if the add-in has never run."""
    p = token_path()
    if not os.path.exists(p):
        return None
    return open(p, encoding="ascii").read().strip() or None


def encode(obj: dict) -> bytes:
    return (json.dumps(obj, default=str, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: bytes) -> dict:
    obj = json.loads(line.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("message must be a JSON object")
    return obj


def error(req_id, kind: str, message: str, tb: str | None = None) -> dict:
    return {"id": req_id, "ok": False, "kind": kind, "error": message, "traceback": tb}


def ok(req_id, result) -> dict:
    return {"id": req_id, "ok": True, "result": result}
