# IoT-MCP

**Drive one computer's desktop from another computer's model.**

OS automation exposed as [Model Context Protocol](https://modelcontextprotocol.io) tools. Run one
command on the machine you want to drive (here, **computer B**). It serves MCP over Streamable HTTP
on port 8000 and announces itself on the LAN with mDNS. From another machine (**computer A**), any
MCP client — opencode, Claude, LangGraph, or the client in this repo — finds it and calls
`open_app`, `media_play_pause`, `focus_window` and friends.

A stdio MCP server cannot express "the model is over here, the desktop is over there." A remote
HTTP MCP server can.

```
computer A (model + MCP client)          computer B (this server, user session)
  opencode / Claude / LangGraph    ──►     http://B:8000/mcp
  iot-mcp-client                          ├─ Linux / macOS / Windows driver
                                          ├─ apps · urls · media · windows
                                          └─ mDNS: _appmcp._tcp (configurable)
```

## Quick start

**On computer B — serve:**

```sh
pixi install                    # once
pixi run server --expose        # binds 0.0.0.0:8000, prints a LAN warning
```

`--expose` is required for a *remote* client, because the default bind is `127.0.0.1`. Driving only
your own machine? Leave it off and use an SSH tunnel. The banner reports what was detected:

```text
iot-mcp 0.1.0 | linux driver: 10/10 capabilities — apps.list, apps.open, apps.close,
url.open, media.play_pause, media.next, media.previous, media.status, windows.list, windows.focus
mDNS: advertising http://192.168.0.54:8000/mcp
listening: http://0.0.0.0:8000/mcp
```

Useful flags: `--port`, `--path`, `--driver`, `--no-mdns`, `--mdns-type`, `--allow-host`,
`--stateless`. `python -m iot_mcp --help` is authoritative.

**On computer A — drive:**

```sh
pixi run discover                                        # what is out there?
pixi run client                                          # discover, connect, prompt
pixi run client -- --call media_status
pixi run client -- --call open_app --args '{"name":"firefox"}' --json
```

Or skip discovery and name the URL (the path is always `/mcp`):

```sh
iot-mcp-client --url http://192.168.0.54:8000/mcp --call list_actions --json
```

Exit codes are scripting-friendly: `0` success · `1` nothing discovered, or a tool reported
`ok: false` · `2` usage/config problem · `130` interrupted. `--json` prints the driver's envelope
verbatim, so `jq` works on it:

```sh
iot-mcp-client --url … --call media_status --json | jq -r '.playing.title // "nothing playing"'
```

### Other clients

| Client | How |
|---|---|
| opencode | [`examples/opencode.jsonc`](examples/opencode.jsonc) |
| Claude Code / Desktop | [`examples/claude.md`](examples/claude.md) |
| LangGraph | [`examples/langgraph_agent.py`](examples/langgraph_agent.py) |
| Anything, no MCP client | [`examples/smoke-test.sh`](examples/smoke-test.sh) — speaks MCP with curl alone |
| An AI agent, generically | [`skills/iot-mcp/SKILL.md`](skills/iot-mcp/SKILL.md) — tiers, safety rules, and a stdlib-only client at [`scripts/iot_call.py`](skills/iot-mcp/scripts/iot_call.py) |

## Won't connect? (90% of problems)

| Symptom | Cause | Fix |
|---|---|---|
| **Hangs, then times out** | Firewall drops port 8000. mDNS is allowed, TCP is not — so `discover` works and the connect still fails. | On B: `sudo ufw allow 8000/tcp` |
| **Instant `connection refused`** | Server bound to `127.0.0.1`, or wrong port | Start the server with `--expose` |
| **`421 Misdirected Request`** / "Invalid Host header" | `Host` header validation | Use the name it expects, or `--allow-host <addr>` |
| **`discover` finds nothing** | mDNS is link-local multicast — VLANs, guest Wi-Fi, AP/client isolation and Docker bridges all stop it, silently | Connect with `--url` |
| **Discovered URL says `127.0.0.1`** | Server is loopback-bound, so it advertises loopback | `--expose`; a remote client dialling `127.0.0.1` reaches itself |

**Five-second check from A, no MCP client needed:**

```sh
curl -sv --max-time 5 http://192.168.0.54:8000/mcp -o /dev/null
```

No HTTP status at all → packets are being dropped → firewall or route. **Any** status, even `400`
or `421` → the packets arrived, so the network is not your problem.

Rehearse on one machine with nothing exposed: `pixi run server --port 8123 --no-mdns`, then
`examples/smoke-test.sh http://127.0.0.1:8123/mcp`. Full detail, including a Wi-Fi demo pre-flight
checklist: [docs/troubleshooting.md](docs/troubleshooting.md).

## Tools

Eleven tools, one JSON envelope per result. **Call `list_actions` first** — it says what this
machine can actually do right now, and why the rest cannot.

| Tool | What it does | Side effect |
|---|---|---|
| `list_actions` | Capability matrix, with reasons and remediation | none |
| `list_apps` | Launchable applications (XDG entries / `.app` bundles / Start Menu) | none |
| `list_windows` | Windows with app, title, workspace, focus | none |
| `media_status` | Player, status, track, artist, album, URL | none |
| `open_app` | Launch by fuzzy name or id | starts a process |
| `open_url` | Hand a URL to the default handler | opens a browser |
| `media_play_pause` · `media_next` · `media_previous` | Transport controls (`play_pause` **toggles**) | changes playback |
| `close_app` | Quit an app, graceful then forced | **can lose unsaved work** |
| `focus_window` | Raise + focus by app or title substring | **steals input focus** |

The four with no side effect are safe to call freely. The last five change what a person is doing —
`close_app` and `focus_window` carry MCP's `destructiveHint` (and `open_url` `openWorldHint`), so a
client can require confirmation without parsing prose.

Two traps the annotation does not cover:

- **`close_app` and `focus_window` match a substring, not an id.** `list_windows` reports a `pid`
  and window id that neither tool accepts, so there is no way to name one target. `close_app("foot")`
  on a machine with three terminals closes all three, and it sweeps the whole process tree.
- **`media_play_pause` echoes a stale status** — the state *at* the toggle, not after it. Re-read
  `media_status` to confirm what a write actually did.

Every result is `{"ok": true, "capability": ..., ...}` or `{"ok": false, "error": {...}}`. A failing
tool comes back as data inside that envelope rather than as an MCP protocol error, so a model sees
"why" instead of a stack trace:

```json
{"ok": false,
 "error": {"type": "dependency_missing",
           "message": "media.status unavailable: 'playerctl' is not installed",
           "capability": "media.status",
           "hint": "Install playerctl (package 'playerctl' on Arch/Fedora, 'playerctl' on Debian).",
           "details": {"driver": "linux"}}}
```

## Discovery

The server advertises `_appmcp._tcp` with TXT records (`path=/mcp`, `transport=streamable-http`,
`name`, `version`), so a client builds the full URL without being told it. Three things to know:

- **mDNS does not route.** Most routers, VLANs, VPNs, Docker bridges and guest Wi-Fi stop it with
  no error at all. When browsing finds nothing, use `--url`.
- **`zeroconf` is optional.** Missing responder, masked Avahi: the server says so and serves
  anyway. Nothing else depends on discovery.
- **The advertised address follows the bind**, so a resolved answer is always an address that
  accepts connections — but that means a loopback server is advertised as loopback, which a remote
  client will dial against itself.

Why the label is `_appmcp` and not `_mcp` (spoiler: `mcp` is taken by "Matrix Configuration
Protocol", port 4458, since 2006 — and a label mismatch fails silently, looking exactly like "the
server is down"): [docs/discovery.md](docs/discovery.md).

## Security

**This server has no authentication.** No user, no token, no TLS, no origin check. The
compensating constraints are that it binds `127.0.0.1` unless you pass `--expose`, keeps DNS-rebinding
protection on unless you pass `--allow-host`, allowlists `open_url` schemes (`file://` refused),
never hands a string to a shell (`execvp` with argv lists, deadline on every subprocess), and
annotates rather than blocks destructive tools.

For anything beyond a trusted LAN, stay on loopback and tunnel
(`ssh -L 8000:127.0.0.1:8000 user@host`) or put an authenticating reverse proxy in front. And when a
demo ends, remove the firewall rule: `sudo ufw delete allow 8000/tcp`.

Full threat statement, including the known local-attacker limitation:
[docs/security.md](docs/security.md).

## Is it actually tested?

Yes on Linux; **read, not run** on macOS and Windows. Developed and verified on one host
(CachyOS, Hyprland, Python 3.15). 489 passing + 4 deliberate `xfail` findings, in a suite that
tripwires any test which tries to start a real process. `focus_window` has never been called
against a live compositor — a real call moves the operator's focus — and the KWin, GNOME, X11,
macOS and Windows paths are covered by argv-recording tests and code review, not by execution.

The honest executed-vs-reviewed table, and what the four `xfail`s admit:
[docs/internals.md](docs/internals.md#what-is-actually-verified).

## Layout and development

```
iot_mcp/cli.py        server entrypoint        iot_mcp/app.py       the eleven tools (no logic)
iot_mcp/config.py     defaults + mDNS type     iot_mcp/discovery.py zeroconf advertise/browse
iot_mcp/drivers/      one module per OS; _proc.py is the ONLY place a subprocess is created
iot_mcp/client/       discovery + connect + tools/call
tests/  examples/  docs/  skills/
```

Two invariants before you add a tool: **capability detection is lazy and reported, never assumed**
(a missing `playerctl` surfaces in `list_actions()` as `dependency_missing`, nothing crashes at
import), and **subprocesses exist only in `_proc.py`**.

```sh
pixi run test           # 493 tests
pixi run lint
pixi run format-check
```

Full layout, design invariants, test-fence history, and the decisions behind `mcp` 2.x, the Hyprland
backend and the Linux-only lockfile: [docs/internals.md](docs/internals.md).

## Status

MVP. No authentication, no TLS, no Windows/macOS field verification, one platform's lockfile. What
is here was chosen because it is verifiable: no shell interpolation, deadlines on every subprocess,
and a capability answer that admits what it cannot do.

## Docs

| | |
|---|---|
| [docs/troubleshooting.md](docs/troubleshooting.md) | Firewall, the discovery-hangs trap, Wi-Fi demo pre-flight, offline smoke test |
| [docs/discovery.md](docs/discovery.md) | mDNS gotchas, advertised-address rules, `_appmcp` naming |
| [docs/security.md](docs/security.md) | Full threat statement and exposure checklist |
| [docs/internals.md](docs/internals.md) | Verification status, platforms, design, decisions |
| [examples/README.md](examples/README.md) | Per-client connection recipes |
| [skills/iot-mcp/SKILL.md](skills/iot-mcp/SKILL.md) | Agent-facing operating instructions |
