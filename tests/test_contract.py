"""The invariants every driver shares: envelopes, typed errors, capability matrix.

These are the promises made to the agent, so they are asserted against every backend —
including the two this host cannot run.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

from iot_mcp.capabilities import ALL_CAPABILITIES, TOOL_FOR_CAPABILITY, Capability
from iot_mcp.drivers.base import NullDriver, Support, ok
from iot_mcp.drivers.linux import LinuxDriver
from iot_mcp.drivers.macos import MacOSDriver
from iot_mcp.drivers.windows import WindowsDriver
from iot_mcp.errors import (
    CommandFailedError,
    CommandTimeoutError,
    DependencyMissingError,
    DriverError,
    InvalidInputError,
    NotFoundError,
    UnsupportedCapabilityError,
)

DRIVERS: dict[str, Any] = {
    "linux": LinuxDriver,
    "macos": MacOSDriver,
    "windows": WindowsDriver,
}

TOOLS = (
    "list_actions",
    "list_apps",
    "open_app",
    "close_app",
    "open_url",
    "media_play_pause",
    "media_next",
    "media_previous",
    "media_status",
    "list_windows",
    "focus_window",
)


def bare(name: str) -> Any:
    """A driver for ``name`` that believes nothing is installed on this machine."""
    return DRIVERS[name](environ={}, which_fn=lambda tool: None)


# --- the tool/capability map is the single source of truth ----------------------


def test_every_capability_names_the_tool_that_exposes_it():
    # list_actions is introspection, not a capability, so it is absent from the map
    assert set(TOOL_FOR_CAPABILITY.values()) == set(TOOLS) - {"list_actions"}
    assert set(TOOL_FOR_CAPABILITY) == set(ALL_CAPABILITIES)
    assert len(set(TOOL_FOR_CAPABILITY.values())) == len(ALL_CAPABILITIES)


def test_capability_names_are_the_dotted_strings_the_server_reports():
    assert [cap.value for cap in ALL_CAPABILITIES] == [
        "apps.list",
        "apps.open",
        "apps.close",
        "url.open",
        "media.play_pause",
        "media.next",
        "media.previous",
        "media.status",
        "windows.list",
        "windows.focus",
    ]


# --- success envelope ----------------------------------------------------------


def test_ok_envelope_is_a_flat_json_object_marked_ok():
    payload = ok(Capability.APPS_LIST, count=2, apps=[{"id": "x"}])
    assert payload["ok"] is True
    assert payload["capability"] == "apps.list"
    assert json.loads(json.dumps(payload)) == payload


def test_ok_without_a_capability_stays_minimal():
    assert ok(done=True) == {"ok": True, "done": True}


# --- failure envelope ----------------------------------------------------------


def every_error() -> list[DriverError]:
    """One instance of each failure mode a driver can report."""
    return [
        UnsupportedCapabilityError("no window API here"),
        DependencyMissingError(
            "playerctl is not installed",
            details={"dependency": "playerctl"},
        ),
        NotFoundError("nothing matched", details={"candidates": ["firefox", "firefox-esr"]}),
        CommandFailedError("playerctl exited 1", details={"returncode": 1}),
        CommandTimeoutError("playerctl timed out", details={"timeout": 10.0}),
        InvalidInputError("file:// URLs are not allowed", details={"url": "file:///etc/passwd"}),
    ]


def test_every_error_carries_the_same_machine_readable_shape():
    for error in every_error():
        body = error.to_dict()
        assert body["ok"] is False
        assert body["error"]["type"], type(error).__name__
        assert body["error"]["message"]
        assert isinstance(body["error"].get("details", {}), dict)
        assert json.loads(json.dumps(body)) == body


def test_error_codes_are_stable_strings_agents_can_branch_on():
    codes = {error.code for error in every_error()}
    assert codes == {
        "unsupported",
        "dependency_missing",
        "not_found",
        "command_failed",
        "timeout",
        "invalid_input",
    }
    assert {error.to_dict()["error"]["type"] for error in every_error()} == codes


def test_a_missing_dependency_and_an_unsupported_capability_are_different_verdicts():
    """One is fixable by the operator, the other never is; the agent must tell them apart."""
    assert DependencyMissingError("x").code == "dependency_missing"
    assert UnsupportedCapabilityError("x").code == "unsupported"


def test_a_hint_is_optional_but_survives_serialisation():
    error = DependencyMissingError("no playerctl", hint="install the 'playerctl' package")
    body = error.to_dict()
    assert body["error"]["hint"] == "install the 'playerctl' package"
    assert str(error) == "no playerctl"  # the message is the exception's str()
    assert DependencyMissingError("no hint here").to_dict()["error"].get("hint") is None


# --- capability matrix ---------------------------------------------------------


@pytest.mark.parametrize("name", sorted(DRIVERS))
def test_capability_matrix_is_json_and_covers_every_capability(name: str) -> None:
    matrix = bare(name).capability_matrix()
    assert json.loads(json.dumps(matrix)) == matrix
    assert matrix["ok"] is True
    assert matrix["driver"] == name
    assert set(matrix["capabilities"]) == {cap.value for cap in ALL_CAPABILITIES}
    for value, record in matrix["capabilities"].items():
        assert record["tool"], value
        assert record["reason"], value


@pytest.mark.parametrize("name", sorted(DRIVERS))
def test_an_absent_toolchain_degrades_rather_than_raising(name: str) -> None:
    """With nothing installed, building a driver and asking what it can do still works."""
    matrix = bare(name).capability_matrix()
    assert matrix["summary"]["total"] == len(ALL_CAPABILITIES)
    for value, record in matrix["capabilities"].items():
        if value.startswith(("media.", "windows.", "url.")):
            assert record["supported"] is False, f"{name}.{value} claims to work with no tools"
    assert matrix["summary"]["supported"] <= 3  # file-based app discovery needs no CLI


@pytest.mark.parametrize("name", sorted(DRIVERS))
def test_support_verdicts_use_only_the_documented_codes(name: str) -> None:
    driver = DRIVERS[name](environ={}, which_fn=lambda tool: None)
    for capability, support in driver.support().items():
        assert isinstance(support, Support)
        assert support.reason, capability
        if support.supported:
            assert support.code == "unsupported"  # the code only matters on failure
        else:
            assert support.code in {"unsupported", "dependency_missing"}, capability


def test_unsupported_capability_is_refused_before_anything_executes(monkeypatch) -> None:
    """A capability probed as unavailable must never reach a subprocess."""
    import iot_mcp.drivers.linux as linux_module

    def exploding(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("a subprocess was launched for an unsupported capability")

    monkeypatch.setattr(linux_module, "run", exploding)
    driver = LinuxDriver(environ={}, which_fn=lambda tool: None)
    with pytest.raises(DriverError):
        asyncio.run(driver.media_next())


def test_matrix_summary_counts_match_the_records() -> None:
    driver = LinuxDriver(
        environ={"PATH": "/usr/bin"},
        which_fn=lambda tool: "/usr/bin/playerctl" if tool == "playerctl" else None,
    )
    matrix = driver.capability_matrix()
    supported = {value for value, record in matrix["capabilities"].items() if record["supported"]}
    assert matrix["summary"]["supported"] == len(supported)
    assert {"media.play_pause", "media.status"} <= supported


# --- NullDriver ----------------------------------------------------------------


def test_null_driver_explains_itself_and_supports_nothing() -> None:
    driver = NullDriver("haiku is not supported")
    matrix = driver.capability_matrix()
    assert matrix["driver"] == "null"
    assert matrix["summary"]["supported"] == 0


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("list_apps", {}),
        ("open_app", {"name": "x"}),
        ("close_app", {"name": "x"}),
        ("open_url", {"url": "https://example.com"}),
        ("media_play_pause", {}),
        ("media_next", {}),
        ("media_previous", {}),
        ("media_status", {}),
        ("list_windows", {}),
        ("focus_window", {"app_or_title": "x"}),
    ],
)
def test_null_driver_refuses_every_action_with_a_typed_error(
    method: str, kwargs: dict[str, Any]
) -> None:
    driver = NullDriver("no backend")
    with pytest.raises(DriverError) as excinfo:
        asyncio.run(getattr(driver, method)(**kwargs))
    assert excinfo.value.to_dict()["ok"] is False
    assert excinfo.value.to_dict()["error"]["message"]


# --- the ABC is a complete contract -------------------------------------------


@pytest.mark.parametrize("name", sorted(DRIVERS))
def test_drivers_implement_every_action_as_a_coroutine(name: str) -> None:
    driver_cls = DRIVERS[name]
    for tool in TOOLS:
        if tool == "list_actions":
            continue
        method = getattr(driver_cls, tool, None)
        assert method is not None, f"{name} does not implement {tool}"
        assert inspect.iscoroutinefunction(method), f"{name}.{tool} must be async"


def test_describe_names_the_backend_so_logs_are_unambiguous() -> None:
    assert "linux" in LinuxDriver(environ={}, which_fn=lambda tool: None).describe().lower()
