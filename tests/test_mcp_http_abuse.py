"""Adversarial MCP-layer abuse over raw HTTP — everything the SDK client refuses to do.

The server is unauthenticated and speaks Streamable HTTP; the wire itself is attack
surface. These tests drive the *real* server in-process over loopback (harness in
:mod:`tests._http`, ports 8300-8399, no subprocess spawn) with hand-built HTTP: forged
Host/Origin, wrong Accept, malformed or truncated bodies, oversized payloads, session
id abuse, forbidden verbs, request floods and type-confused tool arguments.

Findings were established first by probing (see tests/reports); every status code
asserted here is *measured*, not idealogical. The overarching question of each test is
the same: does the abuse degrade one request, or does it hurt the session/server?
So nearly every test ends with a health round-trip proving the session survived.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from _http import (
    BASE_HEADERS,
    initialize_body,
    initialize_session,
    jsonrpc_messages,
    raw_post,
    rpc_body,
    serving,
)

from iot_mcp.app import build_server
from iot_mcp.capabilities import ALL_CAPABILITIES
from iot_mcp.drivers.base import AutomationDriver, NullDriver, Support, ok

PORT = 8361


@pytest.fixture
async def port() -> Any:
    async with serving(build_server(NullDriver("abuse"), name="abuse"), PORT) as bound:
        yield bound


# ---------------------------------------------------------------- header abuse


async def test_json_only_accept_is_rejected_with_406(port: int) -> None:
    """Streamable HTTP requires clients to accept both media types; a client that
    doesn't cannot silently downgrade the server's response framing."""
    status, _, body = await raw_post(
        port,
        initialize_body(),
        headers={**BASE_HEADERS, "Accept": "application/json"},
    )
    assert status == 406
    assert json.loads(body)["error"]["code"] == -32600


async def test_missing_content_type_is_refused(port: int) -> None:
    headers = {k: v for k, v in BASE_HEADERS.items() if k != "Content-Type"}
    status, _, body = await raw_post(port, initialize_body(), headers=headers)
    assert status == 400
    assert b"Content-Type" in body


async def test_text_plain_content_type_is_refused(port: int) -> None:
    """No cross-service POST from an HTML form: strict JSON framing is enforced."""
    status, _, body = await raw_post(
        port,
        initialize_body(),
        headers={**BASE_HEADERS, "Content-Type": "text/plain"},
    )
    assert status == 400


async def test_a_foreign_host_header_is_rejected_as_misdirected(port: int) -> None:
    """DNS-rebinding defence: a rebinding attack presents the attacker's Host.

    (Note for the report: the pre-existing e2e 'rebinding' test claims to check this
    but its header-forgery argument is never actually applied to the request — this
    test forges it for real.)
    """
    status, _, body = await raw_post(
        port,
        initialize_body(),
        headers={**BASE_HEADERS, "Host": "evil.example:1234"},
    )
    assert status == 421
    assert b"Host" in body


async def test_a_portless_host_header_is_rejected(port: int) -> None:
    """Documented strictness: allowed_hosts patterns are host:port shaped, so even
    ``Host: 127.0.0.1`` without port is rejected. Harmless, but it is why the old
    e2e test 'passed': its accidental portless Host tripped 421."""
    status, _, _ = await raw_post(
        port, initialize_body(), headers={**BASE_HEADERS, "Host": "127.0.0.1"}
    )
    assert status == 421


async def test_a_cross_site_origin_is_forbidden(port: int) -> None:
    """Browser-based drive-by against the loopback port is stopped at the Origin gate."""
    status, _, body = await raw_post(
        port, initialize_body(), extra_headers={"Origin": "http://evil.example"}
    )
    assert status == 403
    assert b"Origin" in body


async def test_the_null_origin_from_a_sandboxed_iframe_is_forbidden(port: int) -> None:
    status, _, _ = await raw_post(port, initialize_body(), extra_headers={"Origin": "null"})
    assert status == 403


async def test_the_servers_own_origin_is_accepted(port: int) -> None:
    status, _, _ = await raw_post(
        port, initialize_body(), extra_headers={"Origin": f"http://127.0.0.1:{port}"}
    )
    assert status == 200


# ------------------------------------------------------------- body abuse


async def test_malformed_json_gets_a_jsonrpc_parse_error(port: int) -> None:
    status, _, body = await raw_post(port, b'{"jsonrpc": "2.0", "id": 1, "method": ')
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32700


async def test_valid_json_that_is_not_jsonrpc_is_refused_without_state_change(port: int) -> None:
    status, _, body = await raw_post(port, b'{"hello": "world"}')
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32602
    sid_headers, _ = await initialize_session(port, req_id=2)
    assert "Mcp-Session-Id" in str(sid_headers) or sid_headers  # server still handshakes


async def test_a_five_megabyte_body_is_refused_with_413(port: int) -> None:
    """The unauthenticated listener caps request bodies — a flood of huge bodies
    cannot exhaust server memory one connection at a time."""
    big = rpc_body("tools/call", {"name": "open_app", "args": {"name": "A" * 5_000_000}}, 10)
    status, _, body = await raw_post(port, big, read_timeout=20)
    assert status == 413
    assert b"too large" in body.lower()


async def test_a_truncated_body_hangs_the_request_but_not_the_server(port: int) -> None:
    """Declared Content-Length beyond what is sent: the connection is held pending,
    never answered, and the server stays fully functional for everyone else."""
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await raw_post(port, initialize_body(), truncate_at=5, read_timeout=1.5)
    sid_headers, _ = await initialize_session(port, req_id=3)
    status, _, body = await raw_post(
        port, rpc_body("tools/call", {"name": "list_actions"}, 4), extra_headers=sid_headers
    )
    assert status == 200


async def test_a_client_that_rsts_after_headers_leaves_no_aftermath(port: int) -> None:
    await raw_post(port, initialize_body(), close_early=True)
    sid_headers, _ = await initialize_session(port, req_id=5)
    assert sid_headers


# ---------------------------------------------------------------- sessions


async def test_requests_without_a_session_id_are_rejected(port: int) -> None:
    status, _, body = await raw_post(port, rpc_body("tools/call", {"name": "list_actions"}, 8))
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32600


async def test_a_guessed_session_id_is_not_found(port: int) -> None:
    """Session ids are the only thing isolating concurrent clients — guessing must
    fail (404), not silently attach to someone else's session."""
    status, _, body = await raw_post(
        port,
        rpc_body("tools/call", {"name": "list_actions"}, 9),
        extra_headers={"Mcp-Session-Id": "00000000000000000000000000000000"},
    )
    assert status == 404
    assert "not found" in json.loads(body)["error"]["message"].lower()


