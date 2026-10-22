"""The MCP surface: what an agent can see and call, and what comes back.

These tests drive the real SDK objects, so they fail if a tool disappears, a description
goes missing, an annotation lies about safety, or an error stops being reported as data.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import iot_mcp.drivers.linux as linux_module
from iot_mcp.app import SERVER_INSTRUCTIONS, build_server
from iot_mcp.capabilities import ALL_CAPABILITIES
from iot_mcp.drivers.base import AutomationDriver, NullDriver, Support, ok
from iot_mcp.errors import DependencyMissingError, InvalidInputError

TOOL_NAMES = [
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
]

READ_ONLY = {"list_actions", "list_apps", "media_status", "list_windows"}
#: Tools that change state but are not, by themselves, disruptive.
STATE_CHANGING = {"open_app", "media_play_pause", "media_next", "media_previous"}
#: Tools a cautious client should confirm first: they take work away from the user.
DESTRUCTIVE = {"close_app", "focus_window"}


class RecordingDriver(AutomationDriver):
    """A backend that reports every capability as available and records what was called."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _probe(self) -> dict[Any, Support]:
        return {capability: Support.yes("test double") for capability in ALL_CAPABILITIES}

    async def list_apps(self) -> dict[str, Any]:
        self.calls.append("list_apps")
        return ok("apps.list", count=1, apps=[{"id": "firefox", "name": "Firefox"}])

    async def open_app(self, name: str) -> dict[str, Any]:
        self.calls.append(f"open_app:{name}")
        return ok("apps.open", app=name, launched_argv=["/usr/bin/" + name])

    async def close_app(self, name: str) -> dict[str, Any]:
        self.calls.append(f"close_app:{name}")
        raise DependencyMissingError("nothing to close", hint="start something first")

    async def open_url(self, url: str) -> dict[str, Any]:
        self.calls.append(f"open_url:{url}")
        return ok("url.open", url=url)

    async def media_play_pause(self) -> dict[str, Any]:
        self.calls.append("media_play_pause")
        return ok("media.play_pause", state="toggle-sent")

    async def media_next(self) -> dict[str, Any]:
        self.calls.append("media_next")
        return ok("media.next", state="next-sent")

    async def media_previous(self) -> dict[str, Any]:
        self.calls.append("media_previous")
        return ok("media.previous", state="previous-sent")

    async def media_status(self) -> dict[str, Any]:
        self.calls.append("media_status")
        return ok("media.status", playing=None, players=[])

    async def list_windows(self) -> dict[str, Any]:
        self.calls.append("list_windows")
        return ok("windows.list", count=0, windows=[])

    async def focus_window(self, app_or_title: str) -> dict[str, Any]:
        self.calls.append(f"focus_window:{app_or_title}")
        return ok("windows.focus", window={"title": app_or_title}, already_focused=False)


@pytest.fixture
def recording() -> tuple[Any, Any]:
    """A server wired to :class:`RecordingDriver`."""
    driver = RecordingDriver()
    server = build_server(driver, name="test-server")
    return server, driver


async def _tools(server: Any) -> dict[str, Any]:
    listed = await server.list_tools()
    return {tool.name: tool for tool in listed}


