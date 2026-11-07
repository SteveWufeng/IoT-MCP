"""Server-side configuration and the single source of truth for the mDNS service type.

Everything the CLI can override has a default here, so ``iot_mcp.cli`` stays a thin argument
parser and ``discovery`` never hardcodes a service type of its own.

mDNS service-type naming dispute
--------------------------------
The default is ``_appmcp._tcp``. The label matters because DNS-SD service types are drawn from
the same RFC 6335 registry as TCP/UDP service names, and ``mcp`` is already assigned in it —
to Matrix Configuration Protocol, port 4458/tcp+udp, registered 2006-12. So ``_mcp._tcp``, the
label a number of MCP projects use today, is a *borrowed* name: a DNS-SD lookup on it is
ambiguous about which ``mcp`` is on the other end. ``_appmcp`` is unassigned, and at 7 characters
its 13-character ``_appmcp._tcp`` form sits comfortably inside the 15-character DNS label limit
(``_mcp._tcp`` would too; length is not the objection, the collision is).

The trade-off is a silent one and deployers should understand it: a client that browses
``_mcp._tcp`` will simply not see a server advertising ``_appmcp._tcp``, and vice versa. Nothing
errors, nothing logs on the far side — discovery just returns an empty list. Both ends have to
agree on the label; that is the whole of discovery.

``_mcp._tcp`` remains a perfectly valid explicit value and is normalized identically, so an
operator whose client only browses the community label can advertise it with ``--mdns-type
_mcp._tcp``. The type is a configuration value (``--mdns-type`` / ``IOT_MCP_MDNS_TYPE``),
defined once here; anyone satisfying the stricter reading keeps both ends on ``_appmcp._tcp``,
which is what the local-mcp-kit ``local-discovery-v1`` profile uses. See "Discovery naming" in
``docs/discovery.md`` and "Network & firewall prerequisites" in ``docs/troubleshooting.md``.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .auth import AuthConfigError, TokenAuth, read_token_file

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MDNS_INSTANCE",
    "DEFAULT_MDNS_TYPE",
    "DEFAULT_MCP_PATH",
    "DEFAULT_PORT",
    "LOOPBACK_HOSTS",
    "ServerConfig",
]

#: Loopback by default: an unauthenticated OS-control endpoint must not appear on the LAN
#: unless the operator asks for it with ``--expose``.
DEFAULT_HOST = "127.0.0.1"

#: Streamable HTTP port, matching the MCP ecosystem's usual default.
DEFAULT_PORT = 8000

#: Mount point advertised in the ``path=`` TXT record.
DEFAULT_MCP_PATH = "/mcp"

#: Default mDNS service type. ``_appmcp`` rather than the community's ``_mcp`` because DNS-SD
#: service types share the RFC 6335 service-name registry and ``mcp`` is already assigned there.
#: ``_mcp._tcp`` still works when passed explicitly. Change it here (or with ``--mdns-type``)
#: and nowhere else.
DEFAULT_MDNS_TYPE = "_appmcp._tcp"

#: Default human-readable mDNS instance name.
DEFAULT_MDNS_INSTANCE = "IoT-MCP"

#: Addresses that mean "only this machine". Note what is deliberately **not** here:
#: ``0.0.0.0``/``::`` bind every interface and are therefore an exposure, not a safeguard.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})

_ENV_PREFIX = "IOT_MCP_"


def _env(name: str, default: str) -> str:
    """Read ``IOT_MCP_<name>`` with a fallback."""
    return os.environ.get(f"{_ENV_PREFIX}{name}", default)


def _env_opt(name: str) -> str | None:
    """Read ``IOT_MCP_<name>`` as an optional value (empty string means unset)."""
    value = os.environ.get(f"{_ENV_PREFIX}{name}", "")
    return value or None


def _env_flag(name: str) -> bool:
    """Read ``IOT_MCP_<name>`` as a boolean.

    Only an explicit truthy token turns a flag on — ``IOT_MCP_BROWSER=0`` and an empty value
    both mean off, because a flag that turns *itself* on by being present-but-empty would
    make "disabled by default" the wrong default for the browser tier.
    """
    return os.environ.get(f"{_ENV_PREFIX}{name}", "").strip().lower() in {"1", "true", "yes", "on"}


def _split_argv(raw: str | None) -> tuple[str, ...] | None:
    """Parse a launch command from the environment the way a shell would, minus expansion."""
    if raw is None:
        return None
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    return tuple(parts) or None


@dataclass(frozen=True)
class ServerConfig:
    """Everything needed to bind the server and describe it to the LAN.

    Attributes:
        host: Bind address. Anything outside :data:`LOOPBACK_HOSTS` is a LAN exposure.
        port: TCP port.
        path: Streamable HTTP mount path.
        mdns_type: mDNS service type, with or without the ``.local.`` suffix.
        mdns_instance: Friendly instance name shown to browsers.
        advertise: Whether to run the mDNS responder at all.
        driver: Backend key, or ``None`` to auto-detect.
        server_name: MCP server name reported in the initialize handshake.
        disable_rebinding_protection: Set by ``--expose``; see
            :attr:`allows_lan_clients`.
        allowed_hosts: Explicit ``Host`` header allowlist, or empty for the SDK default.
        token: Bearer secret, from ``--token``/``IOT_MCP_TOKEN``. Set by the CLI after a
            ``--token-file``/``--generate-token`` resolution too, so one field answers
            "is this server authenticated".
        token_file: Path holding the secret (read at startup, never at import time).
        no_auth: ``--no-auth`` was passed. Recorded so the boot banner can say the server is
            open *by explicit request* rather than by oversight.
        allow_query_token: Accept ``?token=`` as well as headers. Off: query strings leak
            into logs and ``Referer``.
        browser_enabled: The operator opted into the browser tier with ``--browser``. This
            registers the control tools only; running anything still requires an approval
            record (:mod:`iot_mcp.addons`).
        browser_argv: ``--browser-argv`` override, which must match the approved fingerprint.
        browser_allow_no_auth: Operator's explicit override for the "exposed + browser must
            have a token" rule. Off by default and loud when on.
        browser_autostart: Start the approved add-on at boot instead of on first use.
    """

    host: str = field(default_factory=lambda: _env("HOST", DEFAULT_HOST))
    port: int = field(default_factory=lambda: int(_env("PORT", str(DEFAULT_PORT))))
    path: str = field(default_factory=lambda: _env("PATH_PREFIX", DEFAULT_MCP_PATH))
    mdns_type: str = field(default_factory=lambda: _env("MDNS_TYPE", DEFAULT_MDNS_TYPE))
    mdns_instance: str = field(default_factory=lambda: _env("MDNS_INSTANCE", DEFAULT_MDNS_INSTANCE))
    advertise: bool = True
    driver: str | None = field(
        default_factory=lambda: os.environ.get(f"{_ENV_PREFIX}DRIVER") or None
    )
    server_name: str = field(default_factory=lambda: _env("NAME", "iot-mcp"))
    disable_rebinding_protection: bool = False
    allowed_hosts: tuple[str, ...] = ()
    token: str | None = field(default_factory=lambda: _env_opt("TOKEN"))
    token_file: str | None = field(default_factory=lambda: _env_opt("TOKEN_FILE"))
    no_auth: bool = field(default_factory=lambda: _env_flag("NO_AUTH"))
    allow_query_token: bool = field(default_factory=lambda: _env_flag("ALLOW_QUERY_TOKEN"))
    browser_enabled: bool = field(default_factory=lambda: _env_flag("BROWSER"))
    browser_argv: tuple[str, ...] | None = field(
        default_factory=lambda: _split_argv(_env_opt("BROWSER_ARGV"))
    )
    browser_allow_no_auth: bool = field(default_factory=lambda: _env_flag("BROWSER_ALLOW_NO_AUTH"))
    browser_autostart: bool = field(default_factory=lambda: _env_flag("BROWSER_AUTOSTART"))

    @property
    def version(self) -> str:
        """Package version, echoed into the ``version=`` TXT record."""
        return __version__

    @property
    def exposed(self) -> bool:
        """Whether this bind address is reachable from other machines on the network."""
        return self.host not in LOOPBACK_HOSTS

    @property
    def authenticated(self) -> bool:
        """Whether a bearer token is configured (and not explicitly turned off)."""
        return not self.no_auth and bool(self.token or self.token_file)

    def auth_settings(self, *, stream: Any = None) -> TokenAuth | None:
        """Resolve the authentication layer, or ``None`` when the server is open.

        Returns:
            A validated :class:`~iot_mcp.auth.TokenAuth`, or ``None`` when no token was
            configured — which is a supported configuration on loopback and a loudly-warned
            one on a LAN bind.

        Raises:
            AuthConfigError: Both sources given (ambiguous), or the secret fails
                validation. The server must refuse to start rather than serve a broken or
                trivially guessable authentication layer.
        """
        if self.token and self.token_file:
            raise AuthConfigError(
                "both a token and --token-file are configured; pick one so it is unambiguous "
                "which secret the server actually accepts"
            )
        if self.no_auth:
            # ``--no-auth`` wins over the environment rather than being ignored by it, but the
            # ambiguity check above still runs first: a config that cannot decide which secret
            # to use is a broken config, not an opt-out.
            return None
        if self.token_file:
            token = read_token_file(self.token_file, stream=stream)
        elif self.token:
            token = self.token
        else:
            return None
        return TokenAuth(token=token, allow_query_token=self.allow_query_token)

    @property
    def browser_needs_auth(self) -> bool:
        """Whether the exposed-browser rule currently bites.

        The rule: a LAN-reachable server with the browser tier enabled must authenticate,
        because the add-on hands a remote caller a real browser with network access. The only
        way out is the operator's explicit ``--browser-allow-no-auth``, which sets this to
        ``False`` — i.e. "auth is needed but you have chosen to proceed without it".
        """
        if self.browser_allow_no_auth:
            return False
        return self.browser_enabled and self.exposed and not self.authenticated

    def browser_policy_error(self) -> str | None:
        """Why this configuration must not start, or ``None`` when it may."""
        if not self.browser_needs_auth:
            return None
        return (
            f"the browser add-on is enabled on a LAN-reachable bind ({self.host}:{self.port}) "
            "with no token, so anyone who can open a TCP connection could drive a real browser "
            "on this machine. Configure --token / --token-file (recommended, plus an SSH "
            "tunnel), bind to 127.0.0.1, or pass --browser-allow-no-auth if you really mean it."
        )

    @property
    def allows_lan_clients(self) -> bool:
        """Whether DNS-rebinding protection is off (needed for LAN ``Host`` headers).

        The MCP SDK validates the ``Host`` header against a loopback-only allowlist by
        default. That is right for loopback and wrong for a machine-name URL, so ``--expose``
        turns the check off (or narrows it to ``--allowed-host`` entries) and says so loudly.
        """
        return self.disable_rebinding_protection or bool(self.allowed_hosts)

    def url_for(self, target: str) -> str:
        """Build a Streamable HTTP URL for ``target``.

        Args:
            target: Hostname, mDNS name, or address (IPv6 addresses may be bare or bracketed).

        Returns:
            Absolute URL, e.g. ``http://192.168.1.20:8000/mcp``.
        """
        host = target.strip().rstrip(".")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}{self.path}"

    @property
    def local_url(self) -> str:
        """URL for a client on the same machine."""
        host = "127.0.0.1" if self.host in {"0.0.0.0", "::", ""} else self.host
        return self.url_for(host)

    def txt_records(self) -> dict[str, str]:
        """TXT records published alongside the service.

        ``auth`` and ``browser`` are both *policy* statements, not secrets: they say whether a
        token is required and whether the browser tier is enabled, so a client can tell "this
        server needs credentials" before it starts a handshake it will be refused by. Neither
        carries the token, its fingerprint, or the add-on's launch command.
        """
        return {
            "path": self.path,
            "transport": "streamable-http",
            "name": self.mdns_instance,
            "version": self.version,
            "auth": "token" if self.authenticated else "none",
            "browser": "on" if self.browser_enabled else "off",
        }

    def warn_if_exposed(self, stream: Any = None) -> bool:
        """Print the loud LAN-exposure warning. Returns whether it printed.

        Two shapes, because the two configurations have different consequences and an
        operator skimming a startup banner must not have to infer which one they are in:

        * **no token** — anyone who can open a TCP connection is an operator (launch apps,
          close them, read media metadata, enumerate windows). This was the explicit MVP
          decision, so the consequence is spelled out in full.
        * **token configured** — access requires the secret, but the secret travels in
          cleartext over plain HTTP; the recommendation stays the SSH tunnel.

        With the browser tier enabled either way, the banner says the endpoint can also drive
        a real browser — that is the difference between "annoying" and "damaging".
        """
        if not self.exposed:
            return False
        out = stream or sys.stderr
        host_header = ", ".join(self.allowed_hosts) if self.allowed_hosts else "any"
        lines = [
            "",
            "*" * 72,
            f"*  IOT-MCP IS BOUND TO {self.host}:{self.port} — REACHABLE FROM THE LAN",
            "*",
        ]
        if self.authenticated:
            source = "--token-file" if self.token_file else "--token/IOT_MCP_TOKEN"
            lines += [
                "*  A bearer token is required for every request (from " + source + ").",
                "*  There is no TLS: the token crosses this network in cleartext, and",
                "*  anyone who can sniff it becomes an operator for as long as it is valid.",
                "*",
            ]
        else:
            lines += [
                "*  Authentication is DISABLED. Anyone who can",
                "*  open a TCP connection to this port can, through the MCP tools:",
                "*    - launch arbitrary installed applications",
                "*    - terminate running applications",
                "*    - open URLs in your browser and read your media metadata",
                "*    - enumerate and focus your windows",
                "*",
                "*  There is no user, token, or origin check behind this endpoint.",
                "*",
            ]
        if self.browser_enabled:
            lines += [
                "*  The BROWSER ADD-ON IS ENABLED: page tools can drive a real browser",
                "*  with network access and arbitrary page JavaScript on this machine.",
                "*",
            ]
        lines += [
            f"*  Host-header validation: {'DISABLED' if self.disable_rebinding_protection else host_header}",
            "*",
            "*  Do not expose this beyond a trusted network. Bind to 127.0.0.1 and",
            "*  use an SSH tunnel (ssh -L 8000:127.0.0.1:8000 host) instead, or put",
            "*  an authenticating reverse proxy in front of it.",
            "*" * 72,
            "",
            "",
        ]
        out.write("\n".join(lines))
        try:
            out.flush()
        except Exception:  # pragma: no cover - a closed stderr must not kill the server
            pass
        return True