async def test_deleting_a_session_makes_its_id_unusable(port: int) -> None:
    """Explicit termination works and the id cannot be resurrected (hijack-after-exit)."""
    sid_headers, _ = await initialize_session(port, req_id=6)
    sid = sid_headers["Mcp-Session-Id"]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"DELETE /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nMcp-Session-Id: {sid}\r\n\r\n".encode()
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    writer.close()
    assert b" 200 " in head.split(b"\r\n", 1)[0]
    status, _, _ = await raw_post(
        port,
        rpc_body("tools/call", {"name": "list_actions"}, 10),
        extra_headers={"Mcp-Session-Id": sid},
    )
    assert status == 404


async def test_get_without_a_session_id_is_rejected(port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: text/event-stream\r\n\r\n".encode()
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.read(64), 5)
    writer.close()
    assert b" 400 " in head.split(b"\r\n", 1)[0]


@pytest.mark.parametrize("verb", ["OPTIONS", "PUT", "PATCH"])
async def test_only_the_supported_verbs_are_served(port: int, verb: str) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{verb} /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.read(64), 5)
    writer.close()
    assert b" 405 " in head.split(b"\r\n", 1)[0]


# ---------------------------------------------------------------- floods


async def test_twenty_concurrent_abusive_requests_degrade_to_codes_not_downtime(
    port: int,
) -> None:
    """A bounded request storm of no-session calls must produce 20 refusals and keep
    serving legitimate traffic. (Bounded deliberately: the sandbox protocol forbids
    open-ended stress.)"""
    codes = await asyncio.gather(
        *[raw_post(port, rpc_body("tools/call", {"name": "x"}, 100 + i)) for i in range(20)]
    )
    assert all(status == 400 for status, _, _ in codes)
    sid_headers, _ = await initialize_session(port, req_id=200)
    status, _, body = await raw_post(
        port, rpc_body("tools/call", {"name": "list_actions"}, 201), extra_headers=sid_headers
    )
    assert status == 200
    assert any("result" in m for m in jsonrpc_messages(body))


# ------------------------------------------------------- argument-type abuse


async def _call(
    session_headers: dict[str, str], port: int, req_id: int, params: dict[str, Any]
) -> dict[str, str | bool]:
    status, _, body = await raw_post(
        port, rpc_body("tools/call", params, req_id), extra_headers=session_headers
    )
    messages = jsonrpc_messages(body)
    assert messages, f"no response message for request {req_id}"
    message = messages[0]
    if "result" in message:
        return {"http": str(status), "is_error": bool(message["result"].get("isError"))}
    return {"http": str(status), "is_error": True, "rpc_error": message["error"]["code"]}


async def test_an_object_where_a_string_name_is_expected_fails_the_tool_not_the_session(
    port: int,
) -> None:
    sid_headers, _ = await initialize_session(port)
    outcome = await _call(
        sid_headers, port, 11, {"name": "open_app", "arguments": {"name": {"evil": True}}}
    )
    assert outcome["http"] == "200" and outcome["is_error"] is True
    outcome = await _call(sid_headers, port, 12, {"name": "media_status"})
    assert outcome["http"] == "200" and outcome["is_error"] is False, "session still healthy"


