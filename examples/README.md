# Examples

Two machines are involved throughout:

| | does what | in this repo |
|---|---|---|
| **computer B** | runs the MCP server, has its desktop automated | `pixi run server` |
| **computer A** | runs the model, calls the tools | `iot_mcp.client`, the configs below |

Everything here assumes B is reachable from A and that B was started with `--expose`
(otherwise it binds `127.0.0.1` and A gets connection-refused).

## Find the URL first

B advertises itself over mDNS; A does not need to be told the address:

```sh
pixi run discover                       # or: iot-mcp-client --browse-only
avahi-browse -rt _mcp._tcp              # same thing, from the system tool
```

which prints something like `http://192.168.0.54:8000/mcp`. Every example below uses that
URL. If mDNS does not reach your client — Docker bridge, VPN, a VLAN between the machines, a
guest Wi-Fi network that suppresses multicast — use B's static address instead.

## Smoke test

`smoke-test.sh` speaks the protocol with nothing but curl. Run it from computer A before
blaming a client:

```sh
examples/smoke-test.sh http://192.168.0.54:8000/mcp
```

```text
== target: http://192.168.0.54:8000/mcp
ok: initialize -> {"jsonrpc":"2.0","id":1,"result":{"capabilities":{...
ok: session cd8d8f125ddd4c18bbd393daa312c6df
ok: tools -> close_app focus_window list_actions list_apps list_windows media_next ...
ok: list_actions -> {"jsonrpc":"2.0","id":3,"result":{"content":[{"text":"{\n  \"ok\": true...

PASS: http://192.168.0.54:8000/mcp speaks MCP over Streamable HTTP
```

## Clients

| file | for |
|---|---|
| `opencode.jsonc` | opencode — put it in `~/.config/opencode/opencode.jsonc` or `./opencode.jsonc` |
| `claude.md` | Claude Code (native `--transport http`) and Claude Desktop (native or via `mcp-remote`) |
| `langgraph_agent.py` | a LangGraph agent whose tools are read from the server at runtime |

## From Python without a framework

The client this repo ships is usable directly, and is what `iot-mcp-client` is built on:

```python
import asyncio
from iot_mcp.client import connect, call_tool, list_tools, resolve_url


async def main() -> None:
    url = await resolve_url(mdns_type="_mcp._tcp", browse_timeout=2.0)
    async with connect(url) as session:
        print([tool["name"] for tool in await list_tools(session)])
        result = await call_tool(session, "media_status")
        print(result.ok, result.payload)


asyncio.run(main())
```

`call_tool` returns `ok=False` with an `error` payload instead of raising when the *tool*
fails, so a REPL or an agent loop survives a bad call; connection failures do raise.
