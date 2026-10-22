"""Client-side helpers: find a server on the LAN, connect, call tools.

Importable independently of the CLI so the LangGraph/example code in ``examples/`` uses the same
resolution logic as :mod:`iot_mcp.client.cli`:

    from iot_mcp.client import connect, resolve_url

    url = await resolve_url()                 # mDNS browse, or pass url="http://b.local:8000/mcp"
    async with connect(url) as session:
        result = await call_tool(session, "list_actions")
"""

from .connect import (
    CallResult,
    call_tool,
    connect,
    list_tools,
    pick_endpoint,
    resolve_url,
)

__all__ = ["CallResult", "call_tool", "connect", "list_tools", "pick_endpoint", "resolve_url"]
