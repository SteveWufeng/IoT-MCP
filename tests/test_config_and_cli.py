"""Configuration, the CLI surface, and the security-relevant bind defaults."""

from __future__ import annotations

import io
import json
import pathlib

import pytest

from iot_mcp.cli import (
    _transport_security,
    build_parser,
    config_from_args,
    consent_gate,
    main,
)
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
    assert set(txt) == {"path", "transport", "name", "version", "auth", "browser"}
    assert json.dumps(txt)


def test_txt_records_declare_the_auth_and_browser_policy_but_no_secret() -> None:
    """A client should learn "this server wants a token" before it handshakes, not from a 401.

    The two extra TXT keys are policy, never material: neither the token nor its fingerprint nor
    the add-on's launch command belongs in a record every host on the LAN can read.
    """
    open_loopback = ServerConfig().txt_records()
    assert open_loopback["auth"] == "none"
    assert open_loopback["browser"] == "off"

    locked = ServerConfig(token="a-token-of-sufficient-length", browser_enabled=True).txt_records()
    assert locked["auth"] == "token"
    assert locked["browser"] == "on"
    assert "a-token-of-sufficient-length" not in json.dumps(locked)


def test_an_explicit_no_auth_downgrades_the_declared_policy() -> None:
    """``--no-auth`` against an environmental token must not advertise ``auth=token``."""
    config = ServerConfig(token="a-token-of-sufficient-length", no_auth=True)
    assert config.authenticated is False
    assert config.auth_settings() is None
    assert config.txt_records()["auth"] == "none"


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


def test_an_authenticated_lan_bind_warns_about_cleartext_not_about_being_open() -> None:
    """The banner has to describe the configuration that is actually running.

    A token changes the exposure from "anyone" to "anyone who can sniff the secret", and an
    operator reading "Authentication is DISABLED" on a server that does have a token learns to
    ignore the banner.
    """
    stream = io.StringIO()
    config = ServerConfig(host="0.0.0.0", port=8000, token="a-token-of-sufficient-length")
    assert config.warn_if_exposed(stream) is True
    text = stream.getvalue()
    assert "REACHABLE FROM THE LAN" in text
    assert "bearer token" in text
    assert "cleartext" in text
    assert "Authentication is DISABLED" not in text
    assert "ssh -L" in text


def test_the_warning_says_so_when_the_browser_tier_is_enabled() -> None:
    stream = io.StringIO()
    ServerConfig(host="0.0.0.0", browser_enabled=True).warn_if_exposed(stream)
    text = stream.getvalue()
    assert "BROWSER ADD-ON IS ENABLED" in text
    assert "real browser" in text


# --- the exposed-browser rule --------------------------------------------------


def test_an_exposed_browser_without_a_token_refuses_to_start() -> None:
    config = ServerConfig(host="0.0.0.0", browser_enabled=True)
    assert config.browser_needs_auth is True
    problem = config.browser_policy_error()
    assert problem is not None
    assert "browser" in problem and "token" in problem


def test_the_browser_rule_is_a_startup_refusal_not_a_runtime_one() -> None:
    """``--browser-allow-no-auth`` is the only override, and it is the operator's own signature."""
    overridden = ServerConfig(host="0.0.0.0", browser_enabled=True, browser_allow_no_auth=True)
    assert overridden.browser_needs_auth is False
    assert overridden.browser_policy_error() is None


def test_a_loopback_browser_needs_no_token() -> None:
    """The rule is about network reachability, not about the browser being dangerous per se."""
    config = ServerConfig(host="127.0.0.1", browser_enabled=True)
    assert config.browser_needs_auth is False
    assert config.browser_policy_error() is None


def test_configuring_both_token_sources_is_refused_rather_than_resolved() -> None:
    """Silently preferring one secret over the other is how a stale token ends up in production."""
    from iot_mcp.auth import AuthConfigError

    config = ServerConfig(token="a-token-of-sufficient-length", token_file="/tmp/whatever")
    with pytest.raises(AuthConfigError):
        config.auth_settings()


