# Networking, firewall, and reachability

Long-form companion to the [troubleshooting table](../README.md#wont-connect-90-of-problems)
in the README. Read this when the short table did not resolve it.

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

## The trap: discovery succeeds, the connection hangs

Those three rows combine into the one failure that will cost you an afternoon.

**mDNS is already allowed; port 8000 is not.** So computer A on Wi-Fi resolves
`Cachy-Y7.local:8000` perfectly — `pixi run discover` prints the URL, the instance name, the TXT
records, all of it — and then the TCP connect to port 8000 is silently dropped and sits there
until it times out. Nothing on B is wrong. The MCP code on both sides is fine. Discovery
answered and then the connection black-holed, and the error you eventually get says "timeout",
which points at every part of this stack except the one that is actually responsible.

Tell the two apart by **how long the failure takes**:

- **hangs, then times out** → packets are being dropped. Firewall or a route. This file.
- **instant `connection refused`** → something answered and disliked it. Wrong port, server
  bound to `127.0.0.1` only, or the `Host` check rejecting you (that one answers `421`, not a
  refusal).

To check from A in five seconds without any MCP client:

```sh
curl -sv --max-time 5 http://192.168.0.54:8000/mcp -o /dev/null
```

A timeout with **no HTTP status at all** is this section. Any status — even `400 Bad Request`,
`401 Unauthorized`, `421 Misdirected Request`, or `429 Too Many Requests` — means the packets
arrived and the firewall is not your problem.

## The server prints the LAN banner and then exits instead of serving

If you started `--expose` from a terminal with no token configured, the server stops before it
binds anything and asks how to proceed. Pressing Enter (or answering anything it does not recognise, or
running out of input) is an explicit abort, and the process exits with status `2` without binding.
That is the consent gate doing its job: pick option 1 to have it write a token, option 2 to point at
a token file you already have, or type `no auth` if you truly mean an open LAN endpoint.

A server under systemd or a CI job is not a terminal, so it never sees this prompt — it starts with
the banner alone. If a service unit is restart-looping, check that `IOT_MCP_TOKEN_FILE` (or
`--token-file`) is set in the unit rather than assuming the prompt swallowed it.

## The server is reachable but says `401` or `429`

`401` means authentication is configured and the request did not present the configured bearer token;
`429` means too many failed attempts came from that source and the endpoint is cooling down. Neither
is a discovery or routing failure.

From the CLI, prefer a token file:

```sh
export IOT_MCP_TOKEN_FILE=~/.config/iot-mcp/token
iot-mcp-client --url http://192.168.0.54:8000/mcp --call media_status
```

With curl, send the header explicitly:

```sh
curl -fsS --max-time 5 \
  -H "Authorization: Bearer $(cat "$IOT_MCP_TOKEN_FILE")" \
  http://192.168.0.54:8000/mcp
```

Check the server banner: it should say `auth: bearer token required`. The client does not need to be
started with a token in advance — it may discover that `auth=token` from TXT records and then warn if
no client token is configured.

Common causes:

- Token in `$IOT_MCP_TOKEN` on B but not A. Server and client use separate environment variables and
  separate processes.
- Client started before exporting `IOT_MCP_TOKEN_FILE`; restart the client.
- Client is pointing at the wrong token file. Check `ls -l ~/.config/iot-mcp/token` and restart the
  client in the foreground so the resolved path and any read error are visible.
- Client sends the token via a proxy that strips `Authorization`; use a direct connection or tunnel.
- Repeated wrong-token attempts produced `429`; wait for `Retry-After`, fix the token, then retry.
- Query-string token was rejected. Use a header, or deliberately start the server with
  `--allow-query-token` only when no header can be set.

## The browser tier refuses to start or missing page tools

`--browser` only registers lifecycle controls. If `tools/list` lacks `browser_navigate`, that is not
necessarily a failure — call `browser_status` and read `approved`, `running`, `state`, `refusal`, and
`tools`.

A server bind that is LAN-reachable with browser enabled and no token is refused at startup. Use a
token, bind to loopback, or explicitly pass `--browser-allow-no-auth` only for a network you already
trust. The escape hatch should not be used to bypass a missing token.

If status says not approved, the operator runs the one-time setup at a terminal on that machine (it
fetches node and chromium, then asks for the typed approval):

```sh
pixi run browser-setup           # or: pixi run browser-setup-headed
pixi run browser
```

Then `browser_start` runs the approved argv — the server needs no `--browser-argv` at all, because
the approved record *is* the launch. Passing a different one is refused: the fingerprint is the
approval, so approve the launch you want rather than repointing a running approval.

### It started, but every page tool says `Chromium distribution 'chrome' is not found`

The bridge is up but has no browser to open. Upstream `@playwright/mcp` defaults to the *chrome
channel* — Google Chrome at `/opt/google/chrome/chrome` — which is not installed on a machine that
only has Brave, and which `playwright install chrome` can only provision on Debian/Ubuntu. Use the
chromium build instead:

```sh
pixi run -e browser browser-install   # the revision the add-on's own playwright expects
pixi run -e browser browser-list      # confirm it landed in ~/.cache/ms-playwright
```

The chrome channel is what the *packaged* default launch names; the setup tasks approve chromium
instead, so re-running setup fixes this case too:

```sh
pixi run browser-setup           # headless chromium
pixi run browser-setup-headed    # visible window
pixi run browser
```

Note where the truth lives: `browser_status` reports `running: true, state: "ready"` for this
failure, because readiness means the bridge process answered `tools/list` — not that a page can
exist. The failing page tool is the honest signal.

### Use Brave instead of the downloaded Chromium

There is no MCP package that needs to be installed as `playwright-brave` for this. Brave is
Chromium-based; the official `@playwright/mcp` server can drive an installed Brave binary with
`--browser chromium --executable-path /usr/bin/brave --isolated`.

Install Brave locally first. The provided setup checks for `/usr/bin/brave`, then asks
for the usual typed approval:

```sh
pixi run browser-brave-setup           # headless Brave
pixi run browser-brave-setup-headed    # visible Brave window
pixi run browser
```

If Brave lives at another path, approve the exact command manually instead of using the task;
the path is part of the fingerprint. One approval record is active at a time, so this replaces a
previous Chromium approval. The launch is fully isolated: `--isolated` keeps the browser's
profile in memory and nothing touches your real Brave profile. Without it, a second Brave
process hands its window to your *running* Brave instance — same default profile — so the agent
would land on your real, logged-in tabs. (Verified live by driving the MCP server directly.)

### Headed approval, but nothing ever appears on screen

Everything reports success — the page loads, screenshots come back, `browser_evaluate` answers —
and there is no window. Three independent layers can each force headless, so check them in order:

1. **The launch argv really is headed.** `--headless` in `browser_status`'s `launch` list means the
   approval still says headless; the fingerprint forces a new approval to change it. Fix: re-run
   `pixi run browser-setup-headed`, then restart the server.
2. **The child got a compositor handle.** The MCP SDK hands stdio servers a scrubbed environment
   (`PATH`, `HOME`, …) and drops `WAYLAND_DISPLAY`/`DISPLAY` with it. A headed launch re-attaches
   the handle it needs — see `bridge.desktop` (`wayland` / `x11` / `none`) in `browser_status`;
   `none` on a desktop session means there is no compositor to reach.
3. **Upstream's own heuristic.** `@playwright/mcp` sets `headless = true` on Linux whenever
   `DISPLAY` is unset, which catches Wayland sessions that deliberately never get an X11 handle.
   The bridge pins `PLAYWRIGHT_MCP_HEADLESS=false` for headed launches; if you launch the add-on
   some other way, set it yourself.

Layers 2 and 3 are handled by the bridge in this repo, so a browser started by an older server
process still behaves like the old one — restart it (`pixi run browser`) before debugging further.

Verify with the process rather than the compositor's idea of it:

```sh
pid=$(pgrep -f 'ms-playwright/chromium.*/chrome --' | head -1)
tr '\0' '\n' < /proc/$pid/cmdline | grep -c '^--headless$'    # 0 = really headed
tr '\0' '\n' < /proc/$pid/environ | grep -E '^(DISPLAY|WAYLAND_DISPLAY)='
hyprctl clients -j | jq -r '.[].class' | sort -u              # the window the WM actually maps
```

On Hyprland the WM, not the browser, decides visibility: a client that is spawned but never mapped
shows in `ps` and nowhere in `hyprctl clients`.

## Opening the port (operator runs these on B)

```sh
sudo ufw allow 8000/tcp comment 'MCP demo (temporary)'
sudo ufw status verbose | grep 8000
```

And when the demo is over — leaving an unauthenticated server open is leaving the desktop open to
the LAN:

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

## Pre-flight checklist for a Wi-Fi demo

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
- **Loopback needs no firewall change at all.** UFW permits loopback, so `127.0.0.1` works today,
  on this machine, with nothing opened. Use that for the rehearsal below and open `8000/tcp` only
  for the actual cross-machine moment.
- **A loopback-bound server advertises loopback**, which a *remote* browser will dutifully dial
  against itself. For the cross-machine demo the server must run `--expose` — that is not a
  preference. See
  [the advertised address follows the bind](discovery.md#the-advertised-address-follows-the-bind).

## Smoke test without any network

The zero-risk rehearsal, before any firewall change and with nothing exposed:

```sh
# terminal 1 on B — loopback, no mDNS, nothing reachable from the LAN
pixi run server --port 8123 --no-mdns

# terminal 2 on B
examples/smoke-test.sh http://127.0.0.1:8123/mcp
```

That exercises the whole server — transport, session handling, tool listing, one real tool call —
with no firewall, no multicast, no routing and no exposure. `--no-mdns` is there to keep even the
multicast packets out of the rehearsal, not because discovery would lie to you: on a loopback bind
it now advertises loopback, and `pixi run discover` on the same machine resolves it.

If the smoke test passes, the server is not what is wrong when the Wi-Fi demo fails; work down the
checklist above instead.

## Isolating the layer

`examples/smoke-test.sh <url>` speaks MCP with nothing but curl. It is the boundary between "the
server is wrong" and "my client is misconfigured":

```sh
bash examples/smoke-test.sh http://192.168.0.54:8000/mcp
```

`PASS` means transport, session handling, tool listing and a real tool call all work, so the
remaining problem is in the client's configuration.
