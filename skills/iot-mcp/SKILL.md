---
name: iot-mcp
description: "Use when driving or debugging an IoT-MCP server: automating a remote desktop (launching or closing apps, opening URLs, focusing windows, controlling media playback), connecting an MCP client to a Streamable HTTP OS-automation server, discovering such a server on a LAN, or diagnosing connection failures. Client-neutral — applies to OpenCode, Claude Code, Claude Desktop, LangGraph, or raw HTTP."
license: MIT
compatibility: "Any MCP-capable client, or none at all — works over raw Streamable HTTP with curl/python. Server tested at v0.1.0, protocol 2025-06-18."
metadata:
  author: orchestrator
  version: "1.0.0"
---

# Driving an IoT-MCP Server

An IoT-MCP server exposes **OS automation on a physical machine** as MCP tools: launch and
close applications, open URLs, raise windows, toggle media playback. The machine is a real
desktop with a real person in front of it. Treat every call as something a human will see
and hear.

Eleven tools. Four are read-only. The other seven change a person's screen.

## The rule that matters most

**You are operating someone else's computer.** Before any mutating call, confirm the machine
is yours to touch. The `capabilities` report leaks the remote home directory
(`details.directories`, e.g. `/home/<someone>/...`) — read it early, because it tells you
whether you are about to disturb the operator or a stranger. Disturbing a stranger is not a
demo.

## Workflow

1. **Discover or confirm the endpoint.** `http://<host>:8000/mcp` — the path is always
   `/mcp`, port `8000` unless the server printed otherwise.
2. **Call `list_actions` first.** It returns which capabilities this machine supports right
   now, plus a `reason`, a `tool`, and `details` for each. Everything else is guesswork
   without it. It costs nothing and has no arguments.
3. **Then read.** `list_windows` / `list_apps` / `media_status` establish the current state.
   These are side-effect free; run them freely and often.
4. **Get explicit consent for the write.** Quote the tool's own description back to the human
   — the server writes warnings into them. If the answer is "maybe", stop.
5. **Write once.** One mutating call, not a chain.
6. **Verify with a read.** Never trust a write's own response — see the gotchas below.
   `media_status` and `list_windows` are honest; the mutating tools' echo is not.

## Tools by tier

### Read-only — call these freely

| Tool | Args | Notes |
|---|---|---|
| `list_actions` | — | Capability matrix. Call before anything else. |
| `list_apps` | — | Launchable apps; use returned `id`/`name` with `open_app`/`close_app`. |
| `list_windows` | — | Windows with app, title, workspace, focus, pid. Backend may be Hyprland/X11/macOS. |
| `media_status` | — | Player state + track metadata. "Never starts or stops anything." |

### Mutating — starts something, consent first

| Tool | Args | Notes |
|---|---|---|
| `open_app` | `name*` | Fuzzy, case-insensitive. Returns immediately; does not wait for exit. |
| `open_url` | `url*` | `http`/`https`/`mailto`/`ftp` via the default handler. |
| `media_play_pause` | — | **Toggles.** Read `media_status` first for a known state. |
| `media_next` / `media_previous` | — | No-ops if `media_status` shows no player. |

### Destructive — explicit human approval, every time

| Tool | Args | Why it bites |
|---|---|---|
| `close_app` | `name*` | "Graceful request first, then force. Unsaved work in that app **can be lost**." |
| `focus_window` | `app_or_title*` | "**Steals input focus** from whatever the user is doing." |

## The fuzzy-match trap

`close_app` and `focus_window` match on a **substring, not an id**. `list_windows` reports a
`pid` and a window `id`, but neither is accepted as input — the API will not let you target
one thing precisely.

Consequences that are easy to miss:

- `close_app("foot")` on a box with three terminal windows kills **all three** — including,
  potentially, the one running the server you are talking to. Self-decapitation is available.
- `close_app` sweeps the **entire process tree**. Closing one browser tab via this API takes
  every other tab with it, unsaved forms included. There is no tab-level or window-level
  close in the inventory.
- It matches several aliases. Observed: `close_app("brave-browser")` searched both `brave` and
  `brave-browser` and terminated 16 PIDs in one call.
- Before a `close_app`, run `list_windows` and `list_apps` and say out loud which processes
  the substring would reach. If the match is ambiguous, do not fire; refine the name or stop.

## Verified gotchas

- **`media_play_pause` reports a stale status.** Its response carried `"status": "Playing"`
  when the true post-toggle state was `Paused`. The echoed field is the state at the moment of
  the call, not after it. Any caller that trusts it instead of re-reading gets the inverted
  answer. Re-read, always.
- **An unauthenticated server.** No token, no API key, no TLS. Anyone on the wire can close
  apps and open URLs. Never expose it beyond a network you control; `--expose` is a loud
  warning, not a feature. Clients must set OAuth off (see config below).
- **No shell injection, deliberately.** The server tokenises `Exec` lines and passes them to
  `execvp`, never to a shell. Do not try to smuggle shell metacharacters through a `name`
  argument; it will fail as a bad app name, not as an injection.
- **`mcp-session-id` is mandatory after `initialize`.** Every subsequent POST must echo it or
  the server cannot associate the request.
