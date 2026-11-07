"""Browser tier control tools: registration, approval gates, and request-only behavior."""

from __future__ import annotations

import json
from typing import Any

import pytest

from iot_mcp import browser as browser_module
from iot_mcp.addons import DEFAULT_BROWSER_ARGS, AddonManager, fingerprint_argv
from iot_mcp.app import SERVER_INSTRUCTIONS, build_server
from iot_mcp.browser import (
    BROWSER_ADDON,
    BROWSER_INSTRUCTIONS,
    CONTROL_TOOLS,
    build_browser_controls,
)
from iot_mcp.drivers.base import NullDriver

DRIVER_TOOLS = {
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
}


class FakeBridge:
    """Duck-typed bridge sufficient for control-tool tests (never touches a process)."""

    def __init__(
        self,
        entry: Any,
        *,
        prefix: str = "browser",
        request_timeout: float = 60.0,
        startup_timeout: float = 30.0,
    ) -> None:
        self.entry = entry
        self.prefix = prefix
        self.request_timeout = request_timeout
        self.startup_timeout = startup_timeout
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.start_calls = 0
        self.stop_calls = 0
        self._state = "idle"
        self._running = False
        self._tools: tuple[Any, ...] = ()

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
        return {
            "addon": self.entry.name,
            "state": self._state,
            "tools": [getattr(tool, "name", None) for tool in self._tools],
            "last_error": None,
            "launch": list(self.entry.argv),
        }

    async def start(self, *, wait: bool = True) -> None:
        self.start_calls += 1
        self._state = "ready"
        self._running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self._state = "stopped"
        self._running = False

    async def call_upstream(self, upstream_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((upstream_name, dict(arguments)))
        return {"ok": True, "content": [{"type": "text", "text": upstream_name}]}


async def _tools(server: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in await server.list_tools()}


async def _call(server: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    return await server.call_tool(name, arguments or {})


def _payload(result: Any) -> dict[str, Any]:
    assert result.content and result.content[0].type == "text"
    return json.loads(result.content[0].text)


def _manager(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, approve: bool = False
) -> AddonManager:
    monkeypatch.setattr("iot_mcp.addons.shutil.which", lambda program: f"/usr/bin/{program}")
    manager = AddonManager(tmp_path)
    if approve:
        manager.approve(
            BROWSER_ADDON,
            argv=list(DEFAULT_BROWSER_ARGS),
            package="@playwright/mcp@latest",
            approver="operator@host",
        )
    return manager


async def test_disabled_browser_tier_adds_no_browser_tools() -> None:
    server = build_server(NullDriver("x"), browser=None)
    assert set(await _tools(server)) == DRIVER_TOOLS
    assert "browser" not in (server.instructions or "").lower()


async def test_enabled_unapproved_server_registers_only_control_tools(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = build_browser_controls(enabled=True, manager=_manager(tmp_path, monkeypatch))
    assert controls is not None
    server = build_server(NullDriver("x"), browser=controls)

    assert set(await _tools(server)) == DRIVER_TOOLS | set(CONTROL_TOOLS)
    assert "operator-approved" in (server.instructions or "")


async def test_server_instructions_explain_request_install_cannot_approve(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = build_browser_controls(enabled=True, manager=_manager(tmp_path, monkeypatch))
    server = build_server(NullDriver("x"), browser=controls)

    assert BROWSER_INSTRUCTIONS in (server.instructions or "")
    assert SERVER_INSTRUCTIONS in (server.instructions or "")


async def test_status_reports_not_approved_and_human_command(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = build_browser_controls(enabled=True, manager=_manager(tmp_path, monkeypatch))
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(await _call(server, "browser_status"))
    assert payload["ok"] is True
    assert payload["enabled"] is True
    assert payload["approved"] is False
    assert payload["state"] == "not_approved"
    assert "iot-mcp-addons install browser" in payload["refusal"]
    assert payload["launch"] == list(DEFAULT_BROWSER_ARGS)
    assert payload["fingerprint"] == fingerprint_argv(DEFAULT_BROWSER_ARGS)


async def test_install_status_shows_exact_approval_that_would_be_written(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = build_browser_controls(enabled=True, manager=_manager(tmp_path, monkeypatch))
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(await _call(server, "browser_install_status"))
    assert payload["ok"] is True
    assert payload["installed"] is False
    assert payload["agent_can_approve"] is False
    assert payload["install_command"] == "iot-mcp-addons install browser"
    assert payload["launch"] == list(DEFAULT_BROWSER_ARGS)


async def test_request_install_files_note_but_installs_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    controls = build_browser_controls(enabled=True, manager=manager)
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(
        await _call(
            server,
            "browser_request_install",
            {"reason": "need to read web docs", "requested_by": "test-client"},
        )
    )

    assert payload["ok"] is True
    assert payload["installed"] is False
    assert payload["approved"] is False
    assert payload["agent_can_approve"] is False
    assert manager.pending_requests(BROWSER_ADDON)[0]["requested_by"] == "test-client"


async def test_request_install_requires_a_reason(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = build_browser_controls(enabled=True, manager=_manager(tmp_path, monkeypatch))
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(await _call(server, "browser_request_install", {"reason": "   "}))
    assert payload["ok"] is False
    assert payload["error"]["type"] == "invalid_input"


async def test_browser_start_refuses_without_human_approval(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = build_browser_controls(enabled=True, manager=_manager(tmp_path, monkeypatch))
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(await _call(server, "browser_start"))
    assert payload["ok"] is False
    assert payload["error"]["type"] == "dependency_missing"
    assert "iot-mcp-addons install" in payload["error"]["hint"]


async def test_invalid_approval_record_is_reported_as_reapproval_needed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    manager.approve(BROWSER_ADDON, approver="operator@host")
    file = manager.entries_dir / "browser.json"
    data = json.loads(file.read_text(encoding="utf-8"))
    data["fingerprint"] = "sha256:tampered"
    file.write_text(json.dumps(data), encoding="utf-8")

    controls = build_browser_controls(enabled=True, manager=manager)
    server = build_server(NullDriver("x"), browser=controls)
    status = _payload(await _call(server, "browser_status"))

    assert status["approved"] is False
    assert status["state"] == "not_approved"
    assert "fingerprint does not match" in status["refusal"]


async def test_requested_argv_mismatch_is_refused_without_starting_anything(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path, monkeypatch, approve=True)
    controls = build_browser_controls(
        enabled=True,
        argv=["npx", "-y", "evil-package", "--headless"],
        manager=manager,
    )

    assert controls is not None
    assert controls.bridge is None
    assert controls.entry is None
    assert "differs from the approved launch" in (controls.refusal or "")


async def test_approved_controls_start_and_register_page_tools(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path, monkeypatch, approve=True)

    class OneToolBridge(FakeBridge):
        @property
        def tools(self) -> tuple[Any, ...]:
            return (_FakeTool("browser_navigate", "navigate"),)

    monkeypatch.setattr(browser_module, "AddonBridge", OneToolBridge)
    controls = build_browser_controls(enabled=True, manager=manager)
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(await _call(server, "browser_start"))
    assert payload["ok"] is True
    assert payload["started"] is True
    assert payload["tools"] == ["browser_navigate"]
    assert "browser_navigate" in await _tools(server)


async def test_approved_controls_stop_deregisters_page_tools(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path, monkeypatch, approve=True)

    class OneToolBridge(FakeBridge):
        @property
        def tools(self) -> tuple[Any, ...]:
            return (_FakeTool("browser_navigate", "navigate"),)

    monkeypatch.setattr(browser_module, "AddonBridge", OneToolBridge)
    controls = build_browser_controls(enabled=True, manager=manager)
    server = build_server(NullDriver("x"), browser=controls)

    await _call(server, "browser_start")
    payload = _payload(await _call(server, "browser_stop"))

    assert payload["ok"] is True
    assert payload["tools_removed"] == ["browser_navigate"]
    assert set(await _tools(server)) == DRIVER_TOOLS | set(CONTROL_TOOLS)


class _FakeTool:
    def __init__(self, name: str, upstream_name: str, schema: dict[str, Any] | None = None) -> None:
        self.name = name
        self.upstream_name = upstream_name
        self.description = f"Upstream description for {upstream_name}."
        self.input_schema = schema or {"type": "object", "properties": {}}
        self.annotations = None
