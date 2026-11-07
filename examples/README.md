# Examples

Two machines are involved throughout:

| | does what | in this repo |
|---|---|---|
| **computer B** | runs the MCP server, has its desktop automated | `pixi run server` |
| **computer A** | runs the model, calls the tools | `iot_mcp.client`, the configs below |

Everything here assumes B is reachable from A and that B was started with `--expose` (otherwise
it binds `127.0.0.1` and A cannot connect: instant *connection refused* if the port is actually
reachable, a **hang** if a firewall drops the packet first — see "Network & firewall
prerequisites" in the top-level README).

If B was started with `--token-file` or `--token`, every example also needs the shared bearer
token. Prefer the file form on machines that can read it:

```sh
export IOT_MCP_TOKEN_FILE=~/.config/iot-mcp/token
```

The repository CLI and `smoke-test.sh` check that variable before `IOT_MCP_TOKEN`. A token in
the environment is convenient but visible to processes and shell history.

## Find the URL first

B advertises itself over mDNS as `_appmcp._tcp`; A does not need to be told the address:

```sh
pixi run discover                       # or: iot-mcp-client --browse-only
avahi-browse -rt _appmcp._tcp           # same thing, from the system tool
```

which prints something like `http://192.168.0.54:8000/mcp`. Every example below uses that
URL. If mDNS does not reach your client — Docker bridge, VPN, a VLAN between the machines, a
guest Wi-Fi network that suppresses multicast — use B's static address instead.

Both ends must name the same service type. A server started with `--mdns-type _mcp._tcp` is
invisible to a client browsing the default, and neither side reports anything wrong; an empty
browse result is the only symptom.

## Smoke test

`smoke-test.sh` speaks the protocol with nothing but curl. Run it from computer A before
blaming a client:

```sh
IOT_MCP_TOKEN_FILE=~/.config/iot-mcp/token \
  examples/smoke-test.sh http://192.168.0.54:8000/mcp
```

For an unauthenticated server, unset both `IOT_MCP_TOKEN_FILE` and `IOT_MCP_TOKEN`.

```text
== target: http://192.168.0.54:8000/mcp
== auth: bearer token from IOT_MCP_TOKEN_FILE (not printed)
ok: initialize -> {"jsonrpc":"2.0","id":1,"result":{"capabilities":{...
ok: session cd8d8f125ddd4c18bbd393daa312c6df
ok: tools -> close_app focus_window list_actions list_apps list_windows media_next ...
ok: list_actions -> {"jsonrpc":"2.0","id":3,"result":{"content":[{"text":"{\n  \"ok\": true...

PASS: http://192.168.0.54:8000/mcp speaks MCP over Streamable HTTP
```

## Clients

| file | for |
|---|---|
| `opencode.jsonc` | opencode — remote transport plus bearer header; put it in `~/.config/opencode/opencode.jsonc` or `./opencode.jsonc` |
| `claude.md` | Claude Code (native `--transport http`) and Claude Desktop (native or via `mcp-remote`), including bearer-token configuration |
| `langgraph_agent.py` | a LangGraph agent whose tools are read from the server at runtime; uses `IOT_MCP_TOKEN_FILE` or `IOT_MCP_TOKEN` |

## From Python without a framework

The client this repo ships is usable directly, and is what `iot-mcp-client` is built on:

```python
import asyncio
import os

from iot_mcp.auth import read_token_file
from iot_mcp.client import connect, call_tool, list_tools, resolve_url


def bearer_token() -> str | None:
    token_file = os.environ.get("IOT_MCP_TOKEN_FILE", "").strip()
    if token_file:
        return read_token_file(token_file).strip() or None
    return os.environ.get("IOT_MCP_TOKEN", "").strip() or None


async def main() -> None:
    url = await resolve_url(browse_timeout=2.0)  # browses DEFAULT_MDNS_TYPE (_appmcp._tcp)
    async with connect(url, token=bearer_token()) as session:
        print([tool["name"] for tool in await list_tools(session)])
        result = await call_tool(session, "media_status")
        print(result.ok, result.payload)


asyncio.run(main())
```

`call_tool` returns `ok=False` with an `error` payload instead of raising when the *tool*
fails, so a REPL or an agent loop survives a bad call; connection failures do raise. A 401
arrives as a connection failure and means the server received the request but rejected the
credential.

A result can also carry `image` content blocks — `browser_take_screenshot` does. Pass
`call_tool(session, "browser_take_screenshot", image_dir="shots")` and they are written there,
with the file paths on `result.images`; without it the blocks are counted in `result.raw` and
dropped, which is the honest version of "a terminal cannot show you this".

## Optional browser tier

These examples work with the eleven core OS tools. If an operator has approved and installed
the `browser` add-on, started the server with `--browser`, and started the bridge,
`tools/list` additionally contains `browser_status`, `browser_start`, `browser_stop`,
`browser_install_status`, `browser_request_install`, `browser_annotate` and
`browser_annotate_clear`. Upstream page tools such as
`browser_navigate` appear only while that bridge is running. Installing it is a human action:

```sh
pixi run addons pending
pixi run addons install browser
```
