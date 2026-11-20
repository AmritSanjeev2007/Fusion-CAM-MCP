# Phase 1 results: bridge and execute_python (2026-11-20)

Fusion 2706.1.17, Python 3.14.7 inside Fusion; server venv Python 3.14.7, `mcp` 1.30.0.

## Built
- `addin/FusionMCP`: loopback socket server (127.0.0.1:27190, token in `%APPDATA%\FusionMCP\token`), main-thread queue via custom events, breadcrumbs (`%APPDATA%\FusionMCP\breadcrumb.log`), log (`addin.log` + Text Commands).
  - Ops: `hello` (socket thread), `ping`, `status`, `exec`, `reload`.
- `src/`: `fusion-cam` MCP server with tools `status`, `execute_python`, `reload_api`, plus `bridge_client.py`. The protocol is loaded from the add-in's own `protocol.py`.
- `tools/install_addin.ps1`: junction into Fusion's AddIns folder (installed).
- `.mcp.json`: project-scoped registration → `.venv\Scripts\fusion-cam-mcp.exe`.

## Verified
| Check | Result |
|---|---|
| Offline bridge tests (`pytest`) | 11/11 pass: round trip and unicode, bad token, exception and traceback, timeout before pickup (cancelled, never runs), timeout while running, reconnect after restart, not-running message, invalid JSON, breadcrumb crash detection, token creation |
| Live tests (`pytest -m fusion`) | 6/6 pass: hello, status, persistent namespace, error shows the user's line, runs on the main thread, reload clears the namespace |
| MCP over stdio (real client → `fusion-cam-mcp.exe` → add-in → Fusion) | 3 tools listed; status 43 ms; `execute_python` median **15.8 ms**, max 27 ms over 20 calls |
| `reload_api` | Reloads `ops.*` in ~11 ms. Changes to `FusionMCP.py` or `bridge/*` need the add-in stopped and run again (confirmed) |
| **Command dialog open** (Extrude, Phase 0 item 7) | Requests **still run** (~10 ms). `status` reports `active_command: "Extrude"`, `idle: false`. `execute_python` now prefixes a warning when Fusion isn't idle; Phase 2–3 tools that modify documents will **refuse** unless idle. |

## Not verified
- A truly **modal** dialog (e.g. a message box or Preferences) blocking custom events. The bridge handles it by design: requests time out before pickup and are cancelled, and `status` says "add-in reachable but main thread busy". That path is unit-tested only.
- The `fusion-cam` tools inside a Claude session: they load in the next session in `C:\Fusion MCP` after approving the project server.

## Next: Phase 2 (STEP import from Inventor)
`import_step`, `inspect`, `screenshot`, `close_scratch`; plus a shared `require_idle()` guard for every op that modifies a document.
