"""``python -m iot_mcp`` — bind the MCP server and describe it to the LAN.

Responsibilities, in order: build config from flags/env, put an unauthenticated LAN bind to an
interactive operator (:func:`consent_gate`), warn if the bind address is reachable by other
machines, resolve the authentication layer, resolve the optional browser tier against its approval
record, build the driver and server, advertise over mDNS (best effort, never fatal), then serve
Streamable HTTP until Ctrl-C.

Three refusals happen before anything binds, because each describes a server that must not run: an
operator who declines the consent gate, an unusable token (``AuthConfigError``) and the
exposed-browser rule (:meth:`iot_mcp.config.ServerConfig.browser_policy_error`). Everything else
that can go wrong at boot — a browser add-on that will not start, mDNS failing — degrades to a
printed line, in line with the project's "failures are data" posture.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import shlex
import signal
import sys
from dataclasses import replace
from typing import Any

from . import __version__
from .addons import default_root
from .app import build_server
from .auth import (
    AuthConfigError,
    bearer_auth_middleware,
    generate_token,
    read_token_file,
    write_token_file,
)
from .browser import BrowserControls, build_browser_controls
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

__all__ = ["build_parser", "config_from_args", "main", "run"]


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the server process.

    Returns:
        An :class:`argparse.ArgumentParser` with the server's full flag surface.
    """
    parser = argparse.ArgumentParser(
        prog="python -m iot_mcp",
        description="OS automation MCP server (Streamable HTTP + mDNS discovery).",
        epilog=(
            "Security: there is NO authentication unless you pass --token or --token-file. "
            "It binds to 127.0.0.1 unless you pass --expose. For remote access prefer an SSH "
            "tunnel: ssh -L 8000:127.0.0.1:8000 user@this-host"
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
        "community label; see 'Discovery naming' in docs/discovery.md.",
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
    auth = parser.add_argument_group(
        "authentication",
        "Bearer token auth. Off unless you configure a token: an unauthenticated endpoint is "
        "fine on loopback and dangerous on --expose. On a terminal, an exposed bind with no "
        "token pauses first: it offers to set a token up, and only proceeds without one if you "
        "type the confirmation phrase.",
    )
    auth.add_argument(
        "--token",
        default=None,
        metavar="SECRET",
        help="Require this bearer token. Prefer --token-file: a flag lands in `ps` and shell "
        "history. Falls back to $IOT_MCP_TOKEN.",
    )
    auth.add_argument(
        "--token-file",
        default=None,
        metavar="PATH",
        help="Read the token from this file (mode 0600 recommended). Falls back to "
        "$IOT_MCP_TOKEN_FILE.",
    )
    auth.add_argument(
        "--generate-token",
        action="store_true",
        help="Print a fresh high-entropy token and exit. With --token-file, write it there "
        "(mode 0600) instead.",
    )
    auth.add_argument(
        "--no-auth",
        action="store_true",
        help="Explicitly run with no token even if one is configured in the environment. "
        "Refuses to start when combined with --browser on an exposed bind.",
    )
    auth.add_argument(
        "--allow-query-token",
        action="store_true",
        help="Also accept ?token=... . Only for clients that cannot set headers: query strings "
        "end up in access logs and Referer headers.",
    )

    browser = parser.add_argument_group(
        "browser add-on",
        "Optional Playwright tier, bridged to the upstream @playwright/mcp server. Disabled "
        "unless --browser is passed, and it will not launch until an operator approves it with "
        "`iot-mcp-addons install browser`.",
    )
    browser.add_argument(
        "--browser",
        action="store_true",
        help="Register the browser control tools (status/start/stop/install-status/request). "
        "Registration is not approval: launching still requires an operator record.",
    )
    browser.add_argument(
        "--browser-argv",
        default=None,
        metavar="'COMMAND...'",
        help="Override the launch command. Must fingerprint-match what the operator approved, "
        "so this is for pointing at a local mirror of the same package, not for running "
        "something else.",
    )
    browser.add_argument(
        "--browser-start",
        action="store_true",
        help="Launch the approved add-on at boot instead of on the first browser_start call. "
        "Page tools only appear in tools/list once it is running.",
    )
    browser.add_argument(
        "--browser-allow-no-auth",
        action="store_true",
        help="Escape hatch for --expose --browser with no token. Off by default: it hands "
        "anyone on the network a real browser with network access on this machine.",
    )

    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Uvicorn/iot_mcp log level.",
    )
    parser.add_argument("--version", action="version", version=f"iot-mcp {__version__}")
    return parser


def _parse_argv_override(raw: str) -> tuple[str, ...]:
    """Split a ``--browser-argv`` value the way a shell would, without expansion.

    Raises:
        ValueError: Unbalanced quoting, or an empty command. Checked at argument-parsing time
            so the operator sees a message instead of a subprocess ``FileNotFoundError``
            several layers down at first launch.
    """
    try:
        parts = tuple(shlex.split(raw))
    except ValueError as exc:
        raise ValueError(f"--browser-argv is not a valid command line: {exc}") from exc
    if not parts:
        raise ValueError(
            "--browser-argv is empty; pass a command, or omit the flag to use the approved one"
        )
    return parts


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
    if getattr(args, "token", None) is not None:
        updates["token"] = args.token
    if getattr(args, "token_file", None) is not None:
        updates["token_file"] = args.token_file
    if getattr(args, "allow_query_token", False):
        updates["allow_query_token"] = True
    if getattr(args, "no_auth", False):
        # An environment variable is a default, not a contract: `--no-auth` has to be able to
        # win against IOT_MCP_TOKEN, or the flag would be a suggestion. The browser policy
        # check downstream then sees an unauthenticated server and refuses --browser unless the
        # operator passes the separate --browser-allow-no-auth.
        updates["token"] = None
        updates["token_file"] = None
        updates["no_auth"] = True
    if getattr(args, "browser", False):
        updates["browser_enabled"] = True
    if getattr(args, "browser_argv", None) is not None:
        updates["browser_argv"] = _parse_argv_override(args.browser_argv)
    if getattr(args, "browser_start", False):
        updates["browser_autostart"] = True
    if getattr(args, "browser_allow_no_auth", False):
        updates["browser_allow_no_auth"] = True
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


NO_AUTH_CONFIRMATION = "no auth"


def _confirm_no_auth(config: ServerConfig, *, ask: Any, tell: Any) -> ServerConfig | None:
    """Hard stop before serving an unauthenticated LAN endpoint. Returns the config to run.

    A pause that only needs Enter to dismiss is a pause nobody reads, so this asks for a
    literal phrase; an empty line, a wrong phrase, EOF and Ctrl-C all abort. The point is
    that running the desktop open to the network has to be something the operator typed,
    never the result of holding down Enter.
    """
    tell(
        "Running with NO authentication puts your desktop on the network — any client that "
        "can reach this port can launch and kill your applications and drive your browser."
    )
    tell("This is not the recommended setup. Options 1 or 2 take seconds.")
    answer = (
        (
            ask("Type 'no auth' (without the quotes) to start anyway, or press Enter to abort: ")
            or ""
        )
        .strip()
        .lower()
    )
    normalized = answer.replace("-", " ").replace("_", " ")
    normalized = " ".join(normalized.split())
    if normalized != NO_AUTH_CONFIRMATION:
        tell("iot-mcp: aborted. Nothing is listening; no firewall rule is needed.")
        tell(
            "  to serve securely: pixi run token -- --token-file "
            f"{default_root() / 'token'} && pixi run server --expose --token-file "
            f"{default_root() / 'token'}"
        )
        return None
    return config


def consent_gate(
    config: ServerConfig, *, stdin: Any = None, stdout: Any = None
) -> ServerConfig | None:
    """Interactive setup/consent step for an unauthenticated LAN bind.

    Fires only when the server would otherwise be open to the LAN (no token configured,
    loopback abandoned or ``--no-auth`` given) *and* the operator is on a terminal. A
    headless start — systemd, CI, a pipe — must not block on a question nobody is there to
    answer, so it passes through with the banner as its only warning.

    Returns:
        The configuration to serve with, or ``None`` when the operator declined, which the
        caller must treat as a refusal to start.
    """
    if not config.exposed or config.authenticated:
        return config
    stream_in = stdin if stdin is not None else sys.stdin
    stream_out = stdout if stdout is not None else sys.stdout
    if not (getattr(stream_in, "isatty", lambda: False)() and hasattr(stream_in, "readline")):
        return config
    tell = lambda line: print(line, file=stream_out, flush=True)  # noqa: E731
    default = str(default_root() / "token")

    def ask(prompt: str) -> str:
        try:
            print(prompt, end="", file=stream_out, flush=True)
            return (stream_in.readline() or "").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=stream_out)
            return ""

    tell("")
    tell("No bearer token is configured, so this endpoint would have NO authentication.")
    tell("Set one up now (recommended) or explicitly accept the risk:")
    tell(f"  1. Generate a fresh token into {default} (mode 0600)")
    tell("  2. Use a token file you already have (enter its path)")
    tell("  3. Run with no authentication anyway (requires typing a confirmation)")
    tell("  Enter  Abort without starting")
    choice = ask("choice [1/2/3] > ")
    if choice == "1":
        try:
            path, _ = write_token_file(default)
        except AuthConfigError as exc:
            tell(f"iot-mcp: {exc}")
            return None
        tell(f"iot-mcp: wrote a new token to {path} (mode 0600); the token itself is not printed.")
        tell("        hand that file (or $IOT_MCP_TOKEN_FILE) to clients — never the token flag.")
        return replace(config, token_file=str(path), token=None, no_auth=False)
    if choice == "2":
        path = ask("token file path > ")
        if not path:
            tell("iot-mcp: aborted — no path given.")
            return None
        try:
            read_token_file(path, stream=stream_out)
        except AuthConfigError as exc:
            # Fail here rather than mid-boot: the operator is holding the correct path in
            # their head right now, and an unreadable file at startup is a confusing one.
            tell(f"iot-mcp: {exc}")
            tell("iot-mcp: aborted — nothing is listening.")
            return None
        return replace(config, token_file=path, token=None, no_auth=False)
    if choice == "3":
        return _confirm_no_auth(config, ask=ask, tell=tell)
    tell("iot-mcp: aborted. Nothing is listening; no firewall rule is needed.")
    tell(
        "  to serve securely: pixi run token -- --token-file "
        f"{default} && pixi run server --expose --token-file {default}"
    )
    return None


