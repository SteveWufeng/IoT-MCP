# Security

Companion to the short [Security](../README.md#security) summary in the README. This is the
complete statement of what the server does and does not defend against.

## The headline

**This server has no authentication.** No user, no token, no origin check. Anyone who can reach
the port can close your applications and open URLs on your desktop. It exists behind these
deliberate constraints:

- **Binds `127.0.0.1` unless you pass `--expose`**, which prints a block of text on stderr saying
  exactly what an attacker on the network can do. Read it; it is not decorative.
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

## Leaving a machine running the tool

Two independent exposures to close:

1. **The port.** `--expose` plus an open firewall port is the desktop, to the LAN. When the demo
   ends: `sudo ufw delete allow 8000/tcp` (see
   [troubleshooting](troubleshooting.md#opening-the-port-operator-runs-these-on-b)).
2. **The transport.** There is no TLS. Credentials, prompts and tool arguments cross the wire in
   plaintext.

For anything beyond a trusted LAN: leave it on loopback and tunnel,

```sh
ssh -L 8000:127.0.0.1:8000 user@host
```

or put an authenticating reverse proxy in front.

## Note on `mcp-remote`

Clients that are stdio-only bridge to this server with `npx mcp-remote <url> --allow-http`
(see [examples/](../examples/)). `--allow-http` exists specifically to stop people pointing an MCP
client at plaintext `http://` by accident. Its presence is a security decision, not a compatibility
flag — say so when you recommend it.
