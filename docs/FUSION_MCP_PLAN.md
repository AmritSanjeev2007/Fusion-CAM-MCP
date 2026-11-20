# Fusion MCP: implementation plan (CAM-only scope)

> **Scope decision (2026-11-20):** Fusion is used **only as a CAM back end**.
> - **Inventor** (existing MCP) does all modelling and drawings.
> - **Outside tools** (CalculiX or FreeCAD) do FEA.
> - **Fusion** takes a STEP file exported from Inventor and: imports it, builds the setup and operations, generates toolpaths, reports machining time, and posts G-code.
> - **CAMotics** checks the G-code outside Fusion.
>
> The modelling and analysis phases of the original plan are dropped (see section 4).

```
Inventor MCP ──STEP──► fusion-cam MCP ──.nc──► gcode check (CAMotics + rules)
 (model, drawings)      (import, setup, ops,     (stock removal, envelope,
                         generate, time, post)    rapids into stock)
```

A custom Python add-in plus MCP server for Autodesk Fusion. It reuses the Inventor MCP's shape
(`C:\Inventor 27 MCP`): a thin tool layer, the real logic in the add-in, a report after every
step, a persistent `execute_python`, and skills that ship with the repo. It runs **alongside**
Autodesk's official Fusion MCP (section 8), not instead of it.

Items marked **[verify]** are API details from memory or docs that must be confirmed in
Phase 0 against the installed Fusion build before code depends on them.

---

## 1. Goals and non-goals

**Goals**
- Import an Inventor STEP file into a scratch Fusion design and report what arrived (bodies, bounding box, volume).
- CAM tools: setup (stock, WCS), operations by strategy, tools from JSON, generate, validity check, machining time, post.
- `execute_python` with variables that persist between calls, as the escape hatch.
- G-code checks outside Fusion (CAMotics plus rule checks), since Fusion has no simulation API.
- A crash-safe CAM layer. Phase 0 run 1 showed an `adsk.cam` call can kill Fusion, so every CAM call writes a breadcrumb first, and cloud tool-library access stays out of normal calls.

**Non-goals**
- Modelling, drawings and analysis in Fusion (Inventor does these).
- FEA in Fusion (outside pipeline, separate project).
- Any COM, Rust or native code inside Fusion.
- Driving Fusion's interface (clicking through dialogs).
- Replacing the official MCP's screenshot, document and API-doc search tools (use both).

---

## 2. Architecture

```
Claude (Code / Desktop)
   │  stdio (MCP)
   ▼
fusion-mcp server  (repo venv, Python 3.12, FastMCP from mcp<2)
   │  TCP 127.0.0.1:27190, newline-delimited JSON, shared token
   ▼
FusionMCP add-in  (inside Fusion's bundled Python, stdlib only)
   │  socket thread ──► request queue ──► app.fireCustomEvent ──► main-thread handler
   ▼
Fusion API  (adsk.core / adsk.fusion / adsk.cam)
```

Why a bridge rather than serving MCP from inside Fusion:
- **No third-party packages inside Fusion.** Installing `mcp` into Fusion's bundled Python is fragile, and Fusion upgrades that Python.
- **Easier server changes.** The MCP layer can be edited and restarted without restarting Fusion.
- **Familiar shape.** Same split as the Inventor repo (`server.py` = tools, the add-in = the real logic).

**The main-thread rule:** every Fusion API call must run on Fusion's main thread. The socket
thread never touches `adsk.*`. It queues the request, fires a custom event, and waits on a
`threading.Event` with a timeout. The event handler runs on the main thread, executes the
request, and stores the result.

**Port:** 27190 (the official MCP uses 27182 and one community add-in uses 9876).
You can override it with the `FUSION_MCP_PORT` environment variable on both sides.

**Security:** bind to `127.0.0.1` only, never `0.0.0.0`. On first start the add-in writes a
random token to `%APPDATA%\FusionMCP\token`. Every request must include it, because this
socket runs any code it is sent.

---

## 3. Repository layout

Suggested location: `C:\Fusion MCP` (git repo, same conventions as the Inventor one).

