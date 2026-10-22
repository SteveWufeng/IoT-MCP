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

A timeout with **no HTTP status at all** is this section. Any status — even `400 Bad Request` or
`421 Misdirected Request` — means the packets arrived and the firewall is not your problem.

## Opening the port (operator runs these on B)

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