async def _call(server: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    return await server.call_tool(name, arguments or {})


def content_payload(result: Any) -> dict[str, Any]:
    """The JSON object an agent actually receives from a tool call."""
    assert result.content and result.content[0].type == "text"
    return json.loads(result.content[0].text)


# --- the advertised tool set ---------------------------------------------------


async def test_exactly_the_mvp_tool_set_is_registered() -> None:
    tools = await _tools(build_server(NullDriver("x")))
    assert sorted(tools) == sorted(TOOL_NAMES)


async def test_every_tool_documentes_itself_for_the_agent() -> None:
    tools = await _tools(build_server(NullDriver("x")))
    for name, tool in tools.items():
        assert tool.description, f"{name} has no description"
        assert len(tool.description) > 40, f"{name}'s description is too thin to act on"
        assert tool.annotations is not None and tool.annotations.title, name


async def test_server_instructions_send_the_agent_to_list_actions_first() -> None:
    server = build_server(NullDriver("x"))
    assert "list_actions" in (server.instructions or "")
    assert "list_actions" in SERVER_INSTRUCTIONS


@pytest.mark.parametrize("name", sorted(READ_ONLY))
async def test_observing_tools_declare_themselves_read_only(name: str) -> None:
    tools = await _tools(build_server(NullDriver("x")))
    annotations = tools[name].annotations
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True
    assert annotations.open_world_hint is False


@pytest.mark.parametrize("name", sorted(STATE_CHANGING | DESTRUCTIVE))
async def test_state_changing_tools_do_not_claim_to_be_read_only(name: str) -> None:
    tools = await _tools(build_server(NullDriver("x")))
    assert tools[name].annotations.read_only_hint is False
    assert tools[name].annotations.idempotent_hint is False


@pytest.mark.parametrize("name", sorted(DESTRUCTIVE))
async def test_close_and_focus_are_flagged_destructive(name: str) -> None:
    """A client that gates dangerous calls needs this hint, not a text scan."""
    tools = await _tools(build_server(NullDriver("x")))
    assert tools[name].annotations.destructive_hint is True


async def test_opening_a_url_is_flagged_as_reaching_outside_the_machine() -> None:
    tools = await _tools(build_server(NullDriver("x")))
    assert tools["open_url"].annotations.open_world_hint is True
    assert tools["open_app"].annotations.open_world_hint is False


async def test_input_schemas_match_the_documented_arguments() -> None:
    tools = await _tools(build_server(NullDriver("x")))
    assert tools["open_app"].input_schema["required"] == ["name"]
    assert tools["open_url"].input_schema["required"] == ["url"]
    assert tools["focus_window"].input_schema["required"] == ["app_or_title"]
    for name in ("list_actions", "list_apps", "media_status", "list_windows", "media_play_pause"):
        assert tools[name].input_schema.get("properties", {}) == {}, name


async def test_the_server_can_be_built_without_naming_a_driver() -> None:
    """``build_server()`` picks this platform's backend and still serves 11 tools."""
    tools = await _tools(build_server())
    assert len(tools) == len(TOOL_NAMES)


# --- results and errors --------------------------------------------------------


async def test_a_successful_call_returns_the_drivers_envelope(recording) -> None:
    server, driver = recording
    result = await _call(server, "list_apps")
    payload = content_payload(result)
    assert payload["ok"] is True and payload["apps"][0]["id"] == "firefox"
    assert driver.calls == ["list_apps"]


async def test_a_typed_driver_failure_comes_back_as_data_not_a_protocol_error(recording) -> None:
    """The agent must be able to read the failure; raising would only say "tool failed"."""
    server, _ = recording
    result = await _call(server, "close_app", {"name": "firefox"})
    payload = content_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "dependency_missing"
    assert payload["error"]["hint"] == "start something first"


async def test_a_string_argument_reaches_the_driver_verbatim(recording) -> None:
    server, driver = recording
    await _call(server, "focus_window", {"app_or_title": "Firefox — Private Browsing"})
    assert driver.calls == ["focus_window:Firefox — Private Browsing"]


async def test_unknown_tools_are_refused_by_the_protocol(recording) -> None:
    server, _ = recording
    with pytest.raises(Exception):
        await _call(server, "delete_everything", {})


async def test_missing_required_arguments_are_refused(recording) -> None:
    server, driver = recording
    with pytest.raises(Exception):
        await _call(server, "open_app", {})
    assert driver.calls == [], "the driver must not run when validation fails"


async def test_structured_content_mirrors_the_json_payload(recording) -> None:
    server, _ = recording
    result = await _call(server, "media_status")
    assert result.structured_content == content_payload(result)


async def test_a_failure_carries_its_structured_content_too(recording) -> None:
    server, _ = recording
    result = await _call(server, "close_app", {"name": "nothing"})
    assert result.structured_content["ok"] is False
    assert result.is_error is False, "a capability failure is data, not a protocol error"


async def test_an_unexpected_driver_bug_becomes_an_internal_error_envelope() -> None:
    from iot_mcp.app import _invoke

    def explode() -> dict[str, Any]:
        raise RuntimeError("bug in driver")

    payload = await _invoke(explode)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "internal_error"
    assert "bug in driver" in payload["error"]["message"]
    assert payload["error"]["hint"]


async def test_invoke_awaits_async_and_sync_paths_alike() -> None:
    from iot_mcp.app import _invoke

    async def async_call() -> dict[str, Any]:
        return ok(done=True)

    assert await _invoke(async_call) == {"ok": True, "done": True}
    assert await _invoke(lambda: ok(sync=True)) == {"ok": True, "sync": True}


async def test_invalid_input_from_the_driver_is_reported_as_invalid_input() -> None:
    from iot_mcp.app import _invoke

    def reject() -> dict[str, Any]:
        raise InvalidInputError("bad url", hint="use https")

    payload = await _invoke(reject)
    assert payload["error"]["type"] == "invalid_input"


async def test_a_null_server_still_answers_list_actions() -> None:
    """Even with no backend, the agent gets a machine-readable capability report."""
    server = build_server(NullDriver("haiku is not supported"))
    payload = content_payload(await _call(server, "list_actions"))
    assert payload["ok"] is True
    assert payload["driver"] == "null"
    assert payload["summary"]["supported"] == 0


async def test_the_real_linux_backend_answers_through_the_same_path(fake_run) -> None:
    """Nothing about the MCP layer depends on the test doubles used elsewhere."""
    fake = fake_run(linux_module)
    from iot_mcp.drivers.linux import LinuxDriver

    driver = LinuxDriver(
        environ={
            "PATH": "/none",
            "HOME": "/none",
            "XDG_DATA_HOME": "/none",
            "XDG_DATA_DIRS": "/none",
        },
        which_fn=lambda tool: None,
    )
    server = build_server(driver, name="linux-test")
    payload = content_payload(await _call(server, "list_actions"))
    assert payload["driver"] == "linux"
    assert payload["summary"]["total"] == 10
    assert fake.calls == [], "list_actions must not spawn anything"
