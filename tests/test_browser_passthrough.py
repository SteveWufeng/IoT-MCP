"""Pass-through browser tools: upstream schemas stay authoritative, arguments stay verbatim."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from iot_mcp.addons import DEFAULT_BROWSER_ARGS, AddonEntry, AddonManager, fingerprint_argv
from iot_mcp.app import build_server
from iot_mcp.browser import BrowserControls, _preparse
from iot_mcp.drivers.base import NullDriver


class FakeBridge:
    def __init__(self, tools: list[Any], *, fail_call: bool = False) -> None:
        self._configured = tuple(tools)
        self._tools: tuple[Any, ...] = ()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_call = fail_call
        self._state = "idle"
        self._running = False
        self.stop_calls = 0

    @property
    def state(self) -> str:
        return self._state

    @property
    def running(self) -> bool:
        return self._running

    @property
    def tools(self) -> tuple[Any, ...]:
        return self._tools

    def status(self) -> dict[str, Any]:
        return {"addon": "browser", "state": self._state, "tools": [t.name for t in self._tools]}

    async def start(self, *, wait: bool = True) -> None:
        self._tools = self._configured
        self._state = "ready"
        self._running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self._state = "stopped"
        self._running = False

    async def call_upstream(self, upstream_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((upstream_name, dict(arguments)))
        if self.fail_call:
            return {
                "ok": False,
                "error": {"type": "upstream_tool_error", "message": "page exploded"},
                "content": [{"type": "text", "text": "page exploded"}],
            }
        return {
            "ok": True,
            "content": [{"type": "text", "text": f"called {upstream_name}"}],
            "structuredContent": {"name": upstream_name, "arguments": arguments},
        }


class FakeTool:
    def __init__(
        self,
        name: str,
        upstream_name: str,
        schema: dict[str, Any] | None = None,
        *,
        description: str | None = None,
    ) -> None:
        self.name = name
        self.upstream_name = upstream_name
        self.description = description or f"Upstream description for {upstream_name}."
        self.input_schema = schema or {"type": "object", "properties": {}}
        self.annotations = None


def _controls(tmp_path: Path, bridge: FakeBridge) -> BrowserControls:
    entry = AddonEntry(
        name="browser",
        argv=DEFAULT_BROWSER_ARGS,
        fingerprint=fingerprint_argv(DEFAULT_BROWSER_ARGS),
        approved_at="now",
        approved_by="operator@host",
        package="@playwright/mcp@latest",
        headless=True,
    )
    return BrowserControls(manager=AddonManager(tmp_path), entry=entry, bridge=bridge)


async def _tools(server: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in await server.list_tools()}


async def _call(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await server.call_tool(name, arguments)
    assert result.content and result.content[0].type == "text"
    return json.loads(result.content[0].text)


def _schema(tool: Any) -> dict[str, Any]:
    return getattr(tool, "input_schema", None) or tool.inputSchema or {}


async def test_started_page_tools_are_registered_under_upstream_names_and_schemas(
    tmp_path: Path,
) -> None:
    tool = FakeTool(
        "browser_navigate",
        "navigate",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}, "timeout": {"type": "number"}},
            "required": ["url"],
        },
        description="Navigate the browser to a URL.",
    )
    bridge = FakeBridge([tool])
    controls = _controls(tmp_path, bridge)
    server = build_server(NullDriver("x"), browser=controls)

    assert await controls.start() == ("browser_navigate",)
    tools = await _tools(server)

    assert "browser_navigate" in tools
    assert tools["browser_navigate"].description == "Navigate the browser to a URL."
    assert _schema(tools["browser_navigate"]) == tool.input_schema


async def test_page_tool_arguments_are_forwarded_verbatim(tmp_path: Path) -> None:
    tool = FakeTool(
        "browser_click",
        "click",
        {
            "type": "object",
            "properties": {"element": {"type": "string"}, "button": {"type": "string"}},
        },
    )
    bridge = FakeBridge([tool])
    controls = _controls(tmp_path, bridge)
    server = build_server(NullDriver("x"), browser=controls)
    await controls.start()

    payload = await _call(server, "browser_click", {"element": "submit", "button": "left"})
    assert payload["ok"] is True
    assert bridge.calls == [("click", {"element": "submit", "button": "left"})]


async def test_page_tool_preserves_upstream_error_envelope(tmp_path: Path) -> None:
    bridge = FakeBridge([FakeTool("browser_navigate", "navigate")], fail_call=True)
    controls = _controls(tmp_path, bridge)
    server = build_server(NullDriver("x"), browser=controls)
    await controls.start()

    payload = await _call(server, "browser_navigate", {"url": "https://example.test"})
    assert payload["ok"] is False
    assert payload["error"]["type"] == "upstream_tool_error"
    assert payload["content"][0]["text"] == "page exploded"


async def test_page_tool_collisions_with_control_and_driver_tools_are_skipped(
    tmp_path: Path,
) -> None:
    bridge = FakeBridge(
        [
            FakeTool("browser_status", "status"),
            FakeTool("list_apps", "list_apps"),
            FakeTool("browser_navigate", "navigate"),
        ]
    )
    controls = _controls(tmp_path, bridge)
    server = build_server(NullDriver("x"), browser=controls)

    added = await controls.start()
    tools = await _tools(server)

    assert added == ("browser_navigate",)
    assert controls.skipped == ("browser_status", "list_apps")
    assert "Upstream description for status." not in tools["browser_status"].description
    assert "browser_navigate" in tools


async def test_page_tools_are_unregistered_when_browser_stops(tmp_path: Path) -> None:
    bridge = FakeBridge([FakeTool("browser_navigate", "navigate")])
    controls = _controls(tmp_path, bridge)
    server = build_server(NullDriver("x"), browser=controls)
    await controls.start()

    removed = await controls.stop()
    tools = await _tools(server)

    assert removed == ("browser_navigate",)
    assert bridge.stop_calls == 1
    assert "browser_navigate" not in tools
    assert "browser_status" in tools


def test_preparse_decodes_json_containers_only_where_schema_declares_them() -> None:
    schema = {
        "type": "object",
        "properties": {
            "headers": {"type": "object"},
            "flags": {"type": "array"},
            "label": {"type": "string"},
            "maybe": {"anyOf": [{"type": "object"}, {"type": "string"}]},
        },
    }
    arguments = {
        "headers": '{"a": 1}',
        "flags": "[1, 2]",
        "label": '{"keep": "string"}',
        "maybe": '{"x": true}',
        "extra": "[not json",
    }

    parsed = _preparse(arguments, schema)

    assert parsed["headers"] == {"a": 1}
    assert parsed["flags"] == [1, 2]
    assert parsed["label"] == '{"keep": "string"}'
    assert parsed["maybe"] == {"x": True}
    assert parsed["extra"] == "[not json"


def test_preparse_passes_non_dict_arguments_through() -> None:
    assert _preparse({}, {"type": "object"}) == {}
    assert _preparse(None, {"type": "object"}) == {}
