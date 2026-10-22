"""Client plumbing: what it reports when a server answers, and when it does not.

No real sockets here: the session is a double, and discovery is patched. The end-to-end
loopback run (real server, real HTTP) is covered by ``tests/test_server_e2e.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import types
from typing import Any

import pytest

from iot_mcp.client.cli import _dump, _print_endpoints, build_parser
from iot_mcp.client.connect import (
    _payload_from,
    call_tool,
    describe_exception,
    list_tools,
    pick_endpoint,
    resolve_url,
)
from iot_mcp.discovery import DiscoveredEndpoint

#: ``iot_mcp.client`` re-exports the ``connect()`` helper, which shadows the submodule of the
#: same name: ``iot_mcp.client.connect`` is the *function*. Fetch the module explicitly when a
#: test needs to patch its internals.
connect_module = importlib.import_module("iot_mcp.client.connect")


def result(
    *,
    structured: Any = None,
    text: str | None = None,
    is_error: bool = False,
) -> Any:
    """A stand-in for ``mcp.types.CallToolResult``."""
    content = [] if text is None else [types.SimpleNamespace(type="text", text=text)]
    return types.SimpleNamespace(content=content, structured_content=structured, is_error=is_error)


class FakeSession:
    """A client session that replays scripted results and records requests."""

    def __init__(
        self,
        results: dict[str, Any] | None = None,
        raises: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self.results = results or {}
        self.raises = raises
        self.hang = hang
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.requests.append((name, dict(arguments or {})))
        if self.raises is not None:
            raise self.raises
        if self.hang:
            await asyncio.sleep(30)
        value = self.results.get(name)
        return value if value is not None else result(structured={"ok": True, "tool": name})

    async def list_tools(self) -> Any:
        return types.SimpleNamespace(
            tools=[
                types.SimpleNamespace(
                    name="open_app",
                    description="Launch an application.  ",
                    input_schema={"type": "object", "required": ["name"]},
                    annotations=None,
                )
            ]
        )


# --- payload extraction --------------------------------------------------------


def test_structured_content_is_preferred() -> None:
    payload = _payload_from(result(structured={"ok": True, "count": 2}, text='{"ok": false}'))
    assert payload == {"ok": True, "count": 2}


def test_text_content_is_parsed_as_json_when_there_is_no_structure() -> None:
    assert _payload_from(result(text='{"ok": true, "state": "Playing"}')) == {
        "ok": True,
        "state": "Playing",
    }


def test_plain_text_from_another_server_survives() -> None:
    """Servers that only emit text must still produce readable output."""
    assert _payload_from(result(text="just prose")) == "just prose"


def test_multiple_text_blocks_are_joined() -> None:
    multi = types.SimpleNamespace(
        content=[types.SimpleNamespace(text="line one"), types.SimpleNamespace(text="line two")],
        structured_content=None,
        is_error=False,
    )
    assert _payload_from(multi) == "line one\nline two"


def test_an_empty_result_is_none_not_an_empty_string() -> None:
    assert _payload_from(result()) is None


# --- error reporting -----------------------------------------------------------


def test_a_task_group_wrapper_is_flattened_to_the_real_cause() -> None:
    """``connection refused`` arrives wrapped in anyio boilerplate; show the useful half."""
    inner = ConnectionRefusedError("[Errno 111] Connection refused")
    outer = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    described = describe_exception(outer)
    assert "ConnectionRefusedError" in described
    assert "Errno 111" in described
    assert "TaskGroup" not in described


def test_a_cause_chain_is_reported_in_order() -> None:
    root = OSError("no route to host")
    middle = RuntimeError("transport failed")
    middle.__cause__ = root
    described = describe_exception(middle)
    assert described.index("transport failed") < described.index("no route to host")


def test_an_exception_with_no_message_still_names_its_type() -> None:
    assert describe_exception(ValueError("")) == "ValueError: ValueError"


def test_a_realistically_nested_group_reaches_the_bottom() -> None:
    exc: BaseException = ValueError("bottom")
    for _ in range(4):
        exc = ExceptionGroup("unhandled errors in a TaskGroup", [exc])
    assert "bottom" in describe_exception(exc)


def test_a_pathological_chain_is_walked_with_a_budget_not_forever() -> None:
    """The walk is bounded on purpose: a cyclic chain must not hang the CLI."""
    exc: BaseException = ValueError("bottom")
    for _ in range(60):
        exc = ExceptionGroup("unhandled errors in a TaskGroup", [exc])
    described = describe_exception(exc)
    assert isinstance(described, str) and described


# --- tool calls ----------------------------------------------------------------


async def test_a_successful_call_is_reported_as_ok() -> None:
    session = FakeSession({"media_status": result(structured={"ok": True, "playing": None})})
    outcome = await call_tool(session, "media_status")
    assert outcome.ok is True and outcome.payload["ok"] is True
    assert session.requests == [("media_status", {})]


async def test_a_capability_failure_reported_as_data_still_marks_the_result_failed() -> None:
    """Our server returns ok:false without isError; the client must not call that success."""
    failure = {"ok": False, "error": {"type": "not_found", "message": "no player"}}
    outcome = await call_tool(FakeSession({"media_next": result(structured=failure)}), "media_next")
    assert outcome.ok is False
    assert outcome.payload["error"]["type"] == "not_found"


async def test_a_protocol_level_error_is_also_failure() -> None:
    outcome = await call_tool(
        FakeSession({"open_app": result(text="boom", is_error=True)}), "open_app", {"name": "x"}
    )
    assert outcome.ok is False and outcome.raw["is_error"] is True


async def test_a_hung_server_does_not_hang_the_client() -> None:
    outcome = await call_tool(FakeSession(hang=True), "list_apps", timeout=0.1)
    assert outcome.ok is False
    assert outcome.payload["error"]["type"] == "client_timeout"
    assert (
        "0s" in outcome.payload["error"]["message"] or "0.1" in outcome.payload["error"]["message"]
    )


async def test_a_transport_failure_is_reported_not_raised() -> None:
    """The REPL must survive a dropped connection."""
    outcome = await call_tool(FakeSession(raises=ConnectionResetError("peer closed")), "list_apps")
    assert outcome.ok is False
    assert outcome.payload["error"]["type"] == "transport_error"
    assert "peer closed" in outcome.payload["error"]["message"]


async def test_arguments_are_forwarded_as_a_plain_dict() -> None:
    session = FakeSession()
    await call_tool(session, "focus_window", {"app_or_title": "Firefox"})
    assert session.requests == [("focus_window", {"app_or_title": "Firefox"})]


async def test_list_tools_normalises_the_server_catalogue() -> None:
    tools = await list_tools(FakeSession())
    assert tools[0]["name"] == "open_app"
    assert tools[0]["description"] == "Launch an application."
    assert tools[0]["input_schema"]["required"] == ["name"]
    assert tools[0]["annotations"] is None


# --- endpoint selection --------------------------------------------------------


def endpoint(instance: str, name: str = "", host: str = "192.168.0.5") -> DiscoveredEndpoint:
    return DiscoveredEndpoint(
        instance=instance,
        service_type="_mcp._tcp.local.",
        target=f"{instance}.local",
        addresses=(host,),
        port=8000,
        name=name,
    )


def test_nothing_discovered_is_none_not_an_error() -> None:
    assert pick_endpoint([]) is None


def test_the_first_endpoint_wins_without_a_preference() -> None:
    assert pick_endpoint([endpoint("A"), endpoint("B")]).instance == "A"


def test_an_instance_preference_matches_the_instance_name() -> None:
    assert pick_endpoint([endpoint("A"), endpoint("B")], "b").instance == "B"


def test_an_instance_preference_also_matches_the_friendly_txt_name() -> None:
    """Users type what mDNS shows them, which is the ``name=`` TXT value."""
    chosen = pick_endpoint([endpoint("A", name="Studio"), endpoint("B", name="Laptop")], "laptop")
    assert chosen is not None and chosen.instance == "B"


def test_an_unmatched_preference_falls_back_instead_of_failing() -> None:
    assert pick_endpoint([endpoint("A"), endpoint("B")], "nonexistent").instance == "A"


# --- URL resolution ------------------------------------------------------------


async def test_an_explicit_url_bypasses_discovery(monkeypatch) -> None:
    def never(*args, **kwargs):  # pragma: no cover
        raise AssertionError("discovery must not run when --url is given")

    monkeypatch.setattr(connect_module, "browse", never)
    assert (
        await resolve_url(url="  http://192.168.0.54:8000/mcp ") == "http://192.168.0.54:8000/mcp"
    )


async def test_discovery_resolves_to_the_chosen_endpoint_url(monkeypatch) -> None:
    monkeypatch.setattr(
        connect_module, "browse", lambda *a, **k: [endpoint("Studio", host="10.0.0.7")]
    )
    assert await resolve_url(browse_timeout=0.1) == "http://10.0.0.7:8000/mcp"


async def test_nothing_found_explains_the_two_ways_forward(monkeypatch) -> None:
    monkeypatch.setattr(connect_module, "browse", lambda *a, **k: [])
    with pytest.raises(LookupError) as excinfo:
        await resolve_url(browse_timeout=0.1)
    message = str(excinfo.value)
    assert "--url" in message and "--expose" in message


async def test_the_custom_service_type_is_passed_through_to_browsing(monkeypatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        connect_module, "browse", lambda type_, timeout: seen.append((type_, timeout)) or []
    )
    with pytest.raises(LookupError):
        await resolve_url(mdns_type="_iotmcp._tcp", browse_timeout=0.2)
    assert seen == [("_iotmcp._tcp", 0.2)]


# --- client CLI ----------------------------------------------------------------


def test_the_client_defaults_to_discovery_and_the_repl() -> None:
    args = build_parser().parse_args([])
    assert args.url is None and args.mdns_type == "_mcp._tcp"
    assert args.browse_only is False and args.call is None


def test_the_client_can_browse_without_connecting() -> None:
    args = build_parser().parse_args(["--browse-only", "--mdns-type", "_iotmcp._tcp"])
    assert args.browse_only is True and args.mdns_type == "_iotmcp._tcp"


def test_one_shot_mode_takes_json_arguments() -> None:
    args = build_parser().parse_args(
        ["--call", "open_app", "--args", '{"name":"firefox"}', "--json"]
    )
    assert json.loads(args.args) == {"name": "firefox"}
    assert args.json is True


def test_the_client_cli_is_a_separate_entrypoint_from_the_server() -> None:
    prog = build_parser().prog
    assert prog == "python -m iot_mcp.client"
    assert "--expose" not in build_parser().format_help(), "exposure is a server-side decision"


def test_results_are_dumped_as_indented_json() -> None:
    assert json.loads(_dump({"ok": True})) == {"ok": True}
    assert "\n" in _dump({"ok": True, "nested": {"a": 1}})


def test_unserialisable_values_are_stringified_instead_of_crashing_the_client() -> None:
    assert "object at" in _dump({"thing": object()})


def test_the_endpoint_table_shows_both_url_spellings() -> None:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        _print_endpoints([endpoint("Studio", name="Studio Box")])
    text = buffer.getvalue()
    assert "http://192.168.0.5:8000/mcp" in text
    assert "http://Studio.local:8000/mcp" in text
    assert "Studio Box" in text


def test_an_empty_browse_result_prints_a_plain_sentence() -> None:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        _print_endpoints([])
    assert "no MCP servers" in buffer.getvalue()