```
C:\Fusion MCP\
├─ addin\FusionMCP\
│  ├─ FusionMCP.manifest
│  ├─ FusionMCP.py            # run()/stop(): start/stop bridge, register custom event
│  ├─ bridge\
│  │  ├─ tcp_server.py        # socket thread, framing, token check, timeouts
│  │  └─ dispatcher.py        # main-thread handler, op registry, persistent namespace
│  └─ ops\                    # the real logic
│     ├─ units.py             # mm↔cm, deg↔rad, ValueInput helpers
│     ├─ breadcrumb.py        # fsync'd BEGIN/END trace before every CAM call (crash forensics)
│     ├─ docs.py              # status, import_step (to a new scratch design), close_scratch, screenshot
│     ├─ geometry.py          # inspect: bodies, bbox, volume, top face height, holes summary
│     ├─ tools.py             # tool JSON loading (bundled samples, user exports), cached library index
│     └─ cam.py               # setups, operations, generate (poll), check, time, NC program + post
├─ src\
│  ├─ __init__.py
│  ├─ server.py               # FastMCP tools: thin wrappers → bridge_client.call(op, **args)
│  └─ bridge_client.py        # connect, token, send/recv, reconnect, timeouts
├─ external\
│  └─ gcode_check.py          # CAMotics CLI run + envelope/rapid rules
├─ skills\
│  ├─ fusion-cam-usage\SKILL.md   # operating manual: startup, tools, crash rules, troubleshooting
│  └─ fusion-cam-recipes\SKILL.md # strategy choices, feeds/speeds conventions, post configs
├─ .claude-plugin\plugin.json # ships the skills as a plugin (like inventor-mcp-usage)
├─ tools\install_addin.ps1    # junction addin\FusionMCP into Fusion's AddIns folder
├─ tests\                     # pytest against a live Fusion (marked) + unit tests for framing
├─ test_output\
├─ .mcp.json.example
├─ pyproject.toml             # mcp[cli]>=1,<2  (pin below 2: the Inventor lesson)
└─ README.md
```

---

## 4. Phases

### Phase 0: Spikes (half a day). Confirm the assumptions before building.
Run these as one-off scripts from Fusion's Scripts and Add-Ins dialog (Shift+S):
1. `import sys; print(sys.version, sys.executable)`. Record Fusion's Python version. **[verify]**
2. Custom event round trip: a background thread calls `app.fireCustomEvent(id, "x")`, and the handler runs on the main thread and can create a sketch.
3. A socket server thread survives while the add-in runs and shuts down cleanly in `stop()`.
4. Units: confirm the API's internal units (cm, rad) by reading a 10 mm extrude's `physicalProperties.volume` (expect 1.0 cm³ for a 10 mm cube).
5. CAM: on a simple part, confirm `setups.createInput`, `operations.createInput(<strategy>)`, `cam.generateToolpath`, `cam.getMachiningTime` and `cam.postProcess` all work, and list the valid strategy names. **[verify]**
6. `app.activeViewport.saveAsImageFile(path, w, h)` works from the main-thread handler.
7. What happens when a command dialog is open: does the custom event wait, fail, or run anyway?

**Done when:** a short `docs/phase0-findings.md` records each result. Any **[verify]** item that failed is redesigned before Phase 1.

### Phase 1: Bridge and `execute_python`
- Add-in skeleton (section 5.1), TCP server, dispatcher, token file.
- Ops: `ping`, `exec(code)` (persistent namespace pre-bound with `app, ui, design, root, adsk, math, mm()`), `reload` (reimports the `ops.*` modules and clears the namespace).
- Errors come back as `{ok:false, error, traceback}`, never as a silent hang. Each request has a timeout (default 60 s; CAM generation is the exception, see Phase 4).
- MCP server: `status`, `execute_python`, `reload_api`.

**Done when:** from Claude, `execute_python("x = 1")` then `execute_python("print(x + 1)")` prints `2`. Killing the MCP server and starting it again reconnects, and Fusion stays responsive throughout.

### Phase 2: STEP import from Inventor
| Tool | Notes |
|---|---|
| `import_step(path)` | `importManager.createSTEPImportOptions` → `importToNewDocument`. The add-in tracks the new document as **scratch**: the only kind it may close. Returns body count, bounding box (mm), volume (mm³). |
| `inspect()` | Bodies, bounding box, volume, top-face Z, and a summary of planar faces and cylindrical holes (diameter and depth), so Claude can pick strategies |
| `screenshot(path, w, h, view="iso")` | Viewport PNG for visual checks |
| `close_scratch()` | Closes scratch documents without saving; refuses any document it didn't open |

Inventor side: export with the existing Inventor MCP's STEP snippet (translator found by
ClassId, as in the inventor-mcp-usage skill section 6.4). Units: STEP carries them, but check that
the imported bounding box matches Inventor's `inspect()`.

