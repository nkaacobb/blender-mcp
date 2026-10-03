# Blender MCP

A local [Model Context Protocol](https://modelcontextprotocol.io) server that lets
AI clients (Claude Code, VS Code, and other MCP clients) drive the copy of Blender
that is open on this computer. It comes with a web page for browsing, testing, and
starting/stopping the server.

```
  MCP client or tester page        blender-mcp-server.py                Blender
  -------------------------        ---------------------                -------------------------
  Claude Code, VS Code,     HTTP   MCP endpoint                  TCP    Blender Lab listener
  index.html in a browser  ----->  http://127.0.0.1:8765/mcp   ----->   127.0.0.1:9876
                          JSON-RPC (also serves index.html at /)  JSON  runs the Python in the
                                                                        open scene
```

The server never imports `bpy`. Each of its 51 tools builds a short Blender Python
snippet and sends it to the Blender Lab listener running inside Blender. The listener
executes it in the open scene and returns the result.

## Files

| File | Purpose |
| --- | --- |
| [blender-mcp-server.py](blender-mcp-server.py) | The MCP server (FastMCP + Uvicorn). Also serves the tester page. |
| [index.html](index.html) | The tester page: tool explorer, call harness, and server controls. Plain HTML/JS, no build step. |
| [server-control.php](server-control.php) | Small JSON API the page uses to start, stop, and restart the server when the page is opened through Apache (XAMPP). |
| [start-server.ps1](start-server.ps1) | Starts the server in a PowerShell window, the manual way. |
| [assets/](assets/) | Static files for the page, served at `/assets`. |
| [test_http_transport.py](test_http_transport.py) | HTTP transport regression tests. |
| [HTTP-TRANSPORT-DIAGNOSIS.md](HTTP-TRANSPORT-DIAGNOSIS.md) | Notes on the chunked-encoding workaround in the server. |

## Requirements

- **Python 3.10 or newer** with `mcp`, `uvicorn`, and `starlette`
  (tested with Python 3.13.1, mcp 1.26.0, uvicorn 0.34.0, starlette 1.6.0):

  ```powershell
  python -m pip install mcp uvicorn starlette
  ```

- **Blender with the Blender Lab TCP listener** running on `127.0.0.1:9876`. The MCP
  server starts without it, but tool calls report
  `Connection refused by Blender at 127.0.0.1:9876` until it is up.
- **Optional: XAMPP** (Apache + PHP 8.1 or newer, tested with PHP 8.2.12). It is only
  needed for the page's **Start / Stop / Restart** buttons.

## Quick start

1. Open Blender and start the Blender Lab listener.
2. Make sure XAMPP's Apache is running and serving this folder (see
   [Opening it](#opening-it)), then open the page, for example
   <http://localhost/blender-mcp/>.
3. Click the status pill in the top-right corner, then click **Start server**. The page
   connects on its own once the server is up.

Or start it yourself from PowerShell with `.\start-server.ps1` (next section). The page
works the same either way: if the server is already running, the pill shows it as
running; if it is not, **Start server** launches it.

---

## Starting the server from PowerShell

### With the script

```powershell
cd C:\path\to\blender-mcp
.\start-server.ps1
```

[start-server.ps1](start-server.ps1) runs the server in the current window, the same as
the manual command below. Press **Ctrl+C** to stop it. Any arguments are passed to the
server unchanged:

```powershell
.\start-server.ps1 --socket-timeout 120
.\start-server.ps1 --mcp-port 8766 --blender-port 9877
```

If the server is already running (for example, started from the web page), the script
says so, prints the PID, and exits. It never starts a second copy that would fail on
the port. If some other program holds the port, the script names that program.

The script uses `python` from your PATH. If that is the wrong interpreter, edit the
`$Python` line at the top of the script.

The tester page shows a server started this way as **running**, and its **Stop** and
**Restart** buttons work on it (see
[Mixing manual and page control](#mixing-manual-and-page-control)).

### By hand, in the foreground

```powershell
cd C:\path\to\blender-mcp
python .\blender-mcp-server.py
```

You should see:

```
Blender MCP endpoint: http://127.0.0.1:8765/mcp
Blender Lab bridge: 127.0.0.1:9876 (timeout 30s)
Tester page:      http://127.0.0.1:8765/
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)
```

Keep the window open. Every request is logged there. Press **Ctrl+C** to stop the
server.

> **Wrong Python?** `where.exe python` lists every `python.exe` on your PATH, and the
> first one wins. If that one does not have the packages installed, call the right
> interpreter by its full path:
>
> ```powershell
> & "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" .\blender-mcp-server.py
> ```

### Command-line options

| Option | Default | Purpose |
| --- | --- | --- |
| `--mcp-port PORT` (alias `--port`) | `8765` | Port for the MCP endpoint and the tester page. |
| `--mcp-host HOST` (alias `--host`) | `127.0.0.1` | Address to listen on. See [Security](#security) before changing it. |
| `--blender-host HOST` | `127.0.0.1` | Where the Blender Lab listener is. |
| `--blender-port PORT` | `9876` | Blender Lab listener port. |
| `--socket-timeout SECONDS` | `30` | Connect/read timeout for each Blender call. Raise it for slow operations such as renders. |
| `--allow-origin ORIGIN` | | Extra browser origin allowed to call `/mcp` (repeatable). Append `:*` for any port, e.g. `http://mybox:*`. |
| `--allow-host HOST` | | Extra `Host` header value accepted by the DNS-rebinding guard (repeatable). |
| `--web-root DIR` | script folder | Folder containing the `index.html` to serve. |
| `--no-web` | | Serve only `/mcp`, without the tester page. |

Loopback origins (`http://127.0.0.1`, `http://localhost`, and `http://[::1]` on any
port) are always allowed, so pages served by Apache on this computer work without
extra flags. Run `python .\blender-mcp-server.py --help` for the full list.

Examples:

```powershell
python .\blender-mcp-server.py --mcp-port 8766
python .\blender-mcp-server.py --socket-timeout 120
python .\blender-mcp-server.py --no-web
```

### In the background (hidden window)

To start the server without keeping a PowerShell window open, use `Start-Process`
and send its output to log files:

```powershell
$env:PYTHONIOENCODING = 'utf-8'   # keeps redirected output in UTF-8
$server = @{
    FilePath               = 'python'
    ArgumentList           = '-u', 'blender-mcp-server.py'   # -u: write logs immediately
    WorkingDirectory       = 'C:\path\to\blender-mcp'
    WindowStyle            = 'Hidden'
    RedirectStandardOutput = "$env:TEMP\blender-mcp-server.out.log"
    RedirectStandardError  = "$env:TEMP\blender-mcp-server.err.log"
}
Start-Process @server
```

`Start-Process` cannot write both output streams to one file. The startup banner goes
to `.out.log`, and Uvicorn's request log and any errors go to `.err.log`. The server
keeps running after you close PowerShell.

Follow the log live:

```powershell
Get-Content "$env:TEMP\blender-mcp-server.err.log" -Wait -Tail 20
```

### Checking and stopping a running server

Is anything listening on the port?

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
```

No output means the server is not running. Otherwise, `OwningProcess` is the
server's PID. Check that it is `python` before stopping it:

```powershell
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen
Get-Process -Id $listener.OwningProcess   # should be python
Stop-Process -Id $listener.OwningProcess
```

This also stops a server that was started from the web page.

### Testing the endpoint from PowerShell

Ask the MCP server to identify itself:

```powershell
$body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"powershell","version":"1.0"}}}'
Invoke-RestMethod http://127.0.0.1:8765/mcp -Method Post -ContentType application/json `
    -Headers @{ Accept = 'application/json, text/event-stream' } -Body $body
```

The reply contains `serverInfo.name = "Blender MCP"`. That only proves the MCP server
is up. To check the connection to Blender as well, call the `ping_blender` tool. The
server is stateless, so no `initialize` is needed first:

```powershell
$ping = '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ping_blender","arguments":{}}}'
(Invoke-RestMethod http://127.0.0.1:8765/mcp -Method Post -ContentType application/json `
    -Headers @{ Accept = 'application/json, text/event-stream' } -Body $ping).result.structuredContent
```

`connected : True` means Blender answered. `connected : False` comes with an `error`
that explains why.

---

## The tester page (index.html)

### Opening it

Open the page through XAMPP's Apache. Put this folder under `htdocs`, or point an
Apache `Alias` at it, then open its URL, for example
**<http://localhost/blender-mcp/>**. This is the only way to get the
**Start / Stop / Restart** buttons, because they rely on `server-control.php`, which
needs PHP.

| URL | Served by | Start / Stop buttons |
| --- | --- | --- |
| Your Apache URL for this folder, e.g. `http://localhost/blender-mcp/` (recommended) | Apache (XAMPP) | Yes, through `server-control.php`. |
| <http://127.0.0.1:8765/> | The MCP server itself | No. The panel explains that PHP is needed. This URL only works while the server is already running, so it is useful only for browsing and calling tools. |
| `file:///…/index.html` | Nothing | Does not work. The browser sends `Origin: null`, which the server rejects with 403. |

When the MCP server serves the page, the endpoint field is set to `<page origin>/mcp`.
When anything else serves it, the field defaults to `http://127.0.0.1:8765/mcp`. You
can type another endpoint and press **Connect**.

### What happens on load

1. The page picks the endpoint, as described above.
2. It connects with `initialize` → `notifications/initialized` → `tools/list`. The
   status pill in the top-right corner then reads **connected · 51 tools**.
3. It asks `server-control.php` for the server status. If a start was already in
   progress (for example, you reloaded during a start), the page keeps following it.

If the connection fails, the main area explains why (server not running, origin
rejected, host rejected, or a network/CORS problem). When PHP is available, it also
offers a **Start server** button.

### Layout

**Top bar.** The endpoint field, **Connect**, and the status pill. Click the pill to
open the [server panel](#the-server-panel).

**Sidebar.** A search box and the tools, grouped by category (Health & Info, Objects,
Selection, Transforms, Collections, Mesh, Modifiers, Materials, Lighting & Cameras,
Rendering, Animation, Import & Export, Asset Prep, Advanced). Before the page
connects, the list comes from a catalog built into the page and each tool is marked
with ○. After connecting, it is the server's live `tools/list`. The buttons:

- **tools/list** reloads the tool list.
- **resources/list** / **prompts/list** call those MCP methods. This server exposes
  none, so both return an empty list.
- **Smoke test** runs nine read-only tools with no arguments (`ping_blender`,
  `get_blender_version`, `get_scene_info`, `list_objects`, `list_collections`,
  `list_materials`, `get_render_settings`, `get_object_hierarchy`,
  `get_animation_info`) and shows a pass/fail table. It changes nothing in the scene.
- **Clear log** empties the activity log.

**Main area**, for the selected tool:

- **Tool card.** The description, plus a form generated from the tool's JSON input
  schema (text, numbers, checkboxes, drop-downs for enums), with required fields
  marked. Buttons:
  - **Call tool** runs the tool.
  - **Load example** fills in sample arguments.
  - **Preview payload** shows the request without sending it.
  - **Copy cURL** / **Show cURL** give the same request as a `curl` command.

  Below the buttons, an argument table lists each argument's type and description.
  **Copy link** copies a deep link (`#tool=<name>`) that opens this tool directly.
- **Request payload.** The exact JSON-RPC `tools/call` body.
- **Result.** One of three outcomes:
  - **success**.
  - **tool error**: the call reached Blender but the tool failed.
  - **HTTP error / network error**: shown with hints.
- **Activity log.** Every request, with its HTTP status and timing.
- **MCP methods.** A short reference for the protocol methods.
- **Raw JSON-RPC console.** Post any JSON-RPC message straight to the endpoint.

> **Call tool runs for real.** It acts on the scene open in Blender. Tools such as
> `delete_object`, `execute_blender_python`, `save_blend_file`, and `export_gltf`
> change the scene or write files.

### The server panel

Click the status pill to open it. Press Esc or click outside it to close it. It shows
one of four states:

| State | Meaning | Buttons |
| --- | --- | --- |
| **running** | A Python process is listening on port 8765. Its PID and endpoint are shown. | Restart, Stop |
| **stopped** | Nothing is listening on port 8765. | Start server |
| **starting** | The server was launched but has not opened its port yet. | — |
| **port busy** | Another, non-Python program holds port 8765. The page will not touch it; close that program yourself. | — |

**Refresh** and **Show log** are always available. **Show log** shows the last 40
lines of the server log and the log file's path. **Reconnect** appears when the server
is running but the page is not connected.

After **Start server** or **Restart**, the page checks the status every 0.5 s until
the port opens (for up to 45 s), then connects automatically. If the process exits
first, or the port does not open in time, the panel shows the error and opens the log.

---

## How server-control.php works

### API

| Request | Effect |
| --- | --- |
| `GET  server-control.php?action=status` | Returns a status snapshot. |
| `POST server-control.php?action=start` | Starts the server unless it is already running or starting. |
| `POST server-control.php?action=restart` | Stops the server, then starts it. |
| `POST server-control.php?action=stop` | Stops the server. |

- Every response is JSON: `{ ok, message?, error?, status }`.
- POST requests must carry the header `X-Requested-With: server-control`. Other
  websites cannot add a custom header without a CORS preflight, which this script never
  approves, so only this page can trigger the actions.
- Only requests from this computer (`127.0.0.1` / `::1`) are accepted.

You can call it from PowerShell too:

```powershell
$control = 'http://localhost/blender-mcp/server-control.php'   # your Apache URL for this folder
(Invoke-RestMethod "$control?action=status").status
Invoke-RestMethod "$control?action=restart" -Method Post -Headers @{ 'X-Requested-With' = 'server-control' }
```

### Starting

1. **Finds Python.** It uses `PYTHON_EXE` if that is set. Otherwise it takes the first
   `python.exe` on Apache's PATH, skipping the Microsoft Store alias.
2. **Runs the manual command.** It runs `python -u blender-mcp-server.py --mcp-port
   8765` from this folder, the same as starting it by hand. The command is wrapped in
   `cmd.exe`, which redirects stdout and stderr to the log and sets
   `PYTHONIOENCODING=utf-8`.
3. **Launches through WMI.** It creates the process with `Win32_Process.Create`,
   with its window hidden. The server is not a child of Apache, so:
   - it inherits none of Apache's sockets or handles,
   - the request returns at once, and
   - the server keeps running when Apache restarts.
4. **Records the PID.** It saves the PID of the `cmd.exe` wrapper in `state.json`.

### Status

- `netstat -ano` finds the process listening on port 8765.
- `tasklist` gets that process's image name. If the name is `python*.exe`, the server
  counts as **running**.
- The server counts as **starting** when nothing is listening yet, the wrapper from
  `state.json` is still alive, and it was launched less than 60 s ago.

### Stopping

- It runs `taskkill /PID <listener> /T /F`, then waits up to 10 s for the port to close.
- It refuses to kill a non-Python process that holds the port.
- If the server was launched but is not listening yet, it kills the `cmd.exe` wrapper.
  It first checks that the wrapper's command line is its own, because Windows reuses
  PIDs.
- Only one start/stop runs at a time, enforced by a file lock.

### Files it writes

These files live in PHP's temp folder, under `blender-mcp-control\`. When Apache runs
as your user, that is `%TEMP%\blender-mcp-control\`. When Apache runs as a Windows
service, it is usually `C:\Windows\Temp\blender-mcp-control\`. The panel shows the
exact log path under **Show log**.

| File | Contents |
| --- | --- |
| `blender-mcp-server.log` | Output of the current run. |
| `blender-mcp-server.prev.log` | Output of the previous run, rotated on each start. |
| `state.json` | PID and start time of the last launch. |
| `control.lock` | Lock file that serializes actions. |

### Configuration

Constants at the top of [server-control.php](server-control.php):

| Constant | Default | Purpose |
| --- | --- | --- |
| `MCP_PORT` | `8765` | Port passed as `--mcp-port` and watched for status. |
| `PYTHON_EXE` | `''` | Full path to `python.exe`. Empty means the first one on Apache's PATH. |
| `SERVER_SCRIPT` | `blender-mcp-server.py` | Script to run, relative to this folder. |
| `STOP_TIMEOUT` | `10` | Seconds to wait for the port to close after stopping. |
| `START_GRACE` | `60` | Seconds a launch still counts as "starting". |

The page only ever passes `--mcp-port`. For other options (Blender port, timeout, extra
origins), start the server from PowerShell. If you change `MCP_PORT`, also change
`DEFAULT_ENDPOINT` near the top of the script in `index.html`.

### Mixing manual and page control

The panel identifies the server by whatever Python process listens on port 8765:

- A server started from PowerShell (with `start-server.ps1` or by hand) shows as
  **running**, and **Stop** and **Restart** work on it.
- If you did not start one from PowerShell, **Start server** launches it.
- **Restart** relaunches it the page's way: default options, with output going to the
  panel's log.
- A manually started server's output is not in the panel's log. It goes to your
  PowerShell window or to your redirect files.

---

## Connecting an MCP client

The endpoint is `http://127.0.0.1:8765/mcp` (Streamable HTTP, stateless, JSON
responses). For Claude Code:

```powershell
claude mcp add --transport http blender-mcp http://127.0.0.1:8765/mcp
```

Other clients take the equivalent JSON config:

```json
"blender-mcp": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `[Errno 10048] error while attempting to bind on address ('127.0.0.1', 8765)` | A server is already running, possibly one started from the page. Use it, or stop it first ([see above](#checking-and-stopping-a-running-server)). `start-server.ps1` checks for this before starting. |
| Tools fail with `Connection refused by Blender at 127.0.0.1:9876` | Blender or its Blender Lab listener is not running. Start it, or pass `--blender-port` if it listens elsewhere. |
| Tools fail with `Timed out after 30s communicating with Blender` | Blender is busy or the operation is slow. Restart the server with a larger `--socket-timeout`. |
| Panel: `server-control.php is not available` | The page was not opened through Apache, or Apache is stopped. Use the Apache URL, or start the server from PowerShell. |
| Panel: `python.exe was not found on Apache's PATH` | Set `PYTHON_EXE` in `server-control.php` to the full path of `python.exe`. |
| Panel: **port busy** | Another program holds port 8765. Close it, or change the port in both the PHP file and the page. |
| Panel: start fails or times out | Click **Show log**. The usual causes are a missing Python package or the wrong interpreter (set `PYTHON_EXE`). |
| Page: **403**, origin rejected | The page was opened from `file://` or a non-loopback origin. Use one of the URLs above, or start the server with `--allow-origin` and `--allow-host`. |
| Page: **421**, host rejected | The request's `Host` header is not on the allow-list (common behind a reverse proxy). Add `--allow-host <host>`. |
| `UnicodeEncodeError` in a redirected log | Set `$env:PYTHONIOENCODING = 'utf-8'` before starting. |
| Clients report chunked-encoding errors | Security software can rewrite HTTP responses. The server works around this; see [HTTP-TRANSPORT-DIAGNOSIS.md](HTTP-TRANSPORT-DIAGNOSIS.md). |

## Security

The server gives full Python access to your running Blender (`execute_blender_python`,
among others) to anything that can reach the endpoint.

- By default it listens on `127.0.0.1` only.
- Its DNS-rebinding guard and Origin allow-list stop pages on other websites from
  calling it.
- Do not use `--mcp-host 0.0.0.0` or a LAN address unless every device on that network
  is trusted. The server prints a warning when you do.

## Tests

```powershell
python -m unittest -v test_http_transport
```

The tests start their own server on a free port and never contact Blender, so they run
fine while the real server is up.
