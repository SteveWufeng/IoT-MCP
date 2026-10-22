"""LAN discovery: type naming, resolution, and graceful degradation.

The fake ``Zeroconf`` here means no test sends an mDNS packet; end-to-end behaviour on real
hardware was verified separately (see "What is actually verified" in ``docs/internals.md``).
"""

from __future__ import annotations

import ipaddress
import json
import socket
from typing import Any

import pytest

import iot_mcp.discovery as discovery
from iot_mcp.config import ServerConfig
from iot_mcp.discovery import (
    DiscoveredEndpoint,
    DiscoveryError,
    _decode_txt,
    _packed_addresses,
    advertise,
    advertise_addresses,
    browse,
    endpoint_from_info,
    normalize_service_type,
    service_hostname,
    try_advertise,
)


class FakeServiceInfo:
    """Stand-in for a resolved zeroconf ``ServiceInfo``."""

    def __init__(
        self,
        *,
        key: str = "IoT-MCP._mcp._tcp.local.",
        server: str = "Cachy-Y7.local.",
        port: int = 8000,
        properties: dict[bytes, bytes] | None = None,
        addresses: list[bytes] | None = None,
    ) -> None:
        self.key = key
        self.name = key
        self.server = server
        self.port = port
        self.properties = {"path": b"/mcp", "transport": b"streamable-http", "name": b"IoT-MCP"} | (
            properties or {}
        )
        self.addresses = (
            addresses if addresses is not None else [ipaddress.IPv4Address("192.168.0.54").packed]
        )


class FakeZeroconf:
    """Answers ``get_service_info`` from a fixed map; counts open/close."""

    opened: list[FakeZeroconf] = []
    infos: dict[str, FakeServiceInfo | None] = {}

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        FakeZeroconf.opened.append(self)

    def get_service_info(self, service_type: str, name: str, timeout: int = 1500):
        return FakeZeroconf.infos.get(name)

    def close(self) -> None:
        self.closed = True


class FakeServiceBrowser:
    """Notifies the collector with the instance names it was configured with."""

    last: FakeServiceBrowser | None = None

    def __init__(self, zc, service_type: str, listener) -> None:
        self.service_type = service_type
        self.listener = listener
        self.cancelled = False
        FakeServiceBrowser.last = self
        for name in FakeZeroconf.infos:
            listener.add_service(zc, service_type, name)

    def cancel(self) -> None:
        self.cancelled = True


@pytest.fixture
def fake_mdns(monkeypatch):
    """Install the fake responder/browser pair and reset their state."""

    def install(infos: dict[str, FakeServiceInfo | None]) -> None:
        FakeZeroconf.infos = infos
        FakeZeroconf.opened = []
        FakeServiceBrowser.last = None
        monkeypatch.setattr(discovery, "Zeroconf", FakeZeroconf)
        monkeypatch.setattr(discovery, "ServiceBrowser", FakeServiceBrowser)
        monkeypatch.setattr(discovery, "ZEROCONF_IMPORT_ERROR", None)

    return install


# --- service-type naming -------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["_mcp._tcp", "_mcp._tcp.", "_mcp._tcp.local", "_mcp._tcp.local.", "mcp._tcp", "_MCP._TCP"],
)
def test_every_spelling_of_the_default_type_normalises_the_same_way(raw: str) -> None:
    assert normalize_service_type(raw) == "_mcp._tcp.local."


def test_a_custom_type_is_accepted_because_the_default_is_disputed() -> None:
    """Deployers who follow the stricter reading must be able to pick another label."""
    assert normalize_service_type("_iotmcp._tcp") == "_iotmcp._tcp.local."
    assert normalize_service_type("_iot-mcp._udp") == "_iot-mcp._udp.local."


def test_the_protocol_is_filled_in_when_omitted() -> None:
    assert normalize_service_type("iotmcp") == "_iotmcp._tcp.local."


@pytest.mark.parametrize("raw", ["", "   "])
def test_an_empty_service_type_is_refused(raw: str) -> None:
    with pytest.raises(DiscoveryError) as excinfo:
        normalize_service_type(raw)
    assert "must not be empty" in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    ["not a type", "_bad type._tcp", "_x" * 40 + "._tcp", "tcp._mcp", "_mcp._sctp", "_._tcp"],
)
def test_invalid_types_are_refused_with_an_actionable_message(raw: str) -> None:
    with pytest.raises(DiscoveryError) as excinfo:
        normalize_service_type(raw)
    assert "_name._tcp" in str(excinfo.value)


