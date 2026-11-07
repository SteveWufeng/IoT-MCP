# Internals, verification, and decisions

Companion to the README for anyone about to change this code, or decide whether to trust a
platform it has not been run on.

## What is actually verified

This was developed and verified on **one machine**: CachyOS, Hyprland (Wayland), Python 3.15,
`mcp` 2.3.0, `zeroconf` 0.151.5. That is the honest boundary between "tested" and "written
carefully".

### Executed here (600 passing + 4 xfail tests, plus live checks)

- Linux driver end to end against the real session: the real app list (78 `.desktop` entries on
  this machine), real MPRIS `media_status`, 5 real Hyprland windows read through the real
  `hyprctl`, and real launch and real termination of a real process.
- Server + client over real HTTP on loopback, using the real MCP SDK client — including a forged
  `Host:` header being rejected and `file://` being refused before any spawn.
- Bearer authentication over in-process uvicorn, with unauthenticated and wrong-token requests
  rejected before the MCP handshake, valid-token SDK connections succeeding, repeated failures
  throttled, and the client’s token precedence/header plumbing exercised.
- Browser control state and the dynamic pass-through contract against a fake bridge: approval and
  request separation, tool registration/collision skipping, verbatim upstream schema/argument
  forwarding, error-envelope preservation, and removal of page tools on stop. This does **not**
  start or exercise the real `@playwright/mcp` child process.
- Real mDNS advertise → browse → resolve on a LAN interface (resolved to a
  `http://<this host>:<port>/mcp` URL).
- `focus_window` was **not** exercised against the live compositor: a real call moves the
  operator's focus. It is covered by argv-recording tests against real `hyprctl` output, and by
  the no-op path (focusing the window that already has focus) — the same shape of coverage as the
  macOS and Windows window code below.
- `examples/smoke-test.sh` and the no-LangChain path of `examples/langgraph_agent.py`.
- macOS and Windows *parsing and quoting* logic (AppleScript escaping, PowerShell quoting, the
  script builder, window-title matching) — with every subprocess replaced by a recorder that
  asserts on the exact argv. These run on Linux; they do not need a Mac or Windows box.

### Reviewed by reading, never executed

The CI here is code review, not a Mac:

- Everything that actually touches macOS: `osascript`, `open -b`, `.app` bundle scanning,
  `pgrep`/`SIGTERM`, the per-player AppleScript dictionaries.
- Everything that actually touches Windows: the PowerShell runner, `SendInput` media keys,
  Start Menu enumeration, window handles.
- The KWin and GNOME window backends (they are *detected* here, and the detection is tested, but
  there is no KDE or GNOME session on this machine to drive).
- The X11 path via `wmctrl`/`xdotool` (not installed here; the code and its argv are tested against
  a stub). One known limitation lives here: `wmctrl` output is space-separated with no quoting, so
  `WM_CLASS` is taken as the last token and everything before it as the title. It is the same
  heuristic every other `wmctrl` wrapper uses, and it is why `-x` output is lossy for titles ending
  in a single word. See `iot_mcp/drivers/_linux_wayland.py`.
- The LangGraph branch of `examples/langgraph_agent.py` (no model credentials on this box).
- The real browser child process: `npx`, package download, Playwright/Chromium startup, and the
  upstream server’s actual tool behavior. Its lifecycle and protocol mapping are tested through a
  fake bridge; the real upstream integration has not been run by this repo’s default suite.

Treat those as plausibly wrong in detail. They follow documented APIs and every failure path
returns a typed error, but nobody has pressed the button.

### The four xfailed tests

They are deliberate, and they are findings, not skips. Each documents a defect the current code
really has:

| xfail | finding |
|---|---|
| `test_discovery_must_not_block_on_a_fifo_named_like_an_entry` | `discover_apps` `open()`s every `*.desktop` unconditionally, so a FIFO named like an entry blocks forever — and `LinuxDriver` calls it synchronously on cache refresh, freezing the event loop of the whole server |
| `test_protected_name_matching_should_be_case_insensitive` | the protected-process comparison should be case-insensitive |
| `test_cancelling_a_run_should_kill_the_child` | `_proc.run` kills children on its own deadline but not on external cancellation, so a cancelled `tools/call` orphans the helper process |
| `test_internal_errors_should_not_leak_exception_class_names` | `internal_error` reflects the Python exception type to unauthenticated callers |

## Platforms