def test_a_token_file_is_read_lazily_and_yields_the_authenticator(tmp_path) -> None:
    secret = tmp_path / "token"
    secret.write_text("a-token-of-sufficient-length\n", encoding="utf-8")
    secret.chmod(0o600)
    auth = ServerConfig(token_file=str(secret)).auth_settings()
    assert auth is not None
    assert auth.verify("a-token-of-sufficient-length") is True
    assert auth.verify("nope-not-it") is False


def test_no_auth_wins_over_an_environmental_token_without_losing_the_ambiguity_check() -> None:
    from iot_mcp.auth import AuthConfigError

    assert ServerConfig(token="a-token", no_auth=True).auth_settings() is None
    with pytest.raises(AuthConfigError):
        ServerConfig(token="a-token", token_file="/tmp/whatever", no_auth=True).auth_settings()


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


# --- CLI: authentication and browser flags -------------------------------------


def test_a_token_flag_makes_the_server_authenticated() -> None:
    config = config_from_args(parse("--token", "a-token-of-sufficient-length"))
    assert config.authenticated is True
    assert config.txt_records()["auth"] == "token"


def test_no_auth_clears_a_token_that_came_from_the_environment(monkeypatch) -> None:
    """A flag has to be able to overrule the environment, or the flag is decorative."""
    monkeypatch.setenv("IOT_MCP_TOKEN", "a-token-of-sufficient-length")
    assert config_from_args(parse()).authenticated is True
    declined = config_from_args(parse("--no-auth"))
    assert declined.authenticated is False
    assert declined.token is None and declined.no_auth is True


def test_the_query_token_opt_in_is_explicit() -> None:
    assert config_from_args(parse()).allow_query_token is False
    assert config_from_args(parse("--allow-query-token")).allow_query_token is True


def test_browser_flags_reach_the_config() -> None:
    config = config_from_args(
        parse(
            "--browser",
            "--browser-start",
            "--browser-allow-no-auth",
            "--browser-argv",
            "node srv.js --headless",
        )
    )
    assert config.browser_enabled is True
    assert config.browser_autostart is True
    assert config.browser_allow_no_auth is True
    assert config.browser_argv == ("node", "srv.js", "--headless")


def test_browser_defaults_are_off(monkeypatch) -> None:
    """Every browser flag is additive; nothing here may turn the tier on by default."""
    for name in ("IOT_MCP_BROWSER", "IOT_MCP_BROWSER_AUTOSTART", "IOT_MCP_BROWSER_ARGV"):
        monkeypatch.delenv(name, raising=False)
    config = config_from_args(parse())
    assert config.browser_enabled is False
    assert config.browser_autostart is False
    assert config.browser_argv is None


def test_an_unparseable_browser_argv_is_a_usage_error_not_a_crash() -> None:
    with pytest.raises(ValueError, match="browser-argv"):
        config_from_args(parse("--browser-argv", "npx 'unbalanced"))


def test_main_reports_a_bad_browser_argv_instead_of_tracebacking(capsys) -> None:
    assert main(["--browser-argv", "npx 'unbalanced"]) == 2
    assert "browser-argv" in capsys.readouterr().err


def test_generate_token_prints_a_secret_without_binding(capsys) -> None:
    assert main(["--generate-token"]) == 0
    secret = capsys.readouterr().out.strip()
    assert 24 <= len(secret) <= 256
    assert " " not in secret


def test_generate_token_with_a_file_writes_it_at_owner_only(capsys, tmp_path) -> None:
    target = tmp_path / "token"
    assert main(["--generate-token", "--token-file", str(target)]) == 0
    assert target.exists()
    assert target.stat().st_mode & 0o077 == 0
    out = capsys.readouterr().out
    assert str(target) in out
    assert target.read_text(encoding="utf-8").strip()


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


# --- interactive consent gate --------------------------------------------------