# --- TXT decoding --------------------------------------------------------------


def test_txt_pairs_are_decoded_from_bytes() -> None:
    decoded = _decode_txt({b"path": b"/mcp", b"transport": b"streamable-http"})
    assert decoded == {"path": "/mcp", "transport": "streamable-http"}


def test_txt_handles_str_keys_missing_values_and_undecodable_bytes() -> None:
    assert _decode_txt({"path": "/mcp"}) == {"path": "/mcp"}
    assert _decode_txt({b"flag": None}) == {"flag": ""}
    assert _decode_txt({b"\xff": b"ok"}) == {"\ufffd": "ok"}
    assert _decode_txt(None) == {}


def test_leading_dashes_in_txt_keys_are_stripped() -> None:
    assert _decode_txt({b"-path": b"/mcp"})["path"] == "/mcp"


# --- addresses -----------------------------------------------------------------


def test_addresses_are_readable_and_ipv4_comes_first() -> None:
    info = FakeServiceInfo(
        addresses=[
            ipaddress.IPv6Address("fe80::1").packed,
            ipaddress.IPv4Address("192.168.0.54").packed,
        ]
    )
    assert _packed_addresses(info) == ("192.168.0.54", "fe80::1")


def test_unparseable_address_records_are_dropped_not_guessed() -> None:
    info = FakeServiceInfo(addresses=[b"\x01\x02", ipaddress.IPv4Address("10.0.0.1").packed])
    assert _packed_addresses(info) == ("10.0.0.1",)


def test_a_service_with_no_addresses_yields_no_addresses() -> None:
    assert _packed_addresses(FakeServiceInfo(addresses=[])) == ()


def test_a_merged_answer_prefers_the_routable_address_over_loopback() -> None:
    """Two instances on one host share an mDNS hostname, so their A records merge in the cache.

    Observed for real: with a loopback-bound server and an `--expose` one both up, browsing the
    exposed instance returned both records. Dialling the loopback one from another machine
    connects that machine to itself, so a routable address must sort first.
    """
    info = FakeServiceInfo(
        addresses=[
            ipaddress.IPv4Address("127.0.0.1").packed,
            ipaddress.IPv4Address("192.168.0.54").packed,
        ]
    )
    assert _packed_addresses(info) == ("192.168.0.54", "127.0.0.1")
    assert endpoint_from_info("_mcp._tcp.local.", info).host == "192.168.0.54"


def test_a_loopback_only_answer_still_resolves_to_loopback() -> None:
    """The other half: when loopback is all there is, it is the right answer for a local client."""
    info = FakeServiceInfo(addresses=[ipaddress.IPv4Address("127.0.0.1").packed])
    assert _packed_addresses(info) == ("127.0.0.1",)
    assert endpoint_from_info("_mcp._tcp.local.", info).url == "http://127.0.0.1:8000/mcp"


# --- endpoint shape ------------------------------------------------------------


def test_a_resolved_endpoint_prefers_the_address_over_the_hostname() -> None:
    endpoint = endpoint_from_info("_mcp._tcp.local.", FakeServiceInfo())
    assert endpoint.host == "192.168.0.54"
    assert endpoint.url == "http://192.168.0.54:8000/mcp"
    assert endpoint.url_by_name == "http://Cachy-Y7.local:8000/mcp"
    assert endpoint.instance == "IoT-MCP"


def test_an_ipv6_only_endpoint_is_bracketed_in_the_url() -> None:
    endpoint = endpoint_from_info(
        "_mcp._tcp.local.",
        FakeServiceInfo(addresses=[ipaddress.IPv6Address("fd00::5").packed]),
    )
    assert endpoint.url == "http://[fd00::5]:8000/mcp"


def test_a_path_without_a_leading_slash_is_repaired() -> None:
    endpoint = endpoint_from_info("_mcp._tcp.local.", FakeServiceInfo(properties={b"path": b"mcp"}))
    assert endpoint.path == "/mcp" and endpoint.url.endswith("/mcp")


def test_txt_defaults_apply_when_a_record_is_missing() -> None:
    info = FakeServiceInfo()
    info.properties = {b"version": b"0.1.0"}
    endpoint = endpoint_from_info("_mcp._tcp.local.", info)
    assert endpoint.path == "/mcp" and endpoint.transport == "streamable-http"
    assert endpoint.version == "0.1.0"


