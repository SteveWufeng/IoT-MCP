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


# --- narration on the screen (browser_annotate) ------------------------------------------------


class _ReportingBridge(FakeBridge):
    """A bridge whose page evaluation answers with the injected script's own report."""

    REPORT = {
        "url": "https://example.org/pricing",
        "title": "Pricing",
        "note": "the enterprise row is the only one without an API",
        "tone": "info",
        "highlighted": [{"tag": "tr.enterprise", "text": "Enterprise"}],
        "matchedAll": 1,
        "invalidSelector": None,
        "holdSeconds": None,
        "autoExpires": False,
        "cardsOnScreen": 1,
    }

    async def call_upstream(self, upstream_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await super().call_upstream(upstream_name, arguments)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"### Result\n{json.dumps(self.REPORT)}\n### Ran Playwright code",
                }
            ]
        }


def _running_controls(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    bridge_class: type[FakeBridge] = _ReportingBridge,
) -> Any:
    """Approved controls with a (fake) page-evaluation-capable browser already started."""
    monkeypatch.setattr(browser_module, "AddonBridge", bridge_class)
    controls = build_browser_controls(
        enabled=True, manager=_manager(tmp_path, monkeypatch, approve=True)
    )
    assert controls is not None
    bridge = controls.bridge
    bridge._state = "ready"
    bridge._running = True
    return controls


async def test_annotate_is_a_control_tool_registered_before_any_approval(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = build_browser_controls(enabled=True, manager=_manager(tmp_path, monkeypatch))
    server = build_server(NullDriver("x"), browser=controls)
    tools = await _tools(server)

    assert {"browser_annotate", "browser_annotate_clear"} <= set(CONTROL_TOOLS)
    assert {"browser_annotate", "browser_annotate_clear"} <= set(tools)
    assert "browser_annotate" in _payload(await _call(server, "browser_status"))["controls"]


async def test_annotate_is_described_as_narration_not_privilege(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = build_browser_controls(enabled=True, manager=_manager(tmp_path, monkeypatch))
    server = build_server(NullDriver("x"), browser=controls)
    tool = (await _tools(server))["browser_annotate"]
    properties = tool.input_schema.get("properties", {})
    doc = (
        tool.description
        + " "
        + " ".join(str((properties.get(name) or {}).get("description", "")) for name in properties)
    )

    assert "human" in tool.description
    assert "grants no capability" in tool.description
    assert "browser_annotate_clear" in BROWSER_INSTRUCTIONS
    # The note is described as displayed-and-untrusted text, never as something parsed.
    assert "never trusted" in doc


async def test_annotate_needs_a_running_browser(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path, monkeypatch, approve=True)
    monkeypatch.setattr(browser_module, "AddonBridge", FakeBridge)
    controls = build_browser_controls(enabled=True, manager=manager)
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(await _call(server, "browser_annotate", {"note": "looking at the menu"}))

    assert payload["ok"] is False
    assert payload["error"]["type"] == "dependency_missing"
    assert payload["error"]["capability"] == "browser.annotate"
    assert "browser_start" in payload["error"]["hint"]


async def test_clear_needs_a_running_browser_and_says_so_with_its_own_capability(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path, monkeypatch, approve=True)
    monkeypatch.setattr(browser_module, "AddonBridge", FakeBridge)
    controls = build_browser_controls(enabled=True, manager=manager)
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(await _call(server, "browser_annotate_clear"))

    assert payload["ok"] is False
    assert payload["error"]["capability"] == "browser.annotate_clear"


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"note": "   "}, "empty"),
        ({"note": "both ways", "selector": "#a", "text": "a"}, "not both"),
        ({"note": "loud", "tone": "confetti"}, "tone"),
    ],
)
async def test_undrawable_requests_are_invalid_input_not_a_page_call(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], reason: str
) -> None:
    controls = _running_controls(tmp_path, monkeypatch)
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(await _call(server, "browser_annotate", arguments))

    assert payload["ok"] is False
    assert payload["error"]["type"] == "invalid_input"
    assert reason in payload["error"]["message"]
    assert controls.bridge.calls == []


async def test_annotate_draws_by_evaluating_the_generated_script(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = _running_controls(tmp_path, monkeypatch)
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(
        await _call(
            server,
            "browser_annotate",
            {"note": "the enterprise row has no API", "text": "Enterprise", "tone": "warn"},
        )
    )

    assert payload["ok"] is True
    assert payload["annotated"] is True
    assert payload["cleared"] is False
    assert payload["page"]["highlighted"] == [{"tag": "tr.enterprise", "text": "Enterprise"}]

    name, arguments = controls.bridge.calls[-1]
    assert name == "browser_evaluate"
    script = arguments["function"]
    assert script.lstrip().startswith("async () =>")
    assert '"note": "the enterprise row has no API"' in script
    assert '"tone": "warn"' in script


async def test_annotate_clear_says_clear_and_needs_no_note(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = _running_controls(tmp_path, monkeypatch)
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(await _call(server, "browser_annotate_clear"))

    assert payload["ok"] is True
    assert payload["cleared"] is True
    assert payload["annotated"] is False
    assert '"clear": true' in controls.bridge.calls[-1][1]["function"]


async def test_annotate_survives_an_unparsable_page_reply(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = _running_controls(tmp_path, monkeypatch, bridge_class=FakeBridge)
    server = build_server(NullDriver("x"), browser=controls)

    payload = _payload(await _call(server, "browser_annotate", {"note": "hello"}))

    assert payload["ok"] is True
    assert payload["page"] is None
    assert payload["raw"]["content"][0]["text"] == "browser_evaluate"


async def test_upstream_cannot_shadow_the_annotation_tools(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page tool that happens to be named browser_annotate is not registered over ours."""
    manager = _manager(tmp_path, monkeypatch, approve=True)

    class ShadowingBridge(FakeBridge):
        @property
        def tools(self) -> tuple[Any, ...]:
            return (
                _FakeTool("browser_annotate", "annotate"),
                _FakeTool("browser_navigate", "navigate"),
            )

    monkeypatch.setattr(browser_module, "AddonBridge", ShadowingBridge)
    controls = build_browser_controls(enabled=True, manager=manager)
    server = build_server(NullDriver("x"), browser=controls)

    started = _payload(await _call(server, "browser_start"))

    assert "browser_navigate" in started["tools"]
    assert "browser_annotate" not in started["tools"]
    assert set(await _tools(server)) == DRIVER_TOOLS | set(CONTROL_TOOLS) | {"browser_navigate"}
