# IoT-MCP

OS automation exposed as [Model Context Protocol](https://modelcontextprotocol.io) tools.

Run one command on a machine whose desktop you want to drive (this repo calls it **computer
B**). It serves MCP over Streamable HTTP and announces itself on the LAN with mDNS. From
another machine (**computer A**), any MCP client — opencode, Claude, LangGraph, or the client
in this repo — finds it and calls `open_app`, `media_play_pause`, `focus_window` and friends.

The point is that the model is on one computer and the desktop is on another. A stdio MCP
server cannot express that; a remote HTTP MCP server can.

```
computer A (model + MCP client)          computer B (this server, user session)
  opencode / Claude / LangGraph    ──►     http://B:8000/mcp
  iot-mcp-client                          ├─ Linux / macOS / Windows driver
                                          ├─ apps · urls · media · windows
                                          └─ mDNS: _appmcp._tcp (configurable)
```

## Quick start

### Computer B — serve

```sh
pixi install                       # once
pixi run server --expose           # binds 0.0.0.0:8000, prints a LAN warning
```

`--expose` is required for a *remote* client because the default bind is `127.0.0.1`; see
[Security](#security). If you only want to drive your own machine, leave it off and use an
SSH tunnel. And if computer A does not reach computer B on the first try, it is almost always
the firewall or the network segment, not this code — see
[Network & firewall prerequisites](#network--firewall-prerequisites).

The banner tells you what was detected:

```text
iot-mcp 0.1.0 | linux driver: 10/10 capabilities — apps.list, apps.open, apps.close,
url.open, media.play_pause, media.next, media.previous, media.status, windows.list, windows.focus
mDNS: advertising http://192.168.0.54:8000/mcp
listening: http://0.0.0.0:8000/mcp
```

Flags worth knowing: `--port`, `--path`, `--driver` (force a backend), `--no-mdns`,
`--mdns-type`, `--allow-host` (keep Host validation on and name the hosts to accept),
`--stateless` (for clients that cannot hold a session id). `python -m iot_mcp --help` is
authoritative.

### Computer A — drive

```sh
pixi run discover                  # what is out there?
pixi run client                    # discover, connect, then a tool prompt
pixi run client -- --call media_status
pixi run client -- --call open_app --args '{"name":"firefox"}' --json
```

Or with the installed console script, skipping discovery:

```sh
iot-mcp-client --url http://192.168.0.54:8000/mcp --call list_actions --json
```

Exit codes are scripting-friendly: `0` success, `1` nothing discovered or a tool that
reported `ok: false`, `2` a usage/config problem, `130` interrupted. `--json` prints the
driver's envelope verbatim, so `jq` works on it:

```sh
iot-mcp-client --url … --call media_status --json | jq -r '.playing.title // "nothing playing"'
```

`examples/` has the equivalents for opencode, Claude Code/Desktop, and LangGraph, plus
`smoke-test.sh`, which speaks MCP with nothing but curl.

## Tools

Eleven tools, one JSON envelope per result. Call `list_actions` first — it says what this
machine can actually do right now and why the rest cannot.

| tool | what it does | side effect |
|---|---|---|
| `list_actions` | capability matrix, with reasons and remediation | none |
| `list_apps` | launchable applications (XDG entries / `.app` bundles / Start Menu) | none |
| `open_app` | launch by fuzzy name or id | starts a process |
| `close_app` | quit an app, graceful then forced | **can lose unsaved work** |
| `open_url` | hand a URL to the default handler | opens a browser |
| `media_play_pause` / `media_next` / `media_previous` | transport controls | changes playback |
| `media_status` | player, status, track, artist, album, URL | none |
| `list_windows` | windows with app, title, workspace, focus | none |
| `focus_window` | raise+focus by app or title substring | **steals input focus** |

Every result is `{"ok": true, "capability": ..., ...}` or `{"ok": false, "error": {...}}`.
The two tools that can disrupt what the user is doing — `close_app` and `focus_window` — are
marked with MCP's `destructiveHint` annotation (and `open_url` with `openWorldHint`), so a
client can require confirmation without parsing prose. A failing tool comes back as data
inside that envelope rather than as an MCP protocol error, so a model sees "why" instead of a
stack trace:

```json
{
  "ok": false,
  "error": {
    "type": "dependency_missing",
    "message": "media.status unavailable: 'playerctl' is not installed",
    "capability": "media.status",
    "hint": "Install playerctl (package 'playerctl' on Arch/Fedora, 'playerctl' on Debian).",
    "details": {"driver": "linux"}
  }
}
```

## Discovery

The server registers its service with TXT records (`path=/mcp`, `transport=streamable-http`,
`name`, `version`) so a client can build the full URL without being told it. Four things will
bite you, so they are stated up front:

- **mDNS is multicast.** It does not cross most routers, VLANs, VPN clients, Docker bridges
  or guest Wi-Fi. When browsing finds nothing, connect with `--url`.
- **`zeroconf` is optional.** If it cannot be imported, or Avahi is masked, the server says so
  and serves anyway; `--no-mdns` skips the attempt. Nothing else about the server depends on
  discovery.
- **The published addresses follow the bind.** An `--expose` server publishes its non-loopback
  IPv4 addresses; a server bound to loopback publishes only the loopback address, so whatever a
  browser resolves is something that will actually accept the connection. LAN IPv6 addresses are
  not published. When an answer carries more than one address the client dials the most routable
  one — non-loopback before loopback, IPv4 before IPv6, the family a LAN client is most likely to
  have a route for.
- **Prefer one server per machine.** Every instance advertises under the same mDNS hostname
  (`Cachy-Y7.local` here), so a loopback-bound instance and an `--expose` one started at the same
  time publish different address records under one name, and a browser's cache merges them. The
  routable-first ordering keeps the merged answer safe — the client dials the LAN address, never
  its own loopback — but it then points *both* instances at that address, so while both are up,
  reach the loopback one with `--url`. Seen on the demo host.

### Discovery naming

The default service type is **`_appmcp._tcp`**.

DNS-SD service types are drawn from the same RFC 6335 registry as TCP/UDP service names, and
`mcp` is already assigned in it: **port 4458/tcp+udp, "Matrix Configuration Protocol",
registered 2006-12** ([the registry](https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.xhtml?search=mcp)).
So `_mcp._tcp` — the label a number of MCP projects use today — is a borrowed name, and a
DNS-SD lookup on it is ambiguous about which `mcp` answers. `_appmcp` is unassigned, and at 7
characters its `_appmcp._tcp` form sits comfortably inside the 15-character DNS label limit.
(`_mcp._tcp` fits the length limit too; the objection is the collision, not the length.) This is
also the value the local-mcp-kit `local-discovery-v1` discovery profile uses.

**The failure mode is silent, so understand it before a demo.** A client browsing `_mcp._tcp`
will simply not see a server advertising `_appmcp._tcp`. The client gets an empty list, the
server logs nothing, and no error is raised anywhere — it looks identical to "the server is
down" or "mDNS doesn't work here". Both ends must name the same label. If your client only ever
browses the community label, start the server with `--mdns-type _mcp._tcp`; that value is still
fully supported and normalized identically, it just is not the default.

The type is a configuration value in exactly one place — `DEFAULT_MDNS_TYPE` in
`iot_mcp/config.py` — overridable by `--mdns-type` (server *and* client) or
`IOT_MCP_MDNS_TYPE`, and imported everywhere else. `tests/test_config_and_cli.py` fails if a
second copy of the literal appears anywhere in the package.

## Network & firewall prerequisites

**Nothing in this repository has changed any firewall.** The commands below are for whoever
operates machine B, to run by hand on that machine, and they need root. They are written out
because the failure they prevent is easy to misread as a bug in this code.

The numbers are what was measured on the development host (`Cachy-Y7`, Arch/CachyOS, UFW
active) — yours will differ, so read them as a worked example, not as your machine's state:

| checked | result | consequence |
|---|---|---|
| UFW default input policy | `DROP` (`DEFAULT_INPUT_POLICY="DROP"` in `/etc/default/ufw`) | inbound 8000 is denied unless explicitly allowed |
| TCP 8000 in the allow-list | **not present** (open TCP ports were 22, 47984, 47989, 47990, 48010) | remote clients cannot connect yet |
| mDNS | already permitted — `before.rules` explicitly ACCEPTs `udp 224.0.0.251:5353` | discovery works with no change |

### The trap: discovery succeeds, the connection hangs

Read that table again, because those three rows combine into the one failure that will cost you
an afternoon.

**mDNS is already allowed; port 8000 is not.** So computer A on Wi-Fi resolves
`Cachy-Y7.local:8000` perfectly — `pixi run discover` prints the URL, the instance name, the
TXT records, all of it — and then the TCP connect to port 8000 is silently dropped and sits
there until it times out. Nothing on B is wrong. The MCP code on both sides is fine. Discovery
answered and then the connection black-holed, and the error you eventually get says "timeout",
which points at every part of this stack except the one that is actually responsible.

Tell the two apart by how long the failure takes:

- **hangs, then times out** → packets are being dropped. Firewall or a route. This section.
- **instant `connection refused`** → something answered and disliked it. Wrong port, server bound
  to `127.0.0.1` only, or the `Host` check rejecting you (that one answers `421`, not a refusal).

To check from A in five seconds without any MCP client:

```sh
curl -sv --max-time 5 http://192.168.0.54:8000/mcp -o /dev/null
```

A timeout with **no HTTP status at all** is this section. Any status — even `400 Bad Request` or
`421 Misdirected Request` — means the packets arrived, and the firewall is not your problem.

### Opening the port (operator runs these on B)

```sh
sudo ufw allow 8000/tcp comment 'MCP demo (temporary)'
sudo ufw status verbose | grep 8000
```

And when the demo is over — the server has no authentication, so leaving this open is leaving
the desktop open to the LAN:

```sh
sudo ufw delete allow 8000/tcp
```

Not UFW? Find out what is actually in play before guessing (running two firewalls at once is a
real thing):

```sh
systemctl is-active ufw firewalld nftables iptables
sudo firewall-cmd --add-port=8000/tcp --permanent && sudo firewall-cmd --reload   # firewalld
```

`nft`/`iptables` rulesets are machine-specific; add the accept to whichever chain owns inbound
traffic. Do not flush a ruleset to "see if it helps".

### Pre-flight checklist for a Wi-Fi demo

- **Same L2 segment is required.** mDNS is link-local multicast and does not route. Two hosts on
  the same AP/switch: fine. Two VLANs, or a guest network: discovery fails silently, no error.
- **AP / client isolation kills exactly this.** Many ISP routers drop wired↔wireless and
  wireless↔wireless traffic by default. It is the most common silent killer here, and no amount
  of configuration on either computer will fix it from inside.
- **This host is on ethernet, not Wi-Fi.** `wlan0` is `DOWN`; the address is on `enp5s0` at
  `192.168.0.54`. So a Wi-Fi laptop talking to it crosses the wired/wireless boundary — the
  precise case the previous bullet breaks. Test wired-to-wired first if you can.
- **There is a live `ppp0` tunnel on this host.** The default route is still via `enp5s0`, so
  normal LAN traffic should not touch it — but if answers come back from a strange address, or
  replies go out the wrong interface, treat `ppp0` as the first suspect and check
  `ip route get 192.168.0.<A>` on B.
- **A responder must actually be running.** Linux: `systemctl is-active avahi-daemon`. macOS
  ships `mDNSResponder` and needs nothing. Windows needs Bonjour (installed with iTunes/Apple
  software; without it, mDNS is simply absent — use `--url`).
- **Loopback needs no firewall change at all.** UFW permits loopback, so `127.0.0.1` works
  today, on this machine, with nothing opened. Use that for the rehearsal (below) and open
  8000/tcp only for the actual cross-machine moment.
- **Loopback and discovery now agree.** A server bound to loopback advertises the loopback
  address — `127.0.0.1`, or `::1` if that is what it bound — and nothing else. It used to advertise
  its LAN address (`192.168.0.54` here), which the bind was not listening on, so the only client
  that could have used the answer got a crisp `Connection refused`. The published address is now
  the address that accepts: `pixi run server` plus `pixi run client`, both with **no flags on one
  machine**, works. One hazard survives and you should know about it: mDNS registration is
  independent of the TCP bind, so a *remote* browser sees a loopback advertisement too, and
  dialling `127.0.0.1` from A connects A to itself. For the cross-machine demo the server must run
  `--expose` — that is not a preference. Separating the two clients requires knowing which machine
  published the record, which this MVP does not do.

### Smoke test without any network

The zero-risk rehearsal, before any firewall change and with nothing exposed:

```sh
# terminal 1 on B — loopback, no mDNS, nothing reachable from the LAN
pixi run server --port 8123 --no-mdns

# terminal 2 on B
examples/smoke-test.sh http://127.0.0.1:8123/mcp
```

That exercises the whole server — transport, session handling, tool listing, one real tool call
— with no firewall, no multicast, no routing and no exposure. `--no-mdns` is there to keep even
the multicast packets out of the rehearsal, not because discovery would lie to you: on a loopback
bind it now advertises loopback, and `pixi run discover` on the same machine resolves it. If the
smoke test passes, the server is not what is wrong when the Wi-Fi demo fails; work down the
checklist above instead.

## Security

**This server has no authentication.** No user, no token, no origin check. It exists behind
these deliberate constraints:

- Binds `127.0.0.1` unless you pass `--expose`, which prints a block of text on stderr saying
  exactly what an attacker on the network can do. Read it; it is not decorative.
- The MCP SDK's DNS-rebinding protection stays **on** by default. `--expose` turns it off
  (a LAN client's `Host:` header would otherwise be rejected), and `--allow-host myhost:8000`
  lets you keep the protection and name the hosts you accept instead.
- `open_url` validates against a scheme allowlist (`http`, `https`, `mailto`, `ftp`, `ftps`)
  before any helper runs. `file://`, URLs with embedded credentials, whitespace or control
  characters are refused.
- No driver hands a string to a shell. Everything is an argv list to `execvp` (or
  PowerShell's stdin with caller data in environment variables), and every subprocess has a
  deadline. An app named `; rm -rf /` is data.
- Destructive tools are annotated rather than blocked — a model that cannot focus a window is
  not much use. The exception is a short refusal list where the damage is disproportionate to
  the request: the macOS backend will not quit `loginwindow`, `WindowServer`, `Dock` or
  `Finder`, because closing those is a logged-out user, not an automated task.
- Not hardened against a *local* attacker: a FIFO named `*.desktop` planted in a scanned XDG
  applications directory can wedge app discovery (pinned as a known limitation, fix is post-demo
  work). It needs write access to this machine's own filesystem, so it is not a LAN-facing risk.

For anything beyond a trusted LAN: leave it on loopback and tunnel
(`ssh -L 8000:127.0.0.1:8000 user@host`), or put an authenticating reverse proxy in front.

## Platforms and verification status

This was developed and verified on one machine: CachyOS, Hyprland (Wayland), Python 3.15,
`mcp` 2.3.0, `zeroconf` 0.151.5. That is the honest boundary between "tested" and "written
carefully".

**Executed here (362 tests + live checks):**

- Linux driver end to end against the real session: the real app list (78 `.desktop` entries on
  this machine), real MPRIS `media_status`, 5 real Hyprland windows read through the real
  `hyprctl`, and real launch and real termination of a real process.
- Server + client over real HTTP on loopback, using the real MCP SDK client — including a
  forged `Host:` header being rejected and `file://` being refused before any spawn.
- Real mDNS advertise → browse → resolve on a LAN interface (resolved to a
  `http://<this host>:<port>/mcp` URL).
- `focus_window` was **not** exercised against the live compositor: a real call moves the
  operator's focus. It is covered by argv-recording tests against real `hyprctl` output, and by
  the no-op path (focusing the window that already has focus) — the same shape of coverage as
  the macOS and Windows window code below.
- `examples/smoke-test.sh` and the no-LangChain path of `examples/langgraph_agent.py`.
- macOS and Windows *parsing and quoting* logic (AppleScript escaping, PowerShell quoting, the
  script builder, window-title matching) — with every subprocess replaced by a recorder that
  asserts on the exact argv. These run on Linux; they do not need a Mac or Windows box.

**Reviewed by reading, never executed** — the CI here is code review, not a Mac:

- Everything that actually touches macOS: `osascript`, `open -b`, `.app` bundle scanning,
  `pgrep`/`SIGTERM`, the per-player AppleScript dictionaries.
- Everything that actually touches Windows: the PowerShell runner, `SendInput` media keys,
  Start Menu enumeration, window handles.
- The KWin and GNOME window backends (they are *detected* here, and the detection is tested,
  but there is no KDE or GNOME session on this machine to drive).
- The X11 path via `wmctrl`/`xdotool` (not installed here; the code and its argv are tested
  against a stub).
- The LangGraph branch of `examples/langgraph_agent.py` (no model credentials on this box).

Treat those as plausibly wrong in detail. They follow documented APIs and every failure path
returns a typed error, but nobody has pressed the button.

### Platforms

`pixi.toml` locks `linux-64` only, because that is the platform whose resolution and test run
were observed. Adding `osx-arm64`/`win-64` produced a lockfile nobody had executed against, so
it was dropped rather than shipped. On macOS or Windows, add your platform to `platforms` and
regenerate the lock; `pip install .` (see `pyproject.toml`) is the platform-independent path
and does not depend on the lockfile at all.

## Design

```
iot_mcp/
  cli.py            server entrypoint: args -> ServerConfig -> uvicorn
  app.py            the eleven MCP tools; thin wrappers, no logic
  config.py         defaults, and the only definition of the mDNS type
  discovery.py      zeroconf advertise + browse, both optional
  capabilities.py   the capability enum and the matrix shape list_actions() returns
  errors.py         DriverError and its subclasses; the stable error envelope
  drivers/
    base.py         AutomationDriver interface, capability matrix, ok()
    linux.py        XDG apps, xdg-open, playerctl/MPRIS, /proc app matching
    _linux_wayland.py  compositor windows: Hyprland, KWin/GNOME over D-Bus, X11 tools
    macos.py        osascript, open, bundle scanning, AppleScript media dictionaries
    windows.py      PowerShell runner, SendInput media keys, window enum
    _proc.py        the only place subprocesses are created: argv lists, deadlines, caps
    _url.py         the shared URL allowlist
    _winutil.py     window matching shared by every platform
    _xdg.py         .desktop parsing and app-name matching
  client/           discovery + connect + tools/call, and the REPL CLI
tests/              362 tests, fenced so nothing touches the desktop it runs on
examples/           client configs, curl smoke test, LangGraph agent
```

Two invariants worth knowing before you add a tool:

1. **Capability detection is lazy and reported, never assumed.** A missing `playerctl` shows
   up in `list_actions()` as `dependency_missing` with the package name in the hint. Nothing
   crashes at import time because a CLI is absent.
2. **Subprocesses exist only in `_proc.py`.** `run()` gives you `Completed` with capped
   capture and a deadline; `spawn_detached`/`spawn_monitored` start GUI apps that outlive the
   server and never become zombies. If you find yourself typing `create_subprocess_exec`
   elsewhere, stop.

## Development

```sh
pixi run test           # 362 tests, ~8s
pixi run lint
pixi run format-check
```

For a manual run without touching the network, see
[Smoke test without any network](#smoke-test-without-any-network).

The suite fences every module that can put a process on the machine: an autouse fixture
replaces `run`, both spawn helpers and `asyncio.create_subprocess_exec` with a tripwire that
fails the test. Two places opt out by marker (`allow_real_processes`): one test that proves a
real process really gets terminated, and the end-to-end module, whose whole purpose is a real
server and a real client. That fence exists because an early version of the window tests
patched only the driver module while the window code called through a helper module — and the
suite proceeded to move the operator's keyboard focus with real `hyprctl` calls. Patch one
module and the next one is still live.

Two environment notes, because both look like accidents and neither is:

- `[tool.pytest.ini_options] pythonpath` exists because pixi *materialises* an environment
  rather than installing this package into it. Without it, `import iot_mcp` depends on which
  directory pytest was started from.
- Development happened inside an AgentFS sandbox, where running the suite needed `PIXI_HOME`
  and `PYTHONPYCACHEPREFIX` pointed at a non-copy-on-write filesystem, and `pixi.toml`'s
  single-platform lock made a much smaller lockfile to carry through that environment. Outside
  a sandbox none of that applies — `pixi run test` works as written above.

## Decisions worth knowing

- **`mcp` 2.x, not 1.x.** conda-forge resolves 2.3.0, and 2.x renamed `FastMCP` to
  `MCPServer` in `mcp.server.mcpserver`. Nothing else about the API used here moved.
- **A Hyprland backend exists** beyond the original brief, because this is a Hyprland machine
  and a window tool that cannot be run cannot be verified. KWin and GNOME are detected over
  D-Bus and report what to enable; neither is driven.
- **Windows `media_status` reports "unsupported" rather than guessing.** The media keys go
  through `SendInput`, which is a one-way door: nothing there can read SMTC back. A capability
  that says no is better than one that returns a plausible lie.
- **Failures are data.** A driver error becomes `{"ok": false, "error": {...}}` with
  `isError` left unset, so the model reads a stable shape instead of a protocol-level
  exception whose text varies by SDK version.
- **Linux-only lockfile.** See [Platforms](#platforms).

## Status

MVP. No authentication, no TLS, no Windows/macOS field verification, one platform's lockfile,
and a media layer that depends on `playerctl` (Linux), AppleScript dictionaries (macOS) and
`SendInput` (Windows). What is here was chosen because it is verifiable: no shell
interpolation, deadlines on every subprocess, and a capability answer that admits what it
cannot do.