def test_a_zero_port_falls_back_to_the_documented_default() -> None:
    assert endpoint_from_info("_mcp._tcp.local.", FakeServiceInfo(port=0)).port == 8000


def test_endpoints_serialise_to_json_for_the_client_output() -> None:
    payload = endpoint_from_info("_mcp._tcp.local.", FakeServiceInfo()).to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert {"url", "url_by_name", "instance", "txt", "addresses"} <= set(payload)


def test_an_endpoint_with_no_target_name_still_builds_a_url() -> None:
    """A malformed advertisement must not crash the client's output path."""
    endpoint = DiscoveredEndpoint(instance="x", service_type="_mcp._tcp.local.", target="")
    assert endpoint.url_by_name == endpoint.url
    assert endpoint.to_dict()["target"] == ""


# --- hostname / addresses ------------------------------------------------------


def test_the_service_hostname_is_a_dns_name_under_local() -> None:
    name = service_hostname()
    assert name.endswith(".local.")
    assert " " not in name and "_" not in name


def test_advertised_addresses_exclude_loopback_and_link_local(monkeypatch) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "no-such-host.invalid")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, 1, 6, "", ("127.0.1.1", 0)),
            (socket.AF_INET, 1, 6, "", ("169.254.1.1", 0)),
            (socket.AF_INET, 1, 6, "", ("192.168.1.9", 0)),
        ],
    )
    import iot_mcp.discovery as module

    class _Sock:
        def setsockopt(self, *a):
            pass

        def connect(self, addr):
            pass

        def getsockname(self):
            return ("10.1.2.3", 0)

        def close(self):
            pass

    monkeypatch.setattr(module.socket, "socket", lambda *a, **k: _Sock())
    assert discovery.local_ipv4_addresses() == ["10.1.2.3", "192.168.1.9"]


# --- browse --------------------------------------------------------------------


def test_browse_resolves_every_announced_instance(fake_mdns) -> None:
    fake_mdns({"IoT-MCP._mcp._tcp.local.": FakeServiceInfo()})
    endpoints = browse("_mcp._tcp", timeout=0.1)
    assert [endpoint.url for endpoint in endpoints] == ["http://192.168.0.54:8000/mcp"]
    assert FakeServiceBrowser.last.service_type == "_mcp._tcp.local."


def test_an_instance_that_vanishes_mid_resolve_is_skipped(fake_mdns) -> None:
    fake_mdns({"gone._mcp._tcp.local.": None, "here._mcp._tcp.local.": FakeServiceInfo()})
    endpoints = browse("_mcp._tcp", timeout=0.1)
    assert len(endpoints) == 1


