"""Configuration, the CLI surface, and the security-relevant bind defaults."""

from __future__ import annotations

import io
import json
import pathlib

import pytest

from iot_mcp.cli import _transport_security, build_parser, config_from_args, main
from iot_mcp.config import (
    DEFAULT_HOST,
    DEFAULT_MCP_PATH,
    DEFAULT_MDNS_TYPE,
    DEFAULT_PORT,
    LOOPBACK_HOSTS,
    ServerConfig,
)


def parse(*argv: str):
    return build_parser().parse_args(list(argv))


# --- defaults ------------------------------------------------------------------


def test_the_default_bind_is_loopback(monkeypatch) -> None:
    """The safe default must not depend on the environment of whoever starts the server."""
    monkeypatch.delenv("IOT_MCP_HOST", raising=False)
    assert ServerConfig().host == DEFAULT_HOST == "127.0.0.1"
    assert ServerConfig().exposed is False


def test_the_service_type_has_exactly_one_default_definition() -> None:
    """The disputed naming must be changeable from one place; see the module docstring."""
    import iot_mcp.config as config_module
    import iot_mcp.discovery as discovery_module

    # DNS-SD service types come out of the same RFC 6335 registry as service names, and `mcp`
    # is assigned there (port 4458, Matrix Configuration Protocol, 2006-12). Hence `_appmcp`.
    assert DEFAULT_MDNS_TYPE == "_appmcp._tcp"
    # DNS labels are length-limited; `_appmcp` fits, and the check keeps a future rename honest.
    assert len(DEFAULT_MDNS_TYPE.split(".")[0]) <= 15
    # discovery re-exports the same object rather than repeating the literal
    assert discovery_module.DEFAULT_MDNS_TYPE is config_module.DEFAULT_MDNS_TYPE
    # ...and nothing else in the package carries it as a *value*. Docstrings may name the label;
    # a quoted literal elsewhere would be a second default waiting to drift.
    package = pathlib.Path(config_module.__file__).parent
    quoted = tuple(f"{mark}{DEFAULT_MDNS_TYPE}{mark}" for mark in "\"'")
    offenders = [
        str(path.relative_to(package))
        for path in sorted(package.rglob("*.py"))
        if path.name != "config.py"
        and any(marker in path.read_text(encoding="utf-8") for marker in quoted)
    ]
    assert offenders == [], f"service type hardcoded outside config.py: {offenders}"


@pytest.mark.parametrize("label", ["_iotmcp._tcp", "_mcp._tcp", "_AppMCP._TCP.local."])
def test_the_service_type_is_a_flag_not_a_constant(label: str) -> None:
    """Any label is selectable, `_mcp._tcp` included: it lost the default, not the support."""
    config = config_from_args(parse("--mdns-type", label))
    assert config.mdns_type == label


