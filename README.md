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

# Recommended for anything remote: generate a bearer token and require it.
pixi run token -- --token-file ~/.config/iot-mcp/token
pixi run server --expose --token-file ~/.config/iot-mcp/token
```

Without `--token` or `--token-file`, the server runs with **no authentication**. That is acceptable
on loopback and not recommended on `--expose`. On a terminal, `--expose` without a token
therefore **pauses before it binds**: it offers to generate a token for you (or point at one you have), and only starts
unprotected if you type `no auth`; Enter aborts. Headless starts (systemd, CI, a pipe) cannot answer
a prompt, so they run with the banner as their only gate — configure a token there yourself.
DNS-rebinding protection stays on unless `--expose` or `--allow-host` is used. The banner reports
what was detected:

```text
iot-mcp 0.1.0 | linux driver: 10/10 capabilities — apps.list, apps.open, apps.close,
url.open, media.play_pause, media.next, media.previous, media.status, windows.list, windows.focus
mDNS: advertising http://192.168.0.54:8000/mcp
listening: http://0.0.0.0:8000/mcp
auth: bearer token required
browser: disabled (default)
```

Useful flags: `--port`, `--path`, `--driver`, `--no-mdns`, `--mdns-type`, `--allow-host`,
`--stateless`, `--token-file`, `--browser`. `python -m iot_mcp --help` is authoritative.

Want the browser tier as well? Run `pixi run browser-setup` (or `browser-setup-headed` for a window
you can watch) once, then serve with `pixi run browser` — flags still apply
(`pixi run browser --expose --token-file …`). See [the browser tier](#optional-browser-tier).

**On computer A — drive:**

```sh
export IOT_MCP_TOKEN_FILE=~/.config/iot-mcp/token       # if B was started with a token

pixi run discover                                        # what is out there?
pixi run client                                          # discover, connect, prompt
pixi run client -- --call media_status
pixi run client -- --call open_app --args '{"name":"firefox"}' --json
```

Or skip discovery and name the URL (the path is always `/mcp`):

```sh
iot-mcp-client --url http://192.168.0.54:8000/mcp \
  --token-file ~/.config/iot-mcp/token --call list_actions --json
```

Token precedence is `--token`, then `--token-file`, then `$IOT_MCP_TOKEN_FILE`, then
`$IOT_MCP_TOKEN`. Prefer a file: flags appear in `ps` and shell history.

Exit codes are scripting-friendly: `0` success · `1` nothing discovered, or a tool reported
`ok: false` · `2` usage/config problem · `130` interrupted. `--json` prints the driver's envelope
verbatim, so `jq` works on it:

```sh
iot-mcp-client --url … --call media_status --json | jq -r '.playing.title // "nothing playing"'
```

A tool that answers with a picture (`browser_take_screenshot`) gets one more step: the client
writes each image block to a file and prints where it went, because a terminal cannot draw a
picture and a text-only model cannot read one. `--save-images DIR` picks the directory (default: a
temp dir), `--open-images` hands each file to the desktop viewer, and under `--json` the paths are
printed on stderr so stdout stays `jq`-friendly.

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
| **Banner prints, then the server exits (status 2)** | Consent gate: `--expose` with no token, and the prompt was never answered | Choose option 1/2 at the prompt, or set a token before starting headlessly |
| **`421 Misdirected Request`** / "Invalid Host header" | `Host` header validation | Use the name it expects, or `--allow-host <addr>` |
| **`discover` finds nothing** | mDNS is link-local multicast — VLANs, guest Wi-Fi, AP/client isolation and Docker bridges all stop it, silently | Connect with `--url` |
| **Discovered URL says `127.0.0.1`** | Server is loopback-bound, so it advertises loopback | `--expose`; a remote client dialling `127.0.0.1` reaches itself |
| **`401 Unauthorized`** | Server has a bearer token; request/client did not present it | Pass client `--token-file` / `--token`, or set `$IOT_MCP_TOKEN_FILE` / `$IOT_MCP_TOKEN` |

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

The MVP exposes **eleven desktop tools**, one JSON envelope per result. **Call `list_actions`
first** — it says what this machine can actually do right now, and why the rest cannot.

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

### Optional browser tier

Passing `--browser` registers seven tools — the lifecycle ones (`browser_status`, `browser_start`,
`browser_stop`, `browser_install_status`, `browser_request_install`) plus two narration ones
(`browser_annotate`, `browser_annotate_clear`). Registration is not approval:
nothing launches until the operator has set the browser up at a terminal on that machine. From a
clean checkout, the whole setup is:

```sh
pixi run browser-setup           # node + chromium, then the typed approval — headless
pixi run browser-setup-headed    # ... or: the browser opens a real window on this desktop
pixi run browser                 # start the server; the mode is whatever you approved
```

Both setup commands are idempotent — re-running downloads nothing new and only asks again if you
are changing mode — and both stop at a prompt you have to answer by typing `approve browser`. That
prompt is the security model, so nothing answers it for you: an agent can only *ask*
(`browser_request_install`), never install, approve, launch, download, or substitute a browser
command.

The mode is an **approval**, not a flag: the record pins the exact launch argv by fingerprint and
the server runs that record. Which is why `pixi run browser` needs no `--browser-argv` — pass it
only to deliberately override, and a mismatch with the approval is refused. The two argv strings
exist in exactly one place, `pixi.toml` (`browser-approve`, `browser-approve-headed`).

Headed is louder by construction: no `--headless`, so a window opens, can take focus, and is
visible to anyone looking at the screen or sharing it. To make the window appear, the bridge hands
the browser process this session's compositor handle — the Wayland socket on a Wayland session,
`DISPLAY` on an X11 one, never both. `browser_status` says which under `bridge.desktop`
(`wayland` / `x11` / `none`); `none` on a desktop session is a bug or a headless approval, and
[Troubleshooting](docs/troubleshooting.md) covers both.

A window is only transparent if the person watching can see *what the agent is looking at*, and a
screenshot does not travel there — it goes to the caller. So `browser_annotate(note, text=...)`
draws the agent's own words into the live page: a corner card, optionally a box around one element
with the rest dimmed. `browser_annotate_clear()` takes it away, and so does navigating away. It is
cosmetic: it grants nothing, approves nothing, and nothing it is told is trusted.

Underlying tasks, if you want them individually: `pixi run -e browser browser-install`,
`browser-list`, `browser-approve`, `browser-approve-headed`, `browser-serve`. The generic add-on
CLI is still there too — `pixi run addons pending`, `pixi run addons install browser` (packaged
default: chrome channel, headless). Server flags go after the task name:
`pixi run browser --expose --token-file ~/.config/iot-mcp/token`.

After approval, `browser_start` bridges the upstream `@playwright/mcp` server and dynamically adds
its page tools (for example `browser_navigate`) to `tools/list`; `browser_stop` removes them again.
The bridge is only a *client* of that server, and the approved argv is the only command it can run.

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
`name`, `version`, `auth`, `browser`), so a client builds the full URL and sees the server's policy
without being told it. The TXT records never contain the token, fingerprint, or add-on argv. Three
things to know:

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

Authentication is **off unless you configure it**. Generate a bearer token with
`pixi run token`, pass `--token-file`, and every HTTP request must send
`Authorization: Bearer <token>`. There is still no user system or TLS, so a token is not encryption;
on an untrusted network, tunnel (`ssh -L 8000:127.0.0.1:8000 user@host`) or terminate TLS with an
authenticating reverse proxy.

The compensating defaults are a loopback bind, bearer authentication when configured, DNS-rebinding
protection unless `--expose`/`--allow-host`, a `file://`-refusing URL allowlist, no shell
interpolation, and MCP annotations for destructive tools. A LAN-reachable server with the browser
tier enabled **refuses to start without a token** unless `--browser-allow-no-auth` is explicitly
passed. Browser approval is operator-only and argv-pinned.