**Done when:** the Inventor test plate (80×50×10, R5 corners, Ø16 hole, 2× M6) exported to STEP imports with
a bounding box of 80×50×10 mm and volume 37395 mm³ ±0.1 %.

### Phase 3: CAM
| Tool | Notes |
|---|---|
| `cam_status()` | Setups, operations, tool per operation, toolpath valid or outdated, `checkToolpath` result |
| `cam_create_setup(body, stock_offset_mm, wcs="top-centre")` | Milling setup; stock as an offset box; WCS origin from the bounding box |
| `cam_list_strategies()` | Live list from `compatibleStrategies` (55 on 2706.1.17) |
| `cam_list_tools(source)` | Tools from JSON: the bundled `Samples\*.json` or a user library exported from Fusion. The cloud library query is **opt-in** (`source="cloud"`): it's slow (7.7 s) and implicated in the run 1 crash |
| `cam_add_operation(setup, strategy, tool_id, params_json)` | `Tool.createFromJson`; set parameters by name or expression; return the operation name |
| `cam_generate(targets, timeout_s=600)` | Starts generation, then the **bridge polls** `isGenerationCompleted` with short main-thread calls. Never block the main thread. |
| `cam_machining_time(target, rapid_mm_min=5000, tool_change_s=15)` | `getMachiningTime`, converted to mm and s |
| `cam_post(target, post="fanuc.cps", output_dir)` | Post by URL (`system://fanuc.cps` verified); NC program → `postProcess`; returns the `.nc` path |

Every CAM call writes a breadcrumb first (`ops/breadcrumb.py`). On add-in startup, if the last
breadcrumb has BEGIN but no END, `status()` reports "Fusion crashed during <call>" so Claude doesn't
blindly retry it.

**Done when:** the imported test plate gets a facing, 2D-contour and drilling job that generates, passes
`checkToolpath`, reports a cycle time, and posts Fanuc G-code.

### Phase 4: G-code check (outside Fusion)
- Run CAMotics from the command line on the `.nc` file with the stock from the setup. Report material removed and any reported collisions.
- Rule checks in Python: axis envelope, rapids (G0) below the stock top, feeds and spindle present, tool numbers match the setup.
- Report problems by line number.

**Done when:** a deliberately bad program (a rapid through the stock) is flagged at the right line.

### Phase 5: Skills and packaging (section 7)

*Dropped from the original plan:* modelling tools (old Phase 2), analysis (old Phase 3), FEA inside this repo.

---

## 5. Key code skeletons

### 5.1 `FusionMCP.manifest`
```json
{
  "autodeskProduct": "Fusion",
  "type": "addin",
  "id": "<generate a GUID>",
  "author": "Amrit",
  "description": { "": "Local bridge for the custom Fusion MCP server" },
  "version": "0.1.0",
  "runOnStartup": true,
  "supportedOS": "windows",
  "editEnabled": true
}
```
**[verify]** Some Fusion builds write `"autodeskProduct": "Fusion360"`. Copy the value from a
manifest that Fusion itself generates (Scripts and Add-Ins → Create → Python add-in).

### 5.2 Main-thread dispatch (the core of the add-in)
```python
# FusionMCP.py
import adsk.core, threading, queue, json, uuid, traceback
from .bridge import tcp_server, dispatcher

EVENT_ID = "FusionMCP_Dispatch"
_app = adsk.core.Application.get()
_handlers, _pending = [], {}          # keep handler refs alive; id -> (request, Event, result)
_lock = threading.Lock()
_server = None

class _DispatchHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        req_id = args.additionalInfo
        with _lock:
            req, done, slot = _pending[req_id]
        try:
            slot["result"] = {"ok": True, "result": dispatcher.handle(req)}
        except Exception as e:
            slot["result"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
        finally:
            done.set()

def call_on_main_thread(req: dict, timeout: float) -> dict:
    """Called from the socket thread. Never touches adsk.* itself."""
    req_id, done, slot = uuid.uuid4().hex, threading.Event(), {}
    with _lock:
        _pending[req_id] = (req, done, slot)
    _app.fireCustomEvent(EVENT_ID, req_id)
    ok = done.wait(timeout)
    with _lock:
        _pending.pop(req_id, None)
    return slot.get("result") if ok else {"ok": False, "error": f"timeout after {timeout}s (dialog open or Fusion busy?)"}

def run(context):
    global _server
    ev = _app.registerCustomEvent(EVENT_ID)
    h = _DispatchHandler(); ev.add(h); _handlers.append(h)
    _server = tcp_server.start(call_on_main_thread)      # binds 127.0.0.1:27190, token check

def stop(context):
    if _server: _server.shutdown()
    _app.unregisterCustomEvent(EVENT_ID)
```
A timed-out request may still run later on the main thread. The dispatcher must tolerate
its result being thrown away, and the tool must tell Claude to call `inspect()` before retrying.