`pixi.toml` locks `linux-64` only, because that is the platform whose resolution and test run were
observed. Adding `osx-arm64`/`win-64` produced a lockfile nobody had executed against, so it was
dropped rather than shipped. On macOS or Windows, add your platform to `platforms` and regenerate
the lock; `pip install .` (see `pyproject.toml`) is the platform-independent path and does not
depend on the lockfile at all.

## Design

```
iot_mcp/
  cli.py            server entrypoint: args -> ServerConfig -> auth/browser policy -> uvicorn
  app.py            core tool registration + optional browser controls; thin wrappers, no logic
  config.py         defaults, auth/browser policy helpers, TXT records, mDNS type
  auth.py           static bearer token, validation, throttle, ASGI middleware
  addons.py         approval records and agent-authored install requests
  addons_cli.py     human-only list/pending/request/install/revoke CLI
  addon_bridge.py   stdio supervisor and client session for an approved MCP add-on
  browser.py        browser controls + dynamic upstream pass-through
  tooling.py        shared tool result/annotation helpers
  discovery.py      zeroconf advertise + browse, both optional
  capabilities.py   driver/browser capability vocabulary
  errors.py         DriverError and its subclasses; the stable error envelope
  drivers/
    base.py         AutomationDriver interface, capability matrix, ok()
    linux.py        XDG apps, xdg-open, playerctl/MPRIS, /proc app matching
    _linux_wayland.py  compositor windows: Hyprland, KWin/GNOME over D-Bus, X11 tools
    macos.py        osascript, open, bundle scanning, AppleScript media dictionaries
    windows.py      PowerShell runner, SendInput media keys, window enum
    _proc.py        the only place driver subprocesses are created: argv lists, deadlines, caps
    _url.py         the shared URL allowlist
    _winutil.py     window matching shared by every platform
    _xdg.py         .desktop parsing and app-name matching
  client/           discovery + connect + tools/call, token plumbing, and the REPL CLI
tests/              604 tests (600 passing + 4 findings), fenced so nothing touches the desktop
examples/           client configs, curl smoke test, LangGraph agent
skills/             agent-facing skill + a stdlib-only MCP client
```

Four invariants worth knowing before you add a tool:

1. **Capability detection is lazy and reported, never assumed.** A missing `playerctl` shows up in
   `list_actions()` as `dependency_missing` with the package name in the hint. Nothing crashes at
   import time because a CLI is absent.
2. **Driver subprocesses exist only in `_proc.py`.** `run()` gives you `Completed` with capped
   capture and a deadline; `spawn_detached`/`spawn_monitored` start GUI apps that outlive the server
   and never become zombies. The one non-driver process owner is `addon_bridge.py`, which exists to
   supervise a separately approved stdio MCP add-on.
3. **Authentication wraps the complete ASGI application.** The CLI resolves token settings and the
   exposed-browser policy before opening the driver/socket, then wraps `streamable_http_app()` rather
   than only the MCP route. That way a future health/root route cannot become an unauthenticated
   exception.
4. **Approval is filesystem policy, while registration is merely enablement.** `--browser` registers
   control tools only. `iot-mcp-addons install browser` writes the approval record pinning exact
   argv; `browser_start` may create a bridge when that record exists, and `browser_stop` tears the
   bridge down and unregisters its dynamic tools.

### Auth request path

```
HTTP/WS scope
  -> bearer_auth_middleware
       -> per-source sliding-window throttle
       -> extract_token from Authorization, X-Api-Key, X-IoT-MCP-Token, optional query
       -> constant-time verify
  -> MCP Streamable HTTP app
```

The middleware intentionally understands the ASGI scope directly rather than adding a web framework.
Query strings are refused by default and produce a distinct hint. Failure responses are JSON
`401`/`429` objects so an agent can recover rather than retry an opaque protocol failure forever.

### Add-on state and browser pass-through

`AddonManager` stores policy as JSON below `$XDG_CONFIG_HOME/iot-mcp` or `~/.config/iot-mcp`,
overridable with `IOT_MCP_ADDON_ROOT`:

```
requests/<name>-<timestamp>-<pid>-<uuid>.json   agent-authored, untrusted
entries/<name>.json                              human-approved, trusted policy
```

`fingerprint_argv()` hashes a length-prefixed canonicalization of argv, so shell-like string
concatenation cannot forge a boundary collision. `AddonEntry.matches()` compares the exact approved
fingerprint at launch.

