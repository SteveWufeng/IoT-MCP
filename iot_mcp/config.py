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
import sys
from dataclasses import dataclass, field
from typing import Any

from . import __version__

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

    @property
    def version(self) -> str:
        """Package version, echoed into the ``version=`` TXT record."""
        return __version__

    @property
    def exposed(self) -> bool:
        """Whether this bind address is reachable from other machines on the network."""
        return self.host not in LOOPBACK_HOSTS

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
        """TXT records published alongside the service (``path``/``transport``/``name``/``version``)."""
        return {
            "path": self.path,
            "transport": "streamable-http",
            "name": self.mdns_instance,
            "version": self.version,
        }

    def warn_if_exposed(self, stream: Any = None) -> bool:
        """Print the loud LAN-exposure warning. Returns whether it printed.

        Zero authentication was an explicit MVP decision, so the only responsible thing left
        is to make the consequence unmistakable at startup: anyone who can reach this port can
        launch programs, close them, and read what is playing.
        """
        if not self.exposed:
            return False
        out = stream or sys.stderr
        host_header = ", ".join(self.allowed_hosts) if self.allowed_hosts else "any"
        out.write(
            "\n"
            "************************************************************************\n"
            f"*  IOT-MCP IS BOUND TO {self.host}:{self.port} — REACHABLE FROM THE LAN\n"
            "*\n"
            "*  Authentication is DISABLED (explicit MVP scope). Anyone who can\n"
            "*  open a TCP connection to this port can, through the MCP tools:\n"
            "*    - launch arbitrary installed applications\n"
            "*    - terminate running applications\n"
            "*    - open URLs in your browser and read your media metadata\n"
            "*    - enumerate and focus your windows\n"
            "*\n"
            "*  There is no user, token, or origin check behind this endpoint.\n"
            f"*  Host-header validation: {'DISABLED' if self.disable_rebinding_protection else host_header}\n"
            "*\n"
            "*  Do not expose this beyond a trusted network. Bind to 127.0.0.1 and\n"
            "*  use an SSH tunnel (ssh -L 8000:127.0.0.1:8000 host) instead, or put\n"
            "*  an authenticating reverse proxy in front of it.\n"
            "************************************************************************\n\n"
        )
        try:
            out.flush()
        except Exception:  # pragma: no cover - a closed stderr must not kill the server
            pass
        return True
