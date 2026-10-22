# Discovery

How computer B finds computer B's address without anyone typing it. Companion to the short
[Discovery](../README.md#discovery) summary in the README.

The server registers its service with TXT records (`path=/mcp`, `transport=streamable-http`,
`name`, `version`) so a client can build the full URL without being told it.

## Four things that bite

- **mDNS is multicast.** It does not cross most routers, VLANs, VPN clients, Docker bridges or
  guest Wi-Fi. When browsing finds nothing, connect with `--url`.
- **`zeroconf` is optional.** If it cannot be imported, or Avahi is masked, the server says so and
  serves anyway; `--no-mdns` skips the attempt. Nothing else about the server depends on discovery.
- **Discovery succeeding does not mean the server is reachable.** mDNS is usually already allowed
  through the firewall while port 8000 is not, so browsing prints a perfect answer and the
  connection then black-holes. See
  [the trap](troubleshooting.md#the-trap-discovery-succeeds-the-connection-hangs).
- **Prefer one server per machine.** Every instance advertises under the same mDNS hostname
  (`Cachy-Y7.local` here), so a loopback-bound instance and an `--expose` one started at the same
  time publish different address records under one name, and a browser's cache merges them. The
  routable-first ordering keeps the merged answer safe — the client dials the LAN address, never
  its own loopback — but it then points *both* instances at that address, so while both are up,
  reach the loopback one with `--url`. Seen on the demo host.

## The advertised address follows the bind

An `--expose` server publishes its non-loopback IPv4 addresses; a server bound to loopback
publishes only the loopback address, so whatever a browser resolves is something that will
actually accept the connection. LAN IPv6 addresses are not published.

When an answer carries more than one address the client dials the most routable one — non-loopback
before loopback, IPv4 before IPv6, the family a LAN client is most likely to have a route for.

**This used to be broken and the fix is worth knowing.** A loopback-bound server used to advertise
its LAN address (`192.168.0.54` here), which the bind was not listening on, so the only client that
could have used the answer got a crisp `Connection refused`. The published address is now the
address that accepts: `pixi run server` plus `pixi run client`, both with **no flags on one
machine**, works.

**One hazard survives.** mDNS registration is independent of the TCP bind, so a *remote* browser
sees a loopback advertisement too, and dialling `127.0.0.1` from A connects A to itself. Separating
the two clients requires knowing which machine published the record, which this MVP does not do.
For the cross-machine demo the server must run `--expose`.

## Discovery naming

The default service type is **`_appmcp._tcp`**.

DNS-SD service types are drawn from the same RFC 6335 registry as TCP/UDP service names, and `mcp`
is already assigned in it: **port 4458/tcp+udp, "Matrix Configuration Protocol", registered
2006-12**
([the registry](https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.xhtml?search=mcp)).
So `_mcp._tcp` — the label a number of MCP projects use today — is a borrowed name, and a DNS-SD
lookup on it is ambiguous about which `mcp` answers. `_appmcp` is unassigned, and at 7 characters
its `_appmcp._tcp` form sits comfortably inside the 15-character DNS label limit. (`_mcp._tcp` fits
the length limit too; the objection is the collision, not the length.) This is also the value the
local-mcp-kit `local-discovery-v1` discovery profile uses.

**The failure mode is silent, so understand it before a demo.** A client browsing `_mcp._tcp` will
simply not see a server advertising `_appmcp._tcp`. The client gets an empty list, the server logs
nothing, and no error is raised anywhere — it looks identical to "the server is down" or "mDNS
doesn't work here". Both ends must name the same label. If your client only ever browses the
community label, start the server with `--mdns-type _mcp._tcp`; that value is still fully supported
and normalized identically, it just is not the default.

The type is a configuration value in exactly one place — `DEFAULT_MDNS_TYPE` in
`iot_mcp/config.py` — overridable by `--mdns-type` (server *and* client) or `IOT_MCP_MDNS_TYPE`,
and imported everywhere else. `tests/test_config_and_cli.py` fails if a second copy of the literal
appears anywhere in the package.
