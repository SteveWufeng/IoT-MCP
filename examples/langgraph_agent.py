#!/usr/bin/env python3
"""langgraph_agent.py — let a model drive another machine through IoT-MCP.

The interesting part is not the agent loop, it is that the eleven OS tools become LangChain
tools *at runtime*, read from the server's own ``tools/list``. Hand-written wrappers for each
tool would drift the first time the server's schema changes; this cannot.

    export IOT_MCP_URL=http://192.168.0.54:8000/mcp
    export IOT_MCP_MODEL=openai:gpt-5-mini          # any langchain chat model spec
    python examples/langgraph_agent.py "pause my music and focus the firefox window"

Two ways to get the tools, tried in this order:

1. ``langchain[mcp]`` — the maintained home for this integration.
2. ``langchain-mcp-adapters`` — the same API under the old, now unmaintained package name,
   which is what most existing examples and blog posts import.

If neither is importable, the script lists the server's tools with the client this repo
already ships and exits cleanly. That split is deliberate: it separates "my LangChain install
is wrong" from "the server is wrong", which otherwise look identical.

Status: the fallback path is exercised by ``examples/smoke-test.sh`` and the repo's own test
suite. The LangGraph path is written against the LangChain MCP documentation as of 2026-10-22
and has not been executed here — this development machine has no model credentials.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from iot_mcp.client import connect, list_tools


async def server_tools(url: str) -> list[Any]:
    """LangChain tool objects for the IoT-MCP server at ``url``.

    Returns an empty list when no LangChain MCP integration is installed; the caller decides
    what to do about that.

    Args:
        url: Streamable HTTP endpoint of a running server, e.g. ``http://192.168.0.54:8000/mcp``.

    Returns:
        Objects suitable for passing straight into an agent constructor.
    """
    try:
        from langchain.mcp import MultiServerMCPClient  # langchain[mcp], the maintained path
    except ImportError:
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError:
            return []

    client = MultiServerMCPClient({"iot-os": {"transport": "http", "url": url}})
    # get_tools() connects, reads tools/list, and wraps each entry as a StructuredTool that
    # performs a tools/call when invoked. A fresh connection per call is the default; that is
    # fine for an agent and avoids holding a session open across model turns.
    return await client.get_tools()


def build_agent(model_spec: str, tools: list[Any]) -> Any:
    """Create an agent for ``tools`` using whichever agent factory this install has.

    Args:
        model_spec: A ``"provider:model"`` string understood by ``init_chat_model``.
        tools: LangChain tool objects.

    Returns:
        Something with an ``ainvoke({"messages": [...]})`` method.
    """
    from langchain.chat_models import init_chat_model

    model = init_chat_model(model_spec)
    try:
        from langchain.agents import create_agent  # LangChain 1.x
    except ImportError:
        from langgraph.prebuilt import create_react_agent  # LangGraph <1.x

        return create_react_agent(model, tools)
    return create_agent(model, tools)


async def print_tools_without_langchain(url: str) -> int:
    """List the server's tools with this repo's own client, so the example still says something.

    Returns:
        A process exit code.
    """
    print("No LangChain MCP integration importable — listing the server instead.\n")
    try:
        async with connect(url) as session:
            for tool in await list_tools(session):
                annotations = tool.get("annotations") or {}
                hint = " (read-only)" if annotations.get("read_only_hint") else ""
                print(f"  {tool['name']:<16} {tool['description']}{hint}")
    except Exception as exc:  # noqa: BLE001 - the whole point is to report whatever went wrong
        from iot_mcp.client.connect import describe_exception

        print(f"\ncould not talk to {url}:\n  {describe_exception(exc)}", file=sys.stderr)
        return 1
    print(f"\nInstall 'langchain[mcp]' to turn these into agent tools.\n  {url}")
    return 0


async def main() -> int:
    """Run one prompt against the server, or explain why it could not."""
    url = os.environ.get("IOT_MCP_URL", "http://127.0.0.1:8000/mcp")
    prompt = " ".join(sys.argv[1:]) or "What can you do here?"

    tools = await server_tools(url)
    if not tools:
        return await print_tools_without_langchain(url)

    model_spec = os.environ.get("IOT_MCP_MODEL", "")
    if not model_spec:
        print("set IOT_MCP_MODEL (e.g. openai:gpt-5-mini)", file=sys.stderr)
        return 2

    agent = build_agent(model_spec, tools)
    print(f"{len(tools)} tools from {url}\n> {prompt}\n")
    state = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
    for message in state["messages"]:
        content = getattr(message, "content", "")
        if content:
            print(f"[{getattr(message, 'type', 'message')}] {content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
