"""``python -m iot_mcp.client`` — discover an OS-automation server and drive it.

Three modes, so the same tool works for a human poking around and for a script:

* ``--browse-only``: print what mDNS sees and exit (no connection made).
* ``--call NAME [--args JSON]``: one-shot tool call, machine-readable output, non-zero exit when
  the call reports a failure.
* no ``--call``: connect, print the tool list, then an interactive prompt.

The point of this CLI is the A→B path from the README: computer A finds computer B without
anyone typing an IP address.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from ..config import DEFAULT_MDNS_TYPE
from ..discovery import DiscoveryError, browse
from .connect import call_tool, connect, list_tools, resolve_url
from .connect import describe_exception as _describe_exception

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Construct the client argument parser.

    Returns:
        An :class:`argparse.ArgumentParser` covering discovery, one-shot and REPL modes.
    """
    parser = argparse.ArgumentParser(
        prog="python -m iot_mcp.client",
        description="Discover and drive an iot-mcp OS automation server over the LAN.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default=None, help="Connect to this URL instead of browsing.")
    parser.add_argument(
        "--mdns-type",
        default=DEFAULT_MDNS_TYPE,
        help="Service type to browse. Must match what the server advertises; a mismatch is "
        "silent. Servers on the old community label need '_mcp._tcp'.",
    )
    parser.add_argument(
        "--instance", default=None, help="Prefer this instance name when several answer."
    )
    parser.add_argument(
        "--browse-timeout", type=float, default=2.0, help="Seconds to wait for mDNS replies."
    )
    parser.add_argument(
        "--browse-only", action="store_true", help="List discovered servers and exit."
    )
    parser.add_argument("--call", default=None, metavar="TOOL", help="Call one tool and exit.")
    parser.add_argument(
        "--args",
        default="{}",
        metavar="JSON",
        help='Tool arguments as JSON, e.g. \'{"name":"firefox"}\'',
    )
    parser.add_argument(
        "--call-timeout", type=float, default=30.0, help="Seconds to wait for one tool call."
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of prose.")
    parser.add_argument("--quiet", action="store_true", help="Skip the banner and tool listing.")
    parser.add_argument("--log-level", default="warning", choices=["debug", "info", "warning"])
    return parser


def _dump(payload: Any, *, pretty: bool = True) -> str:
    """Serialise a result for the terminal."""
    try:
        return json.dumps(payload, indent=2 if pretty else None, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(payload)


def _print_endpoints(endpoints: list[Any]) -> None:
    """Render browse results as a small table."""
    if not endpoints:
        print("no MCP servers advertised (browse found nothing)")
        return
    print(f"discovered {len(endpoints)} server(s):")
    for endpoint in endpoints:
        friendly = endpoint.name or endpoint.instance
        print(f"  {friendly}")
        print(f"    url      {endpoint.url}")
        if endpoint.url_by_name != endpoint.url:
            print(f"    by name  {endpoint.url_by_name}")
        print(f"    type     {endpoint.service_type}")
        print(f"    version  {endpoint.version or '?'}  transport={endpoint.transport}")


async def _repl(session: Any, tools: list[dict[str, Any]]) -> None:
    """Interactive prompt: ``<tool> [json-args]``, ``tools``, ``actions``, ``help``, ``quit``.

    Args:
        session: An initialized client session.
        tools: Tool descriptors fetched at connect time, used for name matching.
    """
    names = [str(tool["name"]) for tool in tools]
    print("\ntype a tool name (optionally with a JSON object), or: tools, actions, help, quit")
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("iot-mcp> ").strip())
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in {"quit", "exit", "q"}:
            return
        if line in {"help", "?"}:
            print("  tools            re-list tools")
            print("  actions          call list_actions()")
            print(f"  <tool> [json]    call a tool, e.g. {' '.join(names[:2])}")
            continue
        if line == "tools":
            fresh = await list_tools(session)
            print(
                _dump(
                    [
                        {"name": item["name"], "description": item["description"][:70]}
                        for item in fresh
                    ]
                )
            )
            continue
        name, _, rest = line.partition(" ")
        if name not in names:
            print(f"unknown tool {name!r}; available: {', '.join(names)}")
            continue
        try:
            arguments = json.loads(rest) if rest else {}
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as exc:
            print(f"bad arguments: {exc}")
            continue
        result = await call_tool(session, name, arguments)
        print(_dump(result.payload))


async def _amain(args: argparse.Namespace) -> int:
    """Run the selected client mode and return an exit code."""
    if args.browse_only:
        try:
            endpoints = await asyncio.to_thread(browse, args.mdns_type, args.browse_timeout)
        except DiscoveryError as exc:
            print(f"discovery failed: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(_dump([endpoint.to_dict() for endpoint in endpoints], pretty=not args.json))
        else:
            _print_endpoints(endpoints)
        return 0 if endpoints else 1

    try:
        arguments = json.loads(args.args) if args.call else {}
    except json.JSONDecodeError as exc:
        print(f"--args is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if args.call and not isinstance(arguments, dict):
        print("--args must be a JSON object", file=sys.stderr)
        return 2

    try:
        url = await resolve_url(
            url=args.url,
            mdns_type=args.mdns_type,
            browse_timeout=args.browse_timeout,
            instance=args.instance,
        )
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not args.quiet and not args.json:
        print(f"connecting to {url}")

    try:
        async with connect(url) as session:
            if args.call:
                result = await call_tool(session, args.call, arguments, timeout=args.call_timeout)
                print(_dump(result.payload, pretty=not args.json))
                return 0 if result.ok else 1
            tools = await list_tools(session)
            if not args.quiet:
                print(f"{len(tools)} tools:")
                for tool in tools:
                    summary = tool["description"].splitlines()[0] if tool["description"] else ""
                    print(f"  {tool['name']:18} {summary[:72]}")
            await _repl(session, tools)
            return 0
    except Exception as exc:  # noqa: BLE001 - a dead server is a normal client condition
        print(f"connection to {url} failed: {_describe_exception(exc)}", file=sys.stderr)
        print(
            "  hint: is the server running and bound --expose? try --url http://<host>:8000/mcp",
            file=sys.stderr,
        )
        return 3


def main(argv: list[str] | None = None) -> int:
    """Console entry point for the client.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` success, ``1`` a failed tool call or empty browse, ``2`` bad usage/config,
        ``3`` connection failure.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING))
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