- **Tools stay dark until `notifications/initialized` arrives.** A `tools/list` before that
  notification can hang or come back empty.

## Connecting a client

Native attachment is always better than hand-rolled HTTP. **Restart the client after editing
config** — most clients do not hot-load MCP servers, and an agent cannot attach tools to its
own live session.

### OpenCode (V2)

Servers nest under `mcp.servers`, *not* directly under `mcp`.

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "iot": {
        "type": "remote",              // not "local" — that spawns a process on THIS machine
        "url": "http://192.168.0.54:8000/mcp",
        "oauth": false                 // remote defaults to OAuth; this server has no auth at all
      }
    }
  }
}
```

Prefer the CLI, which preserves unrelated config: `opencode mcp add iot --url <url>`, then
`opencode mcp list` and expect `✓ iot connected`. Name the server one clean word: tools are
named `<server>_<tool>`, and non-`[A-Za-z0-9_-]` characters get normalised. `codemode`
defaults to true, so tools arrive as `tools.iot.open_app(...)`; set `"codemode": false` if you
want them on the native tool list so the model sees the destructive flags directly. Leave
`protocol` at `"legacy"` — that is the handshake the Python SDK server speaks.

### Claude Code

Native Streamable HTTP, no bridge:

```sh
claude mcp add --transport http iot-os http://192.168.0.54:8000/mcp
claude mcp list && claude mcp get iot-os      # expect the eleven tools
```

Add `--scope project` to write a committed `.mcp.json` instead of user config.

### Claude Desktop

Edits a JSON config file, then a full restart. If the build accepts a remote server, use
`{"type":"http","url":"http://<host>:8000/mcp"}`. Older builds are stdio-only and need the
`mcp-remote` bridge:

```json
{"mcpServers": {"iot-os": {"command": "npx",
  "args": ["-y", "mcp-remote", "http://192.168.0.54:8000/mcp", "--allow-http"]}}}
```

`--allow-http` exists specifically to stop people pointing a client at plaintext `http://` by
accident. Its presence is a security decision, so say so when you recommend it.

### No client attached — raw HTTP

`scripts/iot_call.py` is a stdlib-only client. Its `--confirm` gate makes a mutating call
impossible to fire by accident:

```sh
./scripts/iot_call.py --url http://<host>:8000/mcp tools          # schemas + tiers
./scripts/iot_call.py --url http://<host>:8000/mcp capabilities   # == list_actions
./scripts/iot_call.py --url http://<host>:8000/mcp call list_windows
./scripts/iot_call.py --url http://<host>:8000/mcp call open_url '{"url":"https://example.com"}' --confirm
```

Exit `3` means it refused without `--confirm`. **Treat that as the signal to ask the human,
not as a flag to add reflexively.**

Protocol shape, if you must hand-roll it: one POST to `/mcp`; `Accept` must offer **both**
`application/json` and `text/event-stream`; replies come back SSE-framed so the JSON is on a
`data:` line; echo `mcp-session-id` on every request after `initialize`; send
`notifications/initialized` before touching tools.

**Build JSON payloads with a real serializer.** Interpolating an argument into a shell string
produces payloads that fail with `Parse error: trailing characters at line 1 column N`,
because quoting, globbing (`?` in URLs!), and `curl --data` (strips newlines) all corrupt it.
Use `--data-binary` and let `json.dumps` do the work. This failure was observed live and was
a client bug, not a server bug.

## Cannot connect — work the ladder in order

Isolate network, protocol, and client config before touching any of them.

1. `ping <host>` — host alive? No → wrong address, wrong subnet, or the machine is off.
2. Is the port open? `bash -c '</dev/tcp/<host>/8000'` or `nc -z <host> 8000`.
   **Timeout while ping succeeds = a firewall.** On the server: `ufw status`, then allow the
   port. This is the single most common cause and it produces a timeout, not a refusal, which
   reads like a dead host and sends people hunting for the wrong bug.
3. `bash examples/smoke-test.sh <url>` — drives initialize → initialized → tools/list → one
   real call with no MCP client involved. A `PASS` means the server is fine and the client
   config is the problem.
4. `iot-mcp-client discover` (in-repo: `pixi run discover`) — mDNS browse on
   `_appmcp._tcp.local.`. Prints the URL, the `by name` URL, version, and transport.

Two server-side flags explain most remaining failures:

- **Connection refused / discovered as `127.0.0.1`** → the server binds `127.0.0.1` by
  default. A remote client gets nothing. Needs `--expose`. Note that a server *without*
  `--expose` still announces itself over mDNS, but publishes `http://127.0.0.1:8000/mcp` — so
  **discovery succeeding does not mean the server is reachable**. Read the announced URL; if
  it says loopback, that is the diagnosis.
- **`421 Misdirected Request` / "Invalid Host header"** → the server validates `Host`.
  Connect by the name it expects, or start it with `--allow-host <the-address-you-use>`.

## Reporting

State which tier each call was, and prove the write landed with a follow-up read.
`terminated_pids: [16 pids], force_killed_pids: []` is a report; "closed the browser" is a
claim. If a capability is unsupported, quote the server's own `reason` string rather than
paraphrasing it.