### 5.3 Wire protocol
Request: `{"id": "...", "token": "...", "op": "exec" | "call" | "ping" | "reload", "args": {...}, "timeout": 60}`
Response: `{"id": "...", "ok": true, "result": "...printed output or JSON..."}` or
`{"id": "...", "ok": false, "error": "...", "traceback": "..."}`. One JSON object per line, UTF-8.

### 5.4 MCP tool (server side)
```python
# src/server.py
from mcp.server.fastmcp import FastMCP
from .bridge_client import call

mcp = FastMCP("fusion-custom", instructions="Custom Fusion modelling/CAM tools. All dimensions mm, angles deg. Workflow: status → new_design → create_sketch → draw → extrude → inspect. Use execute_python for anything else (persistent namespace; raw API is cm/rad).")

@mcp.tool()
def extrude(distance: float, direction: str = "positive", operation: str = "new",
            sketch_index: int = -1, profile_index: int = 0, extent_type: str = "distance") -> str:
    """Extrude a sketch profile. Returns the feature report."""
    return call("features.extrude", locals())

def main(): mcp.run()
```

---

## 6. Setting up in Fusion

1. **Install the add-in:**
   - *Development (recommended):* run `tools\install_addin.ps1`. It creates a junction from
     `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\FusionMCP` → `C:\Fusion MCP\addin\FusionMCP`,
     so edits in the repo are live after restarting the add-in. **[verify]** the AddIns path on this machine.
   - *Alternative:* in Fusion, press Shift+S → **Add-Ins** tab → **+** (or the green plus) → select
     `C:\Fusion MCP\addin\FusionMCP`.
2. In the Scripts and Add-Ins dialog, select **FusionMCP** → **Run**, and tick **Run on Startup**.
3. Check that it started: open the **Text Commands** window and look for the line
   `FusionMCP listening on 127.0.0.1:27190`. The add-in prints its log there.
4. **Debugging:** Scripts and Add-Ins → select FusionMCP → **Debug** opens VS Code attached to Fusion's Python.
5. **After editing code:** either `reload_api` from Claude (reloads the `ops.*` modules) or Stop/Run
   the add-in (needed for changes to `FusionMCP.py` or `bridge\*`).

## 7. Setting up in Claude

### 7.1 Server environment
```powershell
cd "C:\Fusion MCP"
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e . "mcp[cli]<2"
```
Installing with `-e .` creates `.venv\Scripts\fusion-mcp.exe` (from `[project.scripts] fusion-mcp = "src.server:main"`),
so the server no longer depends on the working directory.

### 7.2 Register the MCP server