class FakeTTY:
    """A stand-in for an interactive stdin: isatty() is true, readline() replays lines."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


def test_the_consent_gate_passes_loopback_through_in_silence() -> None:
    gate = consent_gate(ServerConfig(host="127.0.0.1"), stdin=FakeTTY(["1"]), stdout=io.StringIO())
    assert gate is not None and gate.token_file is None


def test_the_consent_gate_passes_an_authenticated_bind_through() -> None:
    config = ServerConfig(host="0.0.0.0", token="x" * 32)
    assert consent_gate(config, stdin=FakeTTY(["1"]), stdout=io.StringIO()) is config


def test_the_consent_gate_stays_silent_when_there_is_no_terminal() -> None:
    """A headless start (systemd, CI, a pipe) must never block on a question."""
    config = ServerConfig(host="0.0.0.0")
    out = io.StringIO()
    assert consent_gate(config, stdin=io.StringIO(""), stdout=out) is config
    assert out.getvalue() == ""


def test_the_consent_gate_sets_a_token_up_when_the_operator_asks_for_it(
    monkeypatch, tmp_path
) -> None:
    out = io.StringIO()
    target = tmp_path / "token"
    monkeypatch.setattr("iot_mcp.cli.default_root", lambda: tmp_path)
    gate = consent_gate(
        ServerConfig(host="0.0.0.0"),
        stdin=FakeTTY(["1"]),
        stdout=out,
    )
    assert gate is not None
    assert gate.token_file == str(target)
    assert target.read_text(encoding="utf-8").strip()
    assert target.stat().st_mode & 0o077 == 0
    text = out.getvalue()
    assert "0600" in text
    assert target.read_text(encoding="utf-8").strip() not in text  # the secret is never echoed


def test_the_consent_gate_uses_a_token_file_the_operator_already_has(tmp_path) -> None:
    existing = tmp_path / "token"
    existing.write_text("s" * 32 + "\n", encoding="utf-8")
    gate = consent_gate(
        ServerConfig(host="0.0.0.0"),
        stdin=FakeTTY(["2", str(existing)]),
        stdout=io.StringIO(),
    )
    assert gate is not None and gate.token_file == str(existing)


def test_the_consent_gate_refuses_a_token_file_that_cannot_be_read(tmp_path) -> None:
    """A typo'd path must be caught at the prompt, not halfway through booting."""
    out = io.StringIO()
    gate = consent_gate(
        ServerConfig(host="0.0.0.0"),
        stdin=FakeTTY(["2", str(tmp_path / "nope")]),
        stdout=out,
    )
    assert gate is None
    assert "cannot read token file" in out.getvalue()


def test_the_consent_gate_aborts_on_an_empty_answer() -> None:
    out = io.StringIO()
    assert consent_gate(ServerConfig(host="0.0.0.0"), stdin=FakeTTY([""]), stdout=out) is None
    assert "aborted" in out.getvalue()


def test_running_without_auth_requires_typing_the_phrase() -> None:
    """Enter, a half-answer and a wrong phrase must all refuse; only the phrase proceeds."""
    for answer in ("", "y", "yes", "no", "no-auth-ish"):
        out = io.StringIO()
        config = ServerConfig(host="0.0.0.0")
        gate = consent_gate(config, stdin=FakeTTY(["3", answer]), stdout=out)
        assert gate is None, f"{answer!r} must not start an open server"
    out = io.StringIO()
    gate = consent_gate(ServerConfig(host="0.0.0.0"), stdin=FakeTTY(["3", "no auth"]), stdout=out)
    assert gate is not None and not gate.authenticated


def test_the_consent_gate_also_covers_no_auth_overriding_an_environmental_token() -> None:
    """--no-auth on an exposed bind is an explicit override, so it earns the same pause."""
    config = ServerConfig(host="0.0.0.0", token="x" * 32, no_auth=True)
    assert consent_gate(config, stdin=FakeTTY(["3", ""]), stdout=io.StringIO()) is None