def test_environment_overrides_are_prefixed_and_typed(monkeypatch) -> None:
    monkeypatch.setenv("IOT_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("IOT_MCP_PORT", "8137")
    monkeypatch.setenv("IOT_MCP_PATH_PREFIX", "/mcp-v2")
    monkeypatch.setenv("IOT_MCP_MDNS_TYPE", "_iotmcp._tcp")
    monkeypatch.setenv("IOT_MCP_MDNS_INSTANCE", "Studio Box")
    monkeypatch.setenv("IOT_MCP_NAME", "studio")
    monkeypatch.setenv("IOT_MCP_DRIVER", "null")
    config = ServerConfig()
    assert (config.host, config.port, config.path) == ("0.0.0.0", 8137, "/mcp-v2")
    assert config.mdns_type == "_iotmcp._tcp" and config.mdns_instance == "Studio Box"
    assert config.server_name == "studio" and config.driver == "null"
    assert config.exposed is True


def test_an_unset_driver_means_auto_detect(monkeypatch) -> None:
    monkeypatch.delenv("IOT_MCP_DRIVER", raising=False)
    assert ServerConfig().driver is None


@pytest.mark.parametrize(
    "host,exposed",
    [
        ("127.0.0.1", False),
        ("localhost", False),
        ("::1", False),
        ("0.0.0.0", True),
        ("::", True),
        ("192.168.0.54", True),
    ],
)
def test_exposure_is_judged_by_bind_address_not_intent(host: str, exposed: bool) -> None:
    assert ServerConfig(host=host).exposed is exposed


def test_wildcard_bind_is_never_treated_as_safe() -> None:
    """``0.0.0.0`` is every interface; it must not appear in the loopback set."""
    assert "0.0.0.0" not in LOOPBACK_HOSTS
    assert "::" not in LOOPBACK_HOSTS


# --- URLs and TXT records ------------------------------------------------------


def test_urls_are_built_for_a_target_host() -> None:
    config = ServerConfig(port=8000, path="/mcp")
    assert config.url_for("B.local") == "http://B.local:8000/mcp"
    assert config.url_for("192.168.0.54.") == "http://192.168.0.54:8000/mcp"


def test_ipv6_targets_are_bracketed_once() -> None:
    config = ServerConfig()
    assert config.url_for("fe80::1") == "http://[fe80::1]:8000/mcp"
    assert config.url_for("[fe80::1]") == "http://[fe80::1]:8000/mcp"


def test_the_local_url_maps_a_wildcard_bind_back_to_loopback() -> None:
    assert ServerConfig(host="0.0.0.0").local_url == "http://127.0.0.1:8000/mcp"
    assert ServerConfig(host="192.168.0.54").local_url == "http://192.168.0.54:8000/mcp"


def test_txt_records_describe_the_transport_and_mount_point() -> None:
    txt = ServerConfig(path="/mcp").txt_records()
    assert txt["path"] == "/mcp"
    assert txt["transport"] == "streamable-http"
    assert set(txt) == {"path", "transport", "name", "version"}
    assert json.dumps(txt)


# --- the exposure warning ------------------------------------------------------


def test_loopback_prints_nothing() -> None:
    stream = io.StringIO()
    assert ServerConfig(host="127.0.0.1").warn_if_exposed(stream) is False
    assert stream.getvalue() == ""


def test_a_lan_bind_warns_loudly_and_says_there_is_no_auth() -> None:
    stream = io.StringIO()
    config = ServerConfig(host="0.0.0.0", port=8000)
    assert config.warn_if_exposed(stream) is True
    text = stream.getvalue()
    assert "REACHABLE FROM THE LAN" in text
    assert "Authentication is DISABLED" in text
    assert "ssh -L" in text, "the warning must offer the safe alternative"
    assert "0.0.0.0:8000" in text


def test_the_warning_reports_whether_host_validation_is_on() -> None:
    disabled = io.StringIO()
    ServerConfig(host="0.0.0.0", disable_rebinding_protection=True).warn_if_exposed(disabled)
    assert "DISABLED" in disabled.getvalue()

    narrowed = io.StringIO()
    ServerConfig(host="0.0.0.0", allowed_hosts=("B.local:8000",)).warn_if_exposed(narrowed)
    assert "B.local:8000" in narrowed.getvalue()


# --- CLI -----------------------------------------------------------------------


def test_no_flags_means_loopback_and_discovery_on() -> None:
    config = config_from_args(parse())
    assert config.host == "127.0.0.1" and config.port == DEFAULT_PORT
    assert config.path == DEFAULT_MCP_PATH
    assert config.advertise is True
    assert config.allows_lan_clients is False


def test_expose_binds_every_interface_and_turns_host_validation_off() -> None:
    config = config_from_args(parse("--expose"))
    assert config.host == "0.0.0.0"
    assert config.disable_rebinding_protection is True
    assert config.allows_lan_clients is True


def test_expose_with_an_explicit_host_respects_the_host() -> None:
    config = config_from_args(parse("--expose", "--host", "192.168.0.54"))
    assert config.host == "192.168.0.54"


def test_a_port_flag_alone_never_widens_the_bind_address() -> None:
    """Changing a port must not quietly turn a loopback endpoint into a LAN one."""
    config = config_from_args(parse("--port", "9999"))
    assert config.host == "127.0.0.1"
    assert config.disable_rebinding_protection is False


def test_allow_host_keeps_validation_on_with_an_explicit_list() -> None:
    config = config_from_args(
        parse("--expose", "--allow-host", "B.local:8000", "--allow-host", "b.local:8000")
    )
    assert config.allowed_hosts == ("B.local:8000", "b.local:8000")
    assert config.disable_rebinding_protection is False, (
        "an allowlist is not a reason to disable checks"
    )


def test_no_mdns_disables_advertising() -> None:
    assert config_from_args(parse("--no-mdns")).advertise is False


def test_expose_with_a_loopback_host_keeps_host_validation() -> None:
    """``--expose --host 127.0.0.1`` asks for exposure and then declines it.

    The relaxation used to follow the raw flag, so this combination dropped ``Host`` validation
    on a bind that ``warn_if_exposed()`` treats as private: a security downgrade with no warning
    to match it. Protection now follows the resolved bind, so the relaxation and the warning can
    only ever describe the same server.
    """
    config = config_from_args(parse("--expose", "--host", "127.0.0.1"))
    assert config.host == "127.0.0.1"
    assert config.exposed is False
    assert config.disable_rebinding_protection is False
    assert _transport_security(config) is None, (
        "loopback must pass nothing to the SDK, which means 'stay protected'"
    )


def test_a_routable_host_relaxes_host_validation_even_without_expose() -> None:
    """A routable ``--host`` is exposure whether or not the flag was used — and it says so."""
    for argv in (["--host", "192.168.0.54"], ["--host", "0.0.0.0"]):
        config = config_from_args(parse(*argv))
        assert config.exposed is True, argv
        assert config.disable_rebinding_protection is True, argv
        assert _transport_security(config).enable_dns_rebinding_protection is False, argv
        stream = io.StringIO()
        assert config.warn_if_exposed(stream) is True, (
            "relaxing Host validation must never be quieter than the bind that caused it"
        )


def test_host_validation_follows_the_resolved_bind_not_the_expose_flag() -> None:
    """One rule: protection is off exactly when the bind is exposed and no allowlist was given."""
    cases = [
        ([], False),
        (["--expose"], True),
        (["--expose", "--host", "127.0.0.1"], False),
        (["--expose", "--host", "localhost"], False),
        (["--expose", "--host", "::"], True),
        (["--host", "localhost"], False),
        (["--expose", "--allow-host", "B.local:8000"], False),
    ]
    for argv, relaxed in cases:
        config = config_from_args(parse(*argv))
        assert config.disable_rebinding_protection is relaxed, argv
        assert config.disable_rebinding_protection == (config.exposed and not config.allowed_hosts)


def test_flags_beat_environment(monkeypatch) -> None:
    monkeypatch.setenv("IOT_MCP_PORT", "8137")
    assert config_from_args(parse("--port", "9000")).port == 9000
    assert config_from_args(parse()).port == 8137


def test_the_stateless_and_log_flags_exist() -> None:
    args = parse("--stateless", "--log-level", "debug")
    assert args.stateless is True and args.log_level == "debug"


def test_an_invalid_log_level_is_refused() -> None:
    with pytest.raises(SystemExit):
        parse("--log-level", "everything")


def test_the_help_text_states_the_security_posture() -> None:
    text = build_parser().format_help()
    assert "NO authentication" in text
    assert "--expose" in text and "ssh -L" in text


def test_main_reports_the_version_without_starting_a_server() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_main_rejects_an_unknown_flag() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--please-hack-me"])
    assert excinfo.value.code != 0


# --- DNS-rebinding protection --------------------------------------------------


def test_loopback_keeps_the_sdk_default_protection() -> None:
    """On loopback the server passes nothing to the SDK, which means "stay protected"."""
    from mcp.server.transport_security import TransportSecuritySettings

    assert _transport_security(ServerConfig(host="127.0.0.1")) is None
    defaults = TransportSecuritySettings()
    assert defaults.enable_dns_rebinding_protection is True, (
        "if the SDK ever defaults to unprotected, passing None on loopback would be unsafe"
    )


def test_an_allowlist_replaces_the_default_hosts_but_stays_enabled() -> None:
    settings = _transport_security(ServerConfig(host="0.0.0.0", allowed_hosts=("B.local:8000",)))
    assert settings.enable_dns_rebinding_protection is True
    assert list(settings.allowed_hosts) == ["B.local:8000"]


def test_expose_disables_the_check_because_lan_hosts_cannot_be_enumerated() -> None:
    settings = _transport_security(ServerConfig(host="0.0.0.0", disable_rebinding_protection=True))
    assert settings.enable_dns_rebinding_protection is False


def test_an_allowlist_beats_the_expose_shortcut() -> None:
    """Passing --allow-host with --expose must narrow, not disable, validation."""
    config = config_from_args(parse("--expose", "--allow-host", "B.local:8000"))
    assert _transport_security(config).enable_dns_rebinding_protection is True
