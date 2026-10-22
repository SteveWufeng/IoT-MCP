"""``python -m iot_mcp`` — bind the MCP server and describe it to the LAN.

Responsibilities, in order: build config from flags/env, warn if the bind address is
reachable by other machines, build the driver and server, advertise over mDNS (best effort,
never fatal), then serve Streamable HTTP until Ctrl-C.

There is deliberately no authentication in this MVP. The only security-relevant behaviour here
is that the default bind address is loopback and that ``--expose`` is loud about what it means.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import replace
from typing import Any

from . import __version__
from .app import build_server
from .config import (
    DEFAULT_MCP_PATH,
    DEFAULT_MDNS_INSTANCE,
    DEFAULT_MDNS_TYPE,
    ServerConfig,
)
from .discovery import Advertisement, stop_advertisement, try_advertise
from .drivers import available_drivers, get_driver
from .errors import DriverError

log = logging.getLogger("iot_mcp")

__all__ = ["build_parser", "main", "run"]


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the server process.

    Returns:
        An :class:`argparse.ArgumentParser` with the server's full flag surface.
    """
    parser = argparse.ArgumentParser(
        prog="python -m iot_mcp",
        description="OS automation MCP server (Streamable HTTP + mDNS discovery).",
        epilog=(
            "Security: this server has NO authentication. It binds to 127.0.0.1 unless you pass "
            "--expose. For remote access prefer an SSH tunnel: "
            "ssh -L 8000:127.0.0.1:8000 user@this-host"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=None, help="Bind address (default: loopback).")
    parser.add_argument("--port", type=int, default=None, help="TCP port.")
    parser.add_argument(
        "--expose",
        action="store_true",
        help="Bind 0.0.0.0 so other machines can connect. INSECURE: prints a warning and "
        "disables DNS-rebinding Host validation.",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST",
        help="Keep Host validation on and allow this Host header (repeatable), e.g. "
        "'B.local:8000'. Use instead of disabling rebinding protection.",
    )
    parser.add_argument("--path", default=None, help=f"Mount path (default: {DEFAULT_MCP_PATH}).")
    parser.add_argument(
        "--driver",
        default=None,
        metavar="NAME",
        help="Automation backend override. One of: "
        + ", ".join(available_drivers())
        + " (default: auto-detect from sys.platform).",
    )
    parser.add_argument("--name", default=None, help="MCP server name reported at initialize.")
    parser.add_argument(
        "--no-mdns", action="store_true", help="Do not advertise the service over mDNS."
    )
    parser.add_argument(
        "--mdns-type",
        default=None,
        help=f"mDNS service type (default: {DEFAULT_MDNS_TYPE}). A client browsing any other "
        "label will not see this server, and will not be told why. Pass '_mcp._tcp' for the "
        "community label; see the README's 'Discovery naming' section.",
    )
    parser.add_argument(
        "--mdns-instance",
        default=None,
        help=f"mDNS instance name (default: {DEFAULT_MDNS_INSTANCE}).",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="Run Streamable HTTP without session state (one request per call). Useful for "
        "clients that cannot keep a session id, at the cost of server-initiated requests.",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Uvicorn/iot_mcp log level.",
    )
    parser.add_argument("--version", action="version", version=f"iot-mcp {__version__}")
    return parser


def config_from_args(args: argparse.Namespace) -> ServerConfig:
    """Turn parsed arguments into a :class:`ServerConfig`.

    Flags beat environment variables beat defaults: the environment is read once by building a
    default :class:`ServerConfig`, then only the flags the caller actually passed are applied.

    ``--expose`` without ``--host`` means ``0.0.0.0``; with an explicit ``--host`` the explicit
    host wins. Naming only ``--port`` never widens the bind address — changing a port must not
    turn a loopback endpoint into a LAN one.

    Args:
        args: Result of :func:`build_parser` parsing.

    Returns:
        The resolved configuration.
    """
    config = ServerConfig()  # defaults + IOT_MCP_* environment
    updates: dict[str, Any] = {}
    if args.host is not None:
        updates["host"] = args.host
    elif args.expose:
        updates["host"] = "0.0.0.0"
    if args.port is not None:
        updates["port"] = args.port
    if args.path is not None:
        updates["path"] = args.path
    if args.mdns_type is not None:
        updates["mdns_type"] = args.mdns_type
    if args.mdns_instance is not None:
        updates["mdns_instance"] = args.mdns_instance
    if args.driver is not None:
        updates["driver"] = args.driver
    if args.name is not None:
        updates["server_name"] = args.name
    if args.no_mdns:
        updates["advertise"] = False
    if args.allow_host:
        updates["allowed_hosts"] = tuple(args.allow_host)
    config = replace(config, **updates)
    # DNS-rebinding protection follows the *resolved* bind, not the `--expose` flag. The two can
    # disagree: `--expose --host 127.0.0.1` asks for exposure and then declines it, and keying
    # off the flag alone dropped Host validation on a loopback bind — silently, because
    # warn_if_exposed() only speaks when the resolved bind really is reachable. Deriving it from
    # `.exposed` means the relaxation and the warning always describe the same server, and a
    # routable `--host` without `--expose` gets the same treatment as the flag.
    if config.exposed and not config.allowed_hosts:
        config = replace(config, disable_rebinding_protection=True)
    return config


def _transport_security(config: ServerConfig) -> Any:
    """Build the SDK's transport-security settings for this bind configuration.

    The MCP SDK validates the ``Host`` header against loopback-only defaults, which is correct
    on loopback and would reject every real LAN client (they send ``Host: B.local:8000``).
    Rather than silently choosing between "broken" and "unprotected", ``--expose`` turns the
    check off and says so in the startup warning, while ``--allow-host`` keeps it on with an
    explicit list.

    Args:
        config: Resolved server configuration.

    Returns:
        A ``TransportSecuritySettings`` instance, or ``None`` to keep SDK defaults.
    """
    if config.allowed_hosts:
        from mcp.server.transport_security import TransportSecuritySettings

        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=list(config.allowed_hosts)
        )
    if config.disable_rebinding_protection:
        from mcp.server.transport_security import TransportSecuritySettings

        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return None