`AddonBridge` owns one stdio MCP client session. Its supervisor task owns lifecycle so start/stop do
not depend on the ASGI request that happened to trigger them. Page tool names are normally mapped
into the `browser_` namespace; names already using that prefix pass through unchanged.

Pass-through deliberately uses a small amount of SDK private surface:

- The upstream `Tool` is inserted into `MCPServer._tool_manager._tools`.
- `_PassThroughTool` overrides `run()` to bypass signature-derived validation because the handler has
  no static schema it could validate against.
- Removal uses `MCPServer.remove_tool()`.
- The published `inputSchema` and description are upstream’s, not a locally maintained mirror.
- `_preparse()` decodes JSON text into objects/arrays only when upstream schema declares those
  types, preserving strings and unparseable values.
- Collisions with the five control tools or existing registered names are skipped, never shadowed.

`mcp>=2.3,<3` is pinned because these are internal touchpoints. If they move, registration fails
loudly instead of silently exposing a broken tool set.

## Development

```sh
pixi run test           # 600 tests + 4 xfail findings
pixi run lint
pixi run format-check
```

For a manual run without touching the network, see
[Smoke test without any network](troubleshooting.md#smoke-test-without-any-network).

The suite fences every module that can put a process on the machine: an autouse fixture replaces
`run`, both spawn helpers and `asyncio.create_subprocess_exec` with a tripwire that fails the test.
Two places opt out by marker (`allow_real_processes`): one test that proves a real process really
gets terminated, and the end-to-end module, whose whole purpose is a real server and a real client.
That fence exists because an early version of the window tests patched only the driver module while
the window code called through a helper module — and the suite proceeded to move the operator's
keyboard focus with real `hyprctl` calls. Patch one module and the next one is still live.

Two environment notes, because both look like accidents and neither is:

- `[tool.pytest.ini_options] pythonpath` exists because pixi *materialises* an environment rather
  than installing this package into it. Without it, `import iot_mcp` depends on which directory
  pytest was started from.
- Development happened inside an AgentFS sandbox, where running the suite needed `PIXI_HOME` and
  `PYTHONPYCACHEPREFIX` pointed at a non-copy-on-write filesystem, and `pixi.toml`'s
  single-platform lock made a much smaller lockfile to carry through that environment. Outside a
  sandbox none of that applies — `pixi run test` works as written above.

## Decisions worth knowing

- **`mcp` 2.x, not 1.x.** conda-forge resolves 2.3.0, and 2.x renamed `FastMCP` to `MCPServer` in
  `mcp.server.mcpserver`. The upper pin is also protection for the browser pass-through’s narrow
  private-registry touchpoint.
- **Authentication is one static bearer token, not a user system.** The endpoint controls one
  desktop, so the minimum meaningful boundary is shared-secret authorization. Adding accounts/RBAC
  without TLS and session management would add more state than risk reduction.
- **Browser capabilities are not driver capabilities.** Driver capability answers describe the OS
  integration; browser tools are an operator-approved add-on with separate lifecycle, approval state,
  and risk surface.
- **A Hyprland backend exists** beyond the original brief, because this is a Hyprland machine and a
  window tool that cannot be run cannot be verified. KWin and GNOME are detected over D-Bus and
  report what to enable; neither is driven.
- **Windows `media_status` reports "unsupported" rather than guessing.** The media keys go through
  `SendInput`, which is a one-way door: nothing there can read SMTC back. A capability that says no
  is better than one that returns a plausible lie.
- **Failures are data.** A driver error becomes `{"ok": false, "error": {...}}` with `isError` left
  unset, so the model reads a stable shape instead of a protocol-level exception whose text varies
  by SDK version.
- **`_appmcp._tcp`, not `_mcp._tcp`.** Why the label is what it is, and why a mismatch fails
  silently: see [Discovery naming](discovery.md#discovery-naming).
- **Linux-only lockfile.** See [Platforms](#platforms).

## Status

MVP. Optional static bearer authentication, no TLS, no Windows/macOS field verification, one
platform’s lockfile, an optional browser add-on whose real child process is unexercised by default,
and a media layer that depends on `playerctl` (Linux), AppleScript dictionaries (macOS) and
`SendInput` (Windows). What is here was chosen because it is verifiable: no shell interpolation,
deadlines on subprocesses, and a capability answer that admits what it cannot do.
