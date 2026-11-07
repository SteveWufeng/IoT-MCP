
# Security

Companion to the short [Security](../README.md#security) summary in the README. This is the
complete statement of what the server does and does not defend against.

## The headline

Authentication is **off unless you configure a bearer token**. If no token is configured, anyone who
can reach the port can close applications, open URLs, and — only when an operator has approved and
started the browser add-on — drive a real browser on this machine.

The deliberate defaults are:

- **Binds `127.0.0.1` unless you pass `--expose`**, which prints a block of text on stderr saying
  exactly what a network attacker can do. Read it; it is not decorative.
- **Bearer-token auth when configured.** Generate one with `--generate-token`, preferably with
  `--token-file`, so every MCP request must present `Authorization: Bearer <token>`.
- **An interactive consent gate on an exposed, unauthenticated bind.** On a terminal, starting with
  `--expose` and no token stops before serving and offers to write a token for you (mode 0600, at
  `$XDG_CONFIG_HOME/iot-mcp/token` by default), to point at a token file you already have, or to
  abort. Choosing "no authentication anyway" requires *typing* `no auth`; Enter, EOF, or anything
  else exits without starting. This is deliberately a pause and a friction step, not a protection —
  a headless start (systemd, CI, stdout or stdin not a terminal) cannot be asked, so it proceeds
  with the banner as its only warning. Configure the token yourself for those.
- **The MCP SDK's DNS-rebinding protection stays on by default.** `--expose` turns it off (a LAN
  client's `Host:` header would otherwise be rejected), and `--allow-host myhost:8000` lets you
  keep the protection and name the hosts you accept instead.
- **`open_url` validates against a scheme allowlist** (`http`, `https`, `mailto`, `ftp`, `ftps`)
  before any helper runs. `file://`, URLs with embedded credentials, whitespace or control
  characters are refused.
- **No driver hands a string to a shell.** Everything is an argv list to `execvp` (or PowerShell's
  stdin with caller data in environment variables), and every subprocess has a deadline. An app
  named `; rm -rf /` is data.
- **Destructive tools are annotated rather than blocked** — a model that cannot focus a window is
  not much use. `close_app` and `focus_window` carry MCP's `destructiveHint`, `open_url` carries
  `openWorldHint`, so a client can require confirmation without parsing prose. The exception is a
  short refusal list where the damage is disproportionate to the request: the macOS backend will
  not quit `loginwindow`, `WindowServer`, `Dock` or `Finder`, because closing those is a
  logged-out user, not an automated task.
- **Not hardened against a local attacker.** A FIFO named `*.desktop` planted in a scanned XDG
  applications directory can wedge app discovery (pinned as a known limitation, fix is post-demo
  work). It needs write access to this machine's own filesystem, so it is not a LAN-facing risk.

## Bearer-token model

The server supports one static shared secret per process. This is deliberately not a user or RBAC
system: one desktop, one operator, one endpoint.

Configure it with:

```sh
pixi run token -- --token-file ~/.config/iot-mcp/token
pixi run server --expose --token-file ~/.config/iot-mcp/token
```

Accepted presentations:

- `Authorization: Bearer <token>` — preferred
- `Authorization: <token>`
- `X-Api-Key: <token>`
- `X-IoT-MCP-Token: <token>`
- `?token=<token>` only when `--allow-query-token` was explicitly passed

Clients should send the token in a header. The client flags are `--token` and `--token-file`, with
fallbacks to `$IOT_MCP_TOKEN_FILE` then `$IOT_MCP_TOKEN`. An explicit flag wins over an environment
variable.

Implementation properties:

- Tokens must be 24–256 printable ASCII characters and contain no whitespace.
- Comparison uses `hmac.compare_digest`.
- Repeated failures from one client produce `429 Too Many Requests` for a cooldown window.
- The generated token file is created at mode `0600`; token-file use is recommended because
  `--token` is visible in process arguments and shell history.
- `--no-auth` explicitly overrides a configured token and prints that authentication is disabled.
- Configuration errors are fatal at startup; ambiguous token sources are refused rather than
  guessed.

A bearer token proves knowledge of the secret. It does **not** provide TLS, user identity, replay
protection, per-user authorization, or request integrity. On plaintext HTTP the token crosses the
network in clear text and can be captured by anyone in a position to observe the traffic. Use a
trusted LAN or an SSH tunnel; for anything else, terminate TLS and forward the bearer header behind
an authenticating reverse proxy.

The TXT records advertise `auth=token` or `auth=none` as policy only. They never contain the token,
its fingerprint, the browser argv, or the add-on fingerprint.

## Browser add-on policy

The browser tier is disabled by default. Starting the server with `--browser` only registers
lifecycle controls; it does not download, launch, or expose page tools.

A human operator must approve the exact command:

```sh
pixi run addons pending
pixi run addons install browser
```

`install` prints the risk block and requires the operator to type `approve browser`. The approval
record pins the launch argv by SHA-256. `--browser-argv` may point at an equivalent local mirror,
but if its fingerprint does not match the approval the server refuses to launch it. This is an
allowlist, not a convenience override.

Agents can only call `browser_request_install`, which files a human-readable note. They cannot
approve, revoke, install a package, change the approval root, or run an unapproved command.

`browser_annotate` draws on the live page (a caption card, a ring around an element) so a person
watching the screen can follow what the agent is doing. It changes nothing about the page's own
content, grants no capability, survives nothing, and is not a channel to the user — the text it is
given is displayed and never trusted, parsed as data by the server and written to the DOM through
`textContent`. It is also not a way to spoof a user into approving something: the card is labelled
as the agent's, and no approval path reads it.

The approval root defaults to `$XDG_CONFIG_HOME/iot-mcp` or `~/.config/iot-mcp`, and can be moved
with `$IOT_MCP_ADDON_ROOT`. Anyone who can write that directory can approve an add-on, so the state
root is part of the host trust boundary.

Once approved and started, IoT-MCP bridges the official `@playwright/mcp` stdio server and republishes
its tools with their upstream schemas. Those tools can navigate to arbitrary web pages, interact
with them, and receive page content back. The upstream browser is a real network client with access
to this machine's browser profile, local files reachable through the browser, and whatever network
routes are available. Treat it as arbitrary web execution.

The exposed-endpoint rule is strict: if the server is LAN-reachable with the browser tier enabled and
no token, startup is refused. Only `--browser-allow-no-auth` overrides that policy, and it is an
escape hatch for a network you already fully trust. It should not be used to get around a token.

## Leaving a machine running the tool

Three independent exposures to close:

1. **The port.** `--expose` plus an open firewall port is the desktop, to the LAN. When the demo
   ends: `sudo ufw delete allow 8000/tcp` (see
   [troubleshooting](troubleshooting.md#opening-the-port-operator-runs-these-on-b)).
2. **The transport.** There is no TLS. Prompts, tool arguments, browser page content and the bearer
   token cross the wire in plaintext unless you tunnel or put TLS in front.
3. **The browser tier.** If approved add-ons are running, stop the server or call `browser_stop`;
   revoke them when they are no longer needed:
   `pixi run addons revoke browser`.

For anything beyond a trusted LAN: leave it on loopback and tunnel,

```sh
ssh -L 8000:127.0.0.1:8000 user@host
```

or put an authenticating reverse proxy in front and forward the bearer header securely.

## Note on `mcp-remote`

Clients that are stdio-only bridge to this server with `npx mcp-remote <url> --allow-http`
(see [examples/](../examples/)). `--allow-http` exists specifically to stop people pointing an MCP
client at plaintext `http://` by accident. Its presence is a security decision, not a compatibility
flag — say so when you recommend it.

If the server is protected by a bearer token, configure the bridge or client to send the same
`Authorization: Bearer <token>` header. Do not use query-string tokens unless the server explicitly
enables `--allow-query-token` and you understand that URLs are commonly logged.