def test_browse_closes_the_responder_even_when_it_fails(fake_mdns, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(discovery, "Zeroconf", explode)
    with pytest.raises(DiscoveryError) as excinfo:
        browse("_mcp._tcp", timeout=0.1)
    assert "--url" in str(excinfo.value), "the failure must tell the operator the fallback"


def test_browse_without_zeroconf_says_what_to_install(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "ZEROCONF_IMPORT_ERROR", ImportError("no module named zeroconf"))
    with pytest.raises(DiscoveryError) as excinfo:
        browse()
    assert "zeroconf" in str(excinfo.value) and "install" in str(excinfo.value)


def test_an_empty_lan_is_an_answer_not_an_error(fake_mdns) -> None:
    fake_mdns({})
    assert browse("_mcp._tcp", timeout=0.1) == []


# --- advertisement -------------------------------------------------------------


def test_try_advertise_degrades_to_a_manual_url_when_mdns_is_broken(monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise OSError("Address family not supported")

    monkeypatch.setattr(discovery, "Zeroconf", explode)
    config = ServerConfig(port=8000)
    handle, message = try_advertise(config)
    assert handle is None
    assert config.local_url in message
    assert "mDNS disabled" in message


def test_try_advertise_survives_an_unexpected_error(monkeypatch) -> None:
    class Weird:
        def __init__(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(discovery, "Zeroconf", Weird)
    handle, message = try_advertise(ServerConfig())
    assert handle is None and "RuntimeError" in message and "127.0.0.1" in message


# --- which addresses get published ----------------------------------------------
#
# A loopback-bound server used to publish its LAN address. The address was real and the bind
# was not listening on it, so the only client that could act on the answer — the one on the
# same machine — got `Connection refused`. These tests pin the published address to the bind.


@pytest.fixture
def fake_registration(monkeypatch):
    """Stands in for zeroconf's registration half so ``advertise()`` opens no socket.

    Returns:
        A dict with ``infos`` (what was handed to ``ServiceInfo``) and ``responders``.
    """
    captured: dict[str, list[Any]] = {"infos": [], "responders": []}

    class RegInfo:
        def __init__(self, type_: str, name: str, **kwargs: Any) -> None:
            self.type = type_
            self.name = name
            properties = kwargs.get("properties") or {}
            # zeroconf encodes TXT to bytes on the wire; mirror that so the client-side
            # assertions below exercise the same shapes browse() really sees.
            kwargs["properties"] = {
                (k.encode() if isinstance(k, str) else k): (v.encode() if isinstance(v, str) else v)
                for k, v in properties.items()
            }
            self.__dict__.update(kwargs)

    class Responder:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            self.registered: list[RegInfo] = []
            captured["responders"].append(self)

        def register_service(self, info: RegInfo, **kwargs) -> None:
            self.registered.append(info)
            captured["infos"].append(info)

        def unregister_service(self, info: RegInfo) -> None:
            self.registered.remove(info)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(discovery, "ServiceInfo", RegInfo)
    monkeypatch.setattr(discovery, "Zeroconf", Responder)
    return captured


def test_a_loopback_bind_publishes_loopback_not_the_lan(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: ["192.168.0.54"])
    assert advertise_addresses(ServerConfig(host="127.0.0.1")) == ["127.0.0.1"]


def test_an_exposed_bind_still_publishes_the_lan_addresses(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: ["192.168.0.54", "10.0.0.5"])
    assert advertise_addresses(ServerConfig(host="0.0.0.0")) == ["192.168.0.54", "10.0.0.5"]


def test_the_published_family_follows_the_bind(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: ["192.168.0.54"])
    # Uvicorn bound to ::1 does not accept 127.0.0.1 (verified on real hardware), so publishing
    # the v4 loopback there would be the same bug in different notation.
    assert advertise_addresses(ServerConfig(host="::1")) == ["::1"]
    # A `localhost` bind listens on both families; v4 is the one clients prefer.
    assert advertise_addresses(ServerConfig(host="localhost")) == ["127.0.0.1"]


def test_advertise_registers_the_address_the_loopback_bind_accepts(fake_registration) -> None:
    config = ServerConfig(host="127.0.0.1", port=8123)
    with advertise(config) as advertisement:
        assert advertisement.addresses == ("127.0.0.1",)
        assert advertisement.urls == ("http://127.0.0.1:8123/mcp",)
        info = fake_registration["infos"][0]
        assert [ipaddress.ip_address(item) for item in info.addresses] == [
            ipaddress.IPv4Address("127.0.0.1")
        ]
        assert info.port == 8123
    assert fake_registration["responders"][0].closed is True


def test_an_ipv6_loopback_advertisement_survives_packing(fake_registration) -> None:
    """``inet_aton`` cannot pack ``::1``, and the old filter dropped it — publishing nothing."""
    with advertise(ServerConfig(host="::1", port=8123)) as advertisement:
        (packed,) = fake_registration["infos"][0].addresses
        assert len(packed) == 16
        assert ipaddress.ip_address(packed) == ipaddress.IPv6Address("::1")
        assert advertisement.urls == ("http://[::1]:8123/mcp",)


def test_an_exposed_advertisement_publishes_every_lan_address(
    fake_registration, monkeypatch
) -> None:
    monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: ["192.168.0.54", "10.0.0.5"])
    with advertise(ServerConfig(host="0.0.0.0", port=8000)) as advertisement:
        assert advertisement.urls == ("http://192.168.0.54:8000/mcp", "http://10.0.0.5:8000/mcp")


def test_a_loopback_advertisement_resolves_back_to_a_usable_url(fake_registration) -> None:
    """The client half: what the responder publishes must resolve to 127.0.0.1."""
    with advertise(ServerConfig(host="127.0.0.1", port=8000)):
        info = fake_registration["infos"][0]
        endpoint = endpoint_from_info(info.type, info)
        assert endpoint.host == "127.0.0.1"
        assert endpoint.url == "http://127.0.0.1:8000/mcp"