When a demo ends, remove the firewall rule: `sudo ufw delete allow 8000/tcp`.

Full threat statement, token model, browser policy, and known local-attacker limitation:
[docs/security.md](docs/security.md).

## Is it actually tested?

Yes on Linux; **read, not run** on macOS and Windows. Developed and verified on one host
(CachyOS, Hyprland, Python 3.15). 600 passing + 4 deliberate `xfail` findings, in a suite that
tripwires any test which tries to start a real process. Auth and browser control/pass-through are
covered with in-process HTTP and a fake bridge; the real upstream Playwright process is not started
by default. `focus_window` has never been called against a live compositor — a real call moves the
operator's focus — and the KWin, GNOME, X11, macOS and Windows paths are covered by argv-recording
tests and code review, not by execution.

The honest executed-vs-reviewed table, and what the four `xfail`s admit:
[docs/internals.md](docs/internals.md#what-is-actually-verified).

## Layout and development

```
iot_mcp/cli.py        server entrypoint        iot_mcp/app.py       core tools + optional browser
iot_mcp/config.py     defaults + policy       iot_mcp/auth.py      bearer token + ASGI middleware
iot_mcp/addons.py     approval state          iot_mcp/addons_cli.py human-only install CLI
iot_mcp/browser.py    browser controls        iot_mcp/addon_bridge.py upstream stdio bridge
iot_mcp/discovery.py  zeroconf advertise/browse
iot_mcp/drivers/      one module per OS; _proc.py is the ONLY place a driver subprocess is created
iot_mcp/client/       discovery + connect + tools/call
tests/  examples/  docs/  skills/
```

Two invariants before you add a tool: **capability detection is lazy and reported, never assumed**
(a missing `playerctl` surfaces in `list_actions()` as `dependency_missing`, nothing crashes at
import), and **subprocesses exist only in `_proc.py`**.

```sh
pixi run test           # 600 tests + 4 xfail findings
pixi run lint
pixi run format-check
```

Full layout, design invariants, test-fence history, and the decisions behind `mcp` 2.x, the Hyprland
backend and the Linux-only lockfile: [docs/internals.md](docs/internals.md).

## Status

MVP. Optional bearer auth, no TLS, no Windows/macOS field verification, one platform's lockfile.
The optional browser tier requires a separate operator approval and its real upstream process is not
exercised by the default suite. What is here was chosen because it is verifiable: no shell
interpolation, deadlines on every subprocess, and a capability answer that admits what it cannot do.

## Docs

| | |
|---|---|
| [docs/troubleshooting.md](docs/troubleshooting.md) | Firewall, the discovery-hangs trap, Wi-Fi demo pre-flight, offline smoke test |
| [docs/discovery.md](docs/discovery.md) | mDNS gotchas, advertised-address rules, `_appmcp` naming |
| [docs/security.md](docs/security.md) | Full threat statement and exposure checklist |
| [docs/internals.md](docs/internals.md) | Verification status, platforms, design, decisions |
| [examples/README.md](examples/README.md) | Per-client connection recipes |
| [skills/iot-mcp/SKILL.md](skills/iot-mcp/SKILL.md) | Agent-facing operating instructions |
