"""mDNS advertisement (server side) and browsing (client side).

The wire contract this module maintains:

* service type — configurable, default ``_appmcp._tcp`` from
  :data:`iot_mcp.config.DEFAULT_MDNS_TYPE` (see that module for the naming dispute; this file
  never hardcodes a type)
* instance name — configurable, default ``IoT-MCP``
* port — the server's TCP port
* TXT — ``path=/mcp``, ``transport=streamable-http``, ``name=<instance>``, ``version=<pkg>``

Everything here degrades instead of failing. ``zeroconf`` may not be installed, the machine may
have no network, or Avahi/systemd-resolved may already own port 5353 — in every one of those
cases the server must still boot and print where to connect manually, because a laptop with a
dead mDNS responder is not a reason to lose OS automation.

Resolution note that matters in practice: an mDNS ``SRV`` target is a hostname (usually
``somehost.local.``) but the ``A`` records in the same response carry the real address. Clients
that build a URL from the target alone depend on the caller also having working ``.local``
resolution, which is exactly what is missing on machines without Avahi. So
:func:`endpoint_from_info` **prefers resolved addresses** and falls back to the target name.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import DEFAULT_MCP_PATH, DEFAULT_MDNS_TYPE, ServerConfig

log = logging.getLogger("iot_mcp.discovery")

try:  # pragma: no cover - the import either works or it does not
    from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

    ZEROCONF_IMPORT_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001 - any failure here must be non-fatal at import time
    ServiceBrowser = None  # type: ignore[assignment]
    ServiceInfo = None  # type: ignore[assignment]
    ServiceListener = object  # type: ignore[misc,assignment]
    Zeroconf = None  # type: ignore[assignment]
    ZEROCONF_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

__all__ = [
    "DiscoveryError",
    "DiscoveredEndpoint",
    "advertise",
    "browse",
    "endpoint_from_info",
    "local_ipv4_addresses",
    "normalize_service_type",
    "try_advertise",
]

#: DNS label limit that matters for service types and instance names.
_MAX_LABEL = 63

#: A service type is `_name._tcp.` / `_name._udp.` with a leading underscore per RFC 2782.
_TYPE_RE = re.compile(r"^_[a-z0-9][a-z0-9-]{0,62}\._(tcp|udp)\.$", re.IGNORECASE)


class DiscoveryError(RuntimeError):
    """mDNS could not be used, with a reason an operator can act on."""


def normalize_service_type(service_type: str) -> str:
    """Canonicalise a user-supplied service type into fully-qualified DNS form.

    ``_appmcp._tcp``, ``_appmcp._tcp.local`` and ``_appmcp._tcp.local.`` are all accepted and
    mean the same thing; callers should not have to know that mDNS lives in ``.local``. Any
    other label (``_mcp._tcp`` included) is normalised the same way.

    Args:
        service_type: Service type, with or without the ``.local.`` suffix.

    Returns:
        The fully-qualified type, e.g. ``_appmcp._tcp.local.``

    Raises:
        DiscoveryError: The value is not a valid ``_service._proto.`` type.
    """
    text = (service_type or "").strip().lower().rstrip(".")
    if not text:
        raise DiscoveryError("service type must not be empty")
    if text.endswith(".local"):
        text = text[: -len(".local")].rstrip(".")
    if not text.startswith("_"):
        text = f"_{text}"
    parts = text.split(".")
    # Fill in the two pieces callers most often omit — the protocol, then the mDNS domain.
    # zeroconf rejects any type that does not end in ".local.", so this is not cosmetic.
    if len(parts) == 1 or parts[-1] not in ("_tcp", "_udp"):
        parts.append("_tcp")
    core = ".".join(parts) + "."
    if not _TYPE_RE.match(core):
        raise DiscoveryError(
            f"{service_type!r} is not a valid mDNS service type; expected _name._tcp "
            "(letters, digits and hyphens, 63 characters max)"
        )
    return f"{core}local."


def local_ipv4_addresses() -> list[str]:
    """Non-loopback IPv4 addresses worth advertising.

    Uses the connect-a-DATAGRAM-socket trick to learn the default-route address (no packets
    are transmitted), then adds every address the hostname resolves to, so a box with two
    interfaces advertises both.

    Returns:
        Dotted-quad strings, default route first. Empty on a machine with no usable IPv4.
    """
    found: list[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate not in found and not candidate.startswith(("127.", "169.254.")):
            found.append(candidate)

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        probe.connect(("8.8.8.8", 53))
        add(probe.getsockname()[0])
    except OSError:
        pass  # no default route: fall back to hostname resolution below
    finally:
        probe.close()

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            add(str(info[4][0]))
    except OSError:
        pass
    return found


def service_hostname() -> str:
    """The FQDN to publish as this service's ``SRV`` target.

    The target must be a DNS name, not an address, and mDNS names belong under ``.local``.

    Returns:
        A sanitised ``host.local.`` name.
    """
    raw = socket.gethostname() or "localhost"
    cleaned = re.sub(r"[^A-Za-z0-9-]", "-", raw).strip("-") or "localhost"
    if cleaned.lower().endswith(".local"):
        return f"{cleaned}."
    return f"{cleaned}.local."


@dataclass(frozen=True)
class DiscoveredEndpoint:
    """One service found by :func:`browse`.

    Attributes:
        instance: Instance name as advertised (e.g. ``IoT-MCP``).
        service_type: Fully-qualified type that was browsed.
        target: ``SRV`` target hostname, trailing dot stripped (often ``host.local``).
        addresses: ``A``/``AAAA`` records resolved for the service, in advertisement order.
        port: TCP port.
        path: Streamable HTTP path from TXT (default ``/mcp``).
        transport: ``transport`` TXT value.
        name: ``name`` TXT value (friendly name).
        version: ``version`` TXT value (server package version).
        properties: Every decoded TXT pair, for forward compatibility.
    """

    instance: str
    service_type: str
    target: str
    addresses: tuple[str, ...] = ()
    port: int = 8000
    path: str = DEFAULT_MCP_PATH
    transport: str = "streamable-http"
    name: str = ""
    version: str = ""
    properties: Mapping[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        """Best connect target: a resolved address if we have one, else the target name."""
        if self.addresses:
            return self.addresses[0]
        return self.target

    @property
    def url(self) -> str:
        """Ready-to-connect Streamable HTTP URL."""
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        path = self.path if self.path.startswith("/") else f"/{self.path}"
        return f"http://{host}:{self.port}{path}"

    @property
    def url_by_name(self) -> str:
        """The same endpoint expressed through the ``.local`` hostname, when there is one."""
        if not self.target:
            return self.url
        return f"http://{self.target}:{self.port}{self.path}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable description, both URL spellings included."""
        return {
            "instance": self.instance,
            "service_type": self.service_type,
            "url": self.url,
            "url_by_name": self.url_by_name,
            "host": self.host,
            "addresses": list(self.addresses),
            "target": self.target,
            "port": self.port,
            "path": self.path,
            "transport": self.transport,
            "name": self.name,
            "version": self.version,
            "txt": dict(self.properties),
        }