**Claude Code (CLI and the desktop app's Code tab), user scope so it works in any folder:**
```powershell
claude mcp add --scope user fusion-custom -- "C:\Fusion MCP\.venv\Scripts\fusion-mcp.exe"
```
Or per project, with `.mcp.json` in the repo (commit `.mcp.json.example` only):
```json
{
  "mcpServers": {
    "fusion-custom": {
      "command": "C:/Fusion MCP/.venv/Scripts/fusion-mcp.exe",
      "env": { "FUSION_MCP_PORT": "27190" }
    }
  }
}
```
**Claude Desktop (chat):** add the same `fusion-custom` block under `mcpServers` in
`%APPDATA%\Claude\claude_desktop_config.json`, then restart the app.

Check it: `claude mcp list` should show `fusion-custom ✓ connected`. Then calling `status`
from a session should return Fusion's version and the active document.

### 7.3 Skills
Ship them in the repo and package them as a plugin, exactly like `inventor-mcp-usage`:
- `skills/fusion-cam-usage/SKILL.md`:
  - startup checks and the Inventor → STEP → Fusion hand-off
  - tool reference and a worked job with real returned outputs
  - main-thread and timeout behaviour
  - **the crash rules:** breadcrumbs, no cloud library by default, never blindly retry a call that crashed
  - troubleshooting, and when to use the official MCP instead (screenshots, API doc search)
- `skills/fusion-cam-recipes/SKILL.md`:
  - strategy choice by feature (face, contour, pocket, drill)
  - stock and WCS conventions
  - feeds and speeds sources
  - post configs
  - the rule that Fusion has no simulation API, so always run `external/gcode_check.py`

Frontmatter template:
```markdown
---
name: fusion-cam-usage
description: How to run CAM in Autodesk Fusion through the custom fusion-custom MCP server, on STEP files exported from Inventor. Use whenever you are about to call any mcp__fusion-custom__* tool, import a STEP for machining, create setups or operations, generate toolpaths, post G-code, start or debug the FusionMCP add-in, or when a call times out, returns a traceback, or Fusion crashes.
---
```
Write each skill **after** Phase 2 or Phase 3 has actually been run, and record real outputs and
failures, as the Inventor skill does. Do not write it from this plan.

Install: add `.claude-plugin/plugin.json` and install the plugin the same way as the Inventor one.
For a quick local test, copy the folders into `%USERPROFILE%\.claude\skills\`.

---

## 8. Setting up the official Fusion MCP server (Autodesk)

### 8.1 Local Fusion MCP (desktop, runs inside Fusion)
1. Make sure Fusion is running and signed in with a subscription.
2. In Fusion, go to **Preferences → General → API** and tick
   **Fusion MCP Server (runs locally on this device)**. Note the port (default **27182**).
   Click **Apply**, then **OK**. Without Apply the change is discarded.
3. Connect Claude:
   - **Claude Desktop:** Settings → Connectors → browse the directory → **Autodesk Fusion** → connect.
     You don't enter a URL.
   - **Claude Code:**
     ```powershell
     claude mcp add --scope user --transport http autodesk-fusion http://127.0.0.1:27182/mcp
     ```
     If you change the port in Fusion, change it in the URL too.
4. Check it: `claude mcp list` shows it connected. Ask *"Take a screenshot of the Fusion viewport"*
   or *"Create a 10 mm cube"*.
5. Notes:
   - It only answers while Fusion is running, and only on this machine (loopback only, no auth).
   - Its tool set is dynamic and may change between Fusion releases, so read the live tool list rather than relying on a fixed one.
   - Reported tools (third-party source): read (projects, documents, API-doc search,
     screenshot, active command), execute (Python `run(_context)` scripts, open/close/save), update (undo/redo),
     and electronics read.

### 8.2 Fusion Data MCP (cloud)
- Cloud-hosted, signs in with your Autodesk account (OAuth). It reads hubs, projects, folders
  and items, and can't edit geometry.
- Claude Desktop: Settings → Connectors → **Add custom connector**, using the URL from Autodesk's
  [Fusion MCPs overview](https://help.autodesk.com/cloudhelp/ENU/Fusion-MCP/files/FMCP-OVERVIEW.htm).
  Claude Code: `claude mcp add --scope user --transport http autodesk-fusion-data <URL>`, then sign in when prompted.

### 8.3 Running both servers together
- They use different ports (27182 official, 27190 custom) and different names (`autodesk-fusion` and `fusion-custom`).
- Division of work: **official** for screenshots, API-doc search, opening documents and undo/redo; **custom**
  for building, checking, transactions and CAM.
- Both queue work onto Fusion's single main thread. Don't run writes through both at the same time.
  After an official-MCP `execute` script, call `inspect()` on the custom one before editing further.

---

## 9. Testing strategy
- **Unit tests (no Fusion):** message framing, token rejection, timeouts, and reconnecting `bridge_client`
  against a fake add-in socket.
- **Live tests (`pytest -m fusion`):** open a scratch design, run the worked plate and the CAM job, check the
  volumes and reports, then close without saving. Never touch documents the tests didn't create.
- **Hang test:** open a command dialog by hand. The tool must time out cleanly with a useful message,
  and Fusion must stay usable.

## 10. Risks
| Risk | Mitigation |
|---|---|
| Fusion's monthly updates change the API or its Python version | Stdlib only inside Fusion; record the version in `status()`; rerun the Phase 0 checks after each update |
| A long request blocks Fusion's main thread | Short main-thread jobs only; CAM generation polls; per-request timeouts |
| Local process exploits the code-execution socket | Loopback bind plus token file; add-in can be switched off in Fusion |
| CAM strategy and parameter names differ by build | Read them live in `cam_status`; keep them in the skill, not hard-coded in the tool layer |
| Topology indices renumber after features | Same advice as Inventor: find edges by geometry right before use; never cache indices across features |
| Cloud saves fail (offline or no project) | `save()` returns a clear error; `export()` to local STEP/F3D always works |