async def test_a_null_url_is_refused_by_schema_validation(port: int) -> None:
    sid_headers, _ = await initialize_session(port)
    outcome = await _call(sid_headers, port, 13, {"name": "open_url", "arguments": {"url": None}})
    assert outcome["is_error"] is True


async def test_a_twenty_digit_integer_name_is_refused_not_coerced(port: int) -> None:
    sid_headers, _ = await initialize_session(port)
    outcome = await _call(
        sid_headers,
        port,
        14,
        {"name": "open_app", "arguments": {"name": 99_999_999_999_999_999_999}},
    )
    assert outcome["is_error"] is True


async def test_an_unknown_jsonrpc_method_never_reaches_a_tool(port: int) -> None:
    sid_headers, _ = await initialize_session(port)
    status, _, body = await raw_post(
        port, rpc_body("shutdown/reboot", {}, 16), extra_headers=sid_headers
    )
    messages = jsonrpc_messages(body)
    assert messages[0]["error"]["code"] == -32601
    outcome = await _call(sid_headers, port, 17, {"name": "media_status"})
    assert outcome["is_error"] is False


async def test_calling_a_nonexistent_tool_is_a_tool_level_error(port: int) -> None:
    sid_headers, _ = await initialize_session(port)
    outcome = await _call(sid_headers, port, 18, {"name": "shutdown_the_machine"})
    assert outcome["http"] == "200" and outcome["is_error"] is True


# ------------------------------------------------- content that breaks JSON


class SurrogateDriver(AutomationDriver):
    """A driver whose data contains a lone surrogate — unserialisable as UTF-8.

    Models a filesystem that hands the driver a filename Python decoded with
    surrogateescape (``b"bad\\xff\\xfename"``); if such a name can reach a tool
    result, serialising the result raises inside the server.
    """

    name = "surro"

    def _probe(self) -> dict[Any, Support]:
        return {capability: Support.yes("test double") for capability in ALL_CAPABILITIES}

    async def list_apps(self) -> dict[str, Any]:
        bad = "bad\udcffname"
        return ok("apps.list", apps=[{"id": bad, "path": "/x/" + bad + ".desktop"}])

    async def open_app(self, name: str) -> dict[str, Any]:
        return ok()

    async def close_app(self, name: str) -> dict[str, Any]:
        return ok()

    async def open_url(self, url: str) -> dict[str, Any]:
        return ok()

    async def media_play_pause(self) -> dict[str, Any]:
        return ok()

    async def media_next(self) -> dict[str, Any]:
        return ok()

    async def media_previous(self) -> dict[str, Any]:
        return ok()

    async def media_status(self) -> dict[str, Any]:
        return ok("media.status", state="playing")

    async def list_windows(self) -> dict[str, Any]:
        return ok()

    async def focus_window(self, app_or_title: str) -> dict[str, Any]:
        return ok()


async def test_unserialisable_tool_data_fails_one_call_and_leaks_no_internals() -> None:
    """A poisoned filename degrades exactly one tool call; the session survives and
    the client-visible text carries no python exception detail."""
    async with serving(build_server(SurrogateDriver(), name="surro-abuse"), 8362) as local:
        sid_headers, _ = await initialize_session(local)
        status, _, body = await raw_post(
            local,
            rpc_body("tools/call", {"name": "list_apps"}, 6),
            extra_headers=sid_headers,
            read_timeout=15,
        )
        assert status == 200, "the HTTP layer still answers"
        text = json.dumps(jsonrpc_messages(body))
        assert '"isError": true' in text or '"isError":true' in text
        assert "Traceback" not in text and "UnicodeEncodeError" not in text
        assert "\udcff" not in text and "\\udcff" not in text, "surrogate never reaches the wire"
        status, _, body = await raw_post(
            local, rpc_body("tools/call", {"name": "media_status"}, 7), extra_headers=sid_headers
        )
        assert status == 200 and any("result" in m for m in jsonrpc_messages(body))


async def test_unicode_control_characters_in_arguments_survive_as_data(port: int) -> None:
    """ESC/ANSI escape codes inside tool args must not become terminal escapes in
    whatever renders the response: they come back as inert (quoted) text at worst."""
    sid_headers, _ = await initialize_session(port)
    hostile = "\x1b[31mPWNED\x1b[0m"
    status, _, body = await raw_post(
        port,
        rpc_body("tools/call", {"name": "open_app", "arguments": {"name": hostile}}, 19),
        extra_headers=sid_headers,
    )
    messages = jsonrpc_messages(body)
    # NullDriver says unsupported (or validation errors) — either way the raw ESC
    # sequence must only appear JSON-escaped inside a JSON string, never raw.
    assert "\x1b" not in body.decode("utf-8", errors="replace").replace("\\u001b", "")
    assert messages and status == 200