def _decode_txt(raw: Any) -> dict[str, str]:
    """Normalise zeroconf's property container (bytes or str keys/values) to ``str`` pairs."""
    out: dict[str, str] = {}
    if not raw:
        return out
    items = raw.items() if hasattr(raw, "items") else raw
    for key, value in items:
        name = key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)
        if isinstance(value, bytes):
            text = value.decode("utf-8", "replace")
        elif value is None:
            text = ""
        else:
            text = str(value)
        out[name.lstrip("-")] = text
    return out


def _packed_addresses(info: Any) -> tuple[str, ...]:
    """Readable IPs from a ``ServiceInfo``, IPv4 first, unparseable entries dropped."""
    readable: list[str] = []
    for packed in getattr(info, "addresses", []) or []:
        text: str | None = None
        try:
            if len(packed) == 4:
                text = socket.inet_ntop(socket.AF_INET, packed)
            elif len(packed) == 16:
                text = socket.inet_ntop(socket.AF_INET6, packed)
        except (OSError, ValueError):
            text = None
        if text and text not in readable:
            readable.append(text)
    if not readable:  # some versions expose the convenience property instead
        for text in getattr(info, "parsed_addresses", []) or []:
            if text and text not in readable:
                readable.append(str(text))
    # IPv4 sorts first on purpose: DiscoveredEndpoint.host takes [0], and on a dual-stack LAN
    # a link-local-looking v6 is far more likely to be unroutable than the advertised v4.
    readable.sort(key=lambda text: (":" in text, text))
    return tuple(readable)