async def run(config: ServerConfig, *, stateless: bool = False, log_level: str = "info") -> int:
    """Serve until interrupted. Returns a process exit code.

    Args:
        config: Bind/discovery configuration.
        stateless: Pass ``stateless_http`` to the Streamable HTTP layer.
        log_level: Uvicorn log level.

    Returns:
        ``0`` on a clean Ctrl-C/``SIGTERM``, non-zero if the server could not start.
    """
    import uvicorn

    config.warn_if_exposed()

    try:
        driver = get_driver(config.driver)
    except DriverError as exc:
        print(f"iot-mcp: {exc}", file=sys.stderr, flush=True)
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr, flush=True)
        return 2

    server = build_server(driver, name=config.server_name)
    # flushed explicitly: when stdout is a pipe or a log file Python block-buffers it, and a
    # server stopped by signal would die without ever showing the operator the endpoint.
    print(f"iot-mcp {__version__} | {driver.describe()}", flush=True)

    advertisement: Advertisement | None = None
    if config.advertise:
        advertisement, message = await asyncio.to_thread(try_advertise, config)
        print(f"mDNS: {message}", flush=True)
    else:
        print(f"mDNS: disabled by --no-mdns, connect manually at {config.local_url}", flush=True)

    starlette_app = server.streamable_http_app(
        streamable_http_path=config.path,
        stateless_http=stateless,
        transport_security=_transport_security(config),
        host=config.host if config.host not in {"0.0.0.0", "::"} else "127.0.0.1",
    )
    uvicorn_config = uvicorn.Config(
        starlette_app,
        host=config.host,
        port=config.port,
        log_level=log_level,
        access_log=False,
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError, OSError):
            loop.add_signal_handler(sig, lambda: setattr(uvicorn_server, "should_exit", True))

    print(f"listening: {config.local_url}" + ("  (stateless)" if stateless else ""), flush=True)
    try:
        await uvicorn_server.serve()
        return 0
    finally:
        await stop_advertisement(advertisement)


def main(argv: list[str] | None = None) -> int:
    """Console entry point.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )
    config = config_from_args(args)
    try:
        return asyncio.run(run(config, stateless=args.stateless, log_level=args.log_level))
    except KeyboardInterrupt:  # pragma: no cover - handled by the signal handler normally
        return 0
    except OSError as exc:
        print(
            f"iot-mcp: could not bind {config.host}:{config.port} — {exc}",
            file=sys.stderr,
            flush=True,
        )
        print(
            "  hint: another process owns the port, or the address is not local.",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