async def run(config: ServerConfig, *, stateless: bool = False, log_level: str = "info") -> int:
    """Serve until interrupted. Returns a process exit code.

    Args:
        config: Bind/discovery/authentication/browser configuration.
        stateless: Pass ``stateless_http`` to the Streamable HTTP layer.
        log_level: Uvicorn log level.

    Returns:
        ``0`` on a clean Ctrl-C/``SIGTERM``, non-zero if the server could not start.
    """
    import uvicorn

    # The consent gate runs *first*, ahead of the banner, so the banner describes the server
    # that is actually about to start: an operator who picks "generate a token" should read
    # "Authentication required", not a stale "DISABLED" that the prompt two lines up already
    # made untrue. The browser policy check downstream still runs *after* the gate, so
    # "no auth" + --browser remains refused even if someone reaches option 3 here.
    config = consent_gate(config)
    if config is None:
        return 2

    config.warn_if_exposed()

    # Auth and the browser policy are checked before the driver or the socket: a server that
    # is configured into a refusal must refuse, not start degraded and serve a dangerous
    # surface while logging about it.
    try:
        auth = config.auth_settings()
    except AuthConfigError as exc:
        print(f"iot-mcp: {exc}", file=sys.stderr, flush=True)
        return 2

    problem = config.browser_policy_error()
    if problem:
        print(f"iot-mcp: refusing to start: {problem}", file=sys.stderr, flush=True)
        return 2

    try:
        driver = get_driver(config.driver)
    except DriverError as exc:
        print(f"iot-mcp: {exc}", file=sys.stderr, flush=True)
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr, flush=True)
        return 2

    controls = build_browser_controls(
        enabled=config.browser_enabled,
        argv=config.browser_argv,
    )
    server = build_server(driver, name=config.server_name, browser=controls)
    # flushed explicitly: when stdout is a pipe or a log file Python block-buffers it, and a
    # server stopped by signal would die without ever showing the operator the endpoint.
    print(f"iot-mcp {__version__} | {driver.describe()}", flush=True)
    print(f"auth: {_auth_line(config, auth)}", flush=True)
    if controls is not None:
        for note in controls.manager.startup_notes():
            print(f"add-on: {note}", flush=True)
        print(f"browser: {_browser_line(config, controls)}", flush=True)

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
    # Wrapped around the whole ASGI app, so it also covers anything else mounted there. The
    # MCP endpoint is the only thing listening, but a future health route should not be an
    # unauthenticated accident.
    app = starlette_app if auth is None else bearer_auth_middleware(starlette_app, auth)
    uvicorn_config = uvicorn.Config(
        app,
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
    # A cold `npx` start is seconds, not microseconds. Launching it as a concurrent task keeps
    # the port listening (and discovery answering) while the add-on warms up; a browser that
    # fails to start costs page tools, not the eleven OS tools beside them.
    starter: asyncio.Task[None] | None = None
    if controls is not None and config.browser_autostart:
        starter = asyncio.create_task(_autostart_browser(controls))
    try:
        await uvicorn_server.serve()
        return 0
    finally:
        if starter is not None and not starter.done():
            starter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await starter
        if controls is not None:
            # Kill the upstream process tree even on the error paths: an orphaned headless
            # browser outliving the server that started it is exactly the leak that turns
            # "restart the server" into "reboot the machine".
            with contextlib.suppress(Exception):
                await controls.stop()
        await stop_advertisement(advertisement)


def _auth_line(config: ServerConfig, auth: Any) -> str:
    """One-line summary of the authentication posture, printed at boot."""
    if auth is None:
        if config.no_auth:
            return "none (explicitly disabled with --no-auth)"
        return "NONE — no token configured; any client that can reach the port is an operator"
    return f"bearer token required (headers only unless --allow-query-token; {auth.fingerprint})"


def _browser_line(config: ServerConfig, controls: BrowserControls) -> str:
    """One-line summary of the browser tier's state, printed at boot."""
    if not controls.approved:
        return f"enabled but NOT APPROVED — {controls.refusal}"
    override = " (argv override)" if config.browser_argv else ""
    when = "autostart" if config.browser_autostart else "starts on browser_start"
    return f"approved{override}, {when}: {' '.join(controls.launch)}"


async def _autostart_browser(controls: BrowserControls) -> None:
    """Bring the approved add-on up in the background; never take the server down with it."""
    try:
        added = await controls.start()
        print(f"browser: upstream add-on running, {len(added)} page tool(s) registered", flush=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failing add-on must not kill the OS tools
        print(f"browser: add-on failed to start — {exc}", file=sys.stderr, flush=True)
        print("  hint: OS tools remain available; run browser_start to retry.", flush=True)


def _emit_token(args: argparse.Namespace) -> int:
    """``--generate-token``: mint a secret and exit without touching the network.

    Without ``--token-file`` the secret goes to stdout so it can be piped into a secret
    manager. With one it is written at mode ``0600`` and only the path is echoed — a generated
    secret printed to a terminal that is being scrolled, logged, or screen-shared is a secret
    shared with the room.
    """
    try:
        if getattr(args, "token_file", None):
            path, _ = write_token_file(args.token_file)
            print(f"iot-mcp: wrote a new token to {path} (mode 0600)")
            print("  use it with: python -m iot_mcp --token-file " + str(path))
        else:
            print(generate_token())
    except AuthConfigError as exc:
        print(f"iot-mcp: {exc}", file=sys.stderr, flush=True)
        return 2
    return 0


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
    if getattr(args, "generate_token", False):
        return _emit_token(args)
    try:
        config = config_from_args(args)
    except ValueError as exc:
        print(f"iot-mcp: {exc}", file=sys.stderr, flush=True)
        return 2
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