def endpoint_from_info(service_type: str, info: Any) -> DiscoveredEndpoint:
    """Turn a resolved ``ServiceInfo`` into a :class:`DiscoveredEndpoint`.

    Args:
        service_type: The type that was browsed.
        info: A populated zeroconf ``ServiceInfo``.

    Returns:
        An endpoint whose :attr:`DiscoveredEndpoint.url` prefers resolved addresses over the
        ``SRV`` target name (see the module docstring for why).
    """
    txt = _decode_txt(getattr(info, "properties", None))
    key = str(getattr(info, "key", "") or "")
    instance = key.split(f".{service_type}", 1)[0] if key else str(getattr(info, "name", ""))
    target = str(getattr(info, "server", "") or "").rstrip(".")
    port = int(getattr(info, "port", 0) or 0)
    path = txt.get("path") or DEFAULT_MCP_PATH
    return DiscoveredEndpoint(
        instance=instance or str(getattr(info, "name", "")),
        service_type=service_type,
        target=target,
        addresses=_packed_addresses(info),
        port=port or 8000,
        path=path if path.startswith("/") else f"/{path}",
        transport=txt.get("transport", "streamable-http"),
        name=txt.get("name", ""),
        version=txt.get("version", ""),
        properties=txt,
    )


class _Collector(ServiceListener):
    """ServiceListener that remembers instance names as they appear."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.errors: list[str] = []

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        if name not in self.names:
            self.names.append(name)

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        if name not in self.names:
            self.names.append(name)

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        if name in self.names:
            self.names.remove(name)

    def error(self, error_text: str) -> None:
        self.errors.append(error_text)


def browse(service_type: str = DEFAULT_MDNS_TYPE, timeout: float = 2.0) -> list[DiscoveredEndpoint]:
    """Browse the LAN for advertised MCP servers.

    Blocks for ``timeout`` seconds while mDNS replies arrive; call it from a thread if you are
    already inside an event loop.

    Args:
        service_type: Type to browse; normalised like :func:`normalize_service_type`.
        timeout: Seconds to wait for replies. 1-3 is usually plenty on a LAN.

    Returns:
        Endpoints that answered, in discovery order. Empty means "nothing found", not "broken".

    Raises:
        DiscoveryError: ``zeroconf`` is unavailable or a responder could not be started (no
            network, port 5353 busy). The message says which.
    """
    if ZEROCONF_IMPORT_ERROR is not None:
        raise DiscoveryError(
            f"the 'zeroconf' package is not importable ({ZEROCONF_IMPORT_ERROR}); "
            "install it, or pass --url to connect directly"
        )
    qualified = normalize_service_type(service_type)
    collector = _Collector()
    zc: Any = None
    browser: Any = None
    try:
        zc = Zeroconf()
        # Passing the listener instance is the API that has stayed stable across zeroconf
        # versions (raw handler callables have changed signature over time).
        browser = ServiceBrowser(zc, qualified, collector)
        # Poll instead of sleeping once: zeroconf delivers records on its own thread, so the
        # extra wake-ups let a healthy LAN return well before `timeout` expires.
        started = time.monotonic()
        while (time.monotonic() - started) < timeout:
            time.sleep(0.1)
        endpoints: list[DiscoveredEndpoint] = []
        for name in list(collector.names):
            info = zc.get_service_info(qualified, name, timeout=1500)
            if info is None:
                log.debug("service %s disappeared before it could be resolved", name)
                continue
            endpoints.append(endpoint_from_info(qualified, info))
        if collector.errors:
            log.debug("mDNS browser reported: %s", "; ".join(collector.errors))
        return endpoints
    except DiscoveryError:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken responder must not look like a crash
        raise DiscoveryError(
            f"mDNS browse failed ({type(exc).__name__}: {exc}). Is a network interface up, and "
            "is Avahi/systemd-resolved healthy? You can always pass --url."
        ) from exc
    finally:
        if browser is not None:
            try:
                browser.cancel()
            except Exception:  # noqa: BLE001
                pass
        if zc is not None:
            try:
                zc.close()
            except Exception:  # noqa: BLE001
                pass


@dataclass
class Advertisement:
    """A live mDNS registration; call :meth:`stop` (or use as a context manager).

    Attributes:
        service_type: Fully-qualified type registered.
        instance: Final instance name (may have been de-duplicated by zeroconf).
        port: Advertised port.
        addresses: Addresses published in the ``A`` records.
        urls: Human-facing URLs, one per advertised address.
    """

    service_type: str
    instance: str
    port: int
    addresses: tuple[str, ...]
    urls: tuple[str, ...]
    _zeroconf: Any = field(default=None, repr=False)
    _info: Any = field(default=None, repr=False)

    def stop(self) -> None:
        """Unregister the service and shut the responder down. Idempotent."""
        if self._zeroconf is None:
            return
        try:
            if self._info is not None:
                self._zeroconf.unregister_service(self._info)
        finally:
            try:
                self._zeroconf.close()
            finally:
                self._zeroconf = None
                self._info = None

    def __enter__(self) -> Advertisement:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()


def advertise(config: ServerConfig) -> Advertisement:
    """Register this server on the LAN via mDNS.

    Args:
        config: Server configuration; provides type, instance name, port and TXT records.

    Returns:
        A live :class:`Advertisement` the caller must :meth:`~Advertisement.stop`.

    Raises:
        DiscoveryError: zeroconf is missing, there is no address to advertise, or the responder
            could not start / the name could not be registered. Callers that just want to keep
            serving should use :func:`try_advertise` instead.
    """
    if ZEROCONF_IMPORT_ERROR is not None:
        raise DiscoveryError(
            f"cannot advertise: 'zeroconf' is not importable ({ZEROCONF_IMPORT_ERROR})"
        )
    qualified = normalize_service_type(config.mdns_type)
    instance = (config.mdns_instance or "IoT-MCP").strip()
    if any(char in instance for char in "\r\n\x00") or len(instance) > _MAX_LABEL:
        raise DiscoveryError(
            f"instance name {instance!r} is empty, too long (>{_MAX_LABEL} chars) or contains "
            "newlines"
        )
    addresses = local_ipv4_addresses()
    if not addresses:
        raise DiscoveryError(
            "no usable non-loopback IPv4 address found, so there is nothing to advertise"
        )
    zc: Any = None
    try:
        zc = Zeroconf()
        info = ServiceInfo(
            qualified,
            f"{instance}.{qualified}",
            addresses=[socket.inet_aton(item) for item in addresses if ":" not in item],
            port=config.port,
            properties=config.txt_records(),
            server=service_hostname(),
        )
        # allow_name_change: if an Avahi-provided name is already taken we would rather
        # advertise as "IoT-MCP #2" than refuse to start.
        zc.register_service(info, allow_name_change=True)
    except Exception as exc:  # noqa: BLE001 - every responder failure must be reportable
        if zc is not None:
            try:
                zc.close()
            except Exception:  # noqa: BLE001
                pass
        raise DiscoveryError(
            f"could not register {qualified} via mDNS ({type(exc).__name__}: {exc}). If Avahi or "
            "systemd-resolved owns port 5353, run with --no-mdns and connect by URL."
        ) from exc
    registered = str(getattr(info, "name", f"{instance}.{qualified}"))
    final_instance = registered.split(f".{qualified}", 1)[0] or instance
    return Advertisement(
        service_type=qualified,
        instance=final_instance,
        port=config.port,
        addresses=tuple(addresses),
        urls=tuple(config.url_for(item) for item in addresses),
        _zeroconf=zc,
        _info=info,
    )


def try_advertise(config: ServerConfig) -> tuple[Advertisement | None, str]:
    """Advertise, never raising. Returns ``(handle, message_for_the_operator)``.

    This is what the server calls: mDNS is an optimisation, so its failure is logged and the
    server keeps serving on the configured URL.
    """
    try:
        handle = advertise(config)
    except DiscoveryError as exc:
        return None, f"mDNS disabled, connect manually at {config.local_url} — {exc}"
    except Exception as exc:  # noqa: BLE001 - belt and braces: the server must survive anything
        return None, (
            f"mDNS disabled after an unexpected error ({type(exc).__name__}: {exc}); "
            f"connect manually at {config.local_url}"
        )
    return handle, "advertising " + ", ".join(handle.urls)


async def stop_advertisement(handle: Advertisement | None) -> None:
    """Stop an advertisement off the event loop (zeroconf's teardown does blocking I/O)."""
    if handle is None:
        return
    await asyncio.to_thread(handle.stop)
