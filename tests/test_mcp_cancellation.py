"""Request cancellation over the wire: ``notifications/cancelled`` must be safe.

The MCP spec lets an unauthenticated client abort an in-flight request. For this
server that is a sharp edge: a running tool call is a coroutine *around a subprocess*.
These tests drive the real stack over loopback HTTP and pin the full contract:

* the running handler receives a real cancellation (does not keep working);
* the abandoned request stream is answered exactly once, with ``REQUEST_CANCELLED``
  (-32800), rather than left hanging;
* the session and later calls are unharmed; and
* cancelling a request id that never existed is inert.

(What the driver does with the cancellation of its child process is pinned separately
in :mod:`tests.test_resource_controls` — it does not kill it, which is a finding.)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
from _http import initialize_session, jsonrpc_messages, raw_post, rpc_body, serving

from iot_mcp.app import build_server
from iot_mcp.capabilities import ALL_CAPABILITIES
from iot_mcp.drivers.base import AutomationDriver, Support, ok

PORT = 8363

#: SDK constant: this era answers a cancelled request with -32800 (streamable_http.py).
REQUEST_CANCELLED = -32800


class SlowDriver(AutomationDriver):
    """``open_app`` announces that it started, then sleeps forever when interrupted."""

    name = "slow"

    def __init__(self) -> None:
        self.events: list[str] = []

    def _probe(self) -> dict[Any, Support]:
        return {capability: Support.yes("test double") for capability in ALL_CAPABILITIES}

    async def list_apps(self) -> dict[str, Any]:
        return ok("apps.list", apps=[])

    async def open_app(self, name: str) -> dict[str, Any]:
        self.events.append("start")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.events.append("cancelled")
            raise
        self.events.append("finish")  # pragma: no cover - cancelling tests never reach it
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


@pytest.fixture
async def slow() -> Any:
    driver = SlowDriver()
    async with serving(build_server(driver, name="slow-test"), PORT) as port:
        yield port, driver


async def _wait_until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true in time")
        await asyncio.sleep(0.02)


async def _retire(call: asyncio.Task[Any]) -> None:
    """Join the in-flight POST, whether it answered (the SDK closes the cancelled
    stream with an error message) or is still hanging (then cancel it)."""
    if call.done():
        with contextlib.suppress(Exception):
            call.result()
        return
    call.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await call


async def test_cancelling_a_running_call_interrupts_the_handler(
    slow: tuple[int, SlowDriver],
) -> None:
    """The cancellation actually unwinds the tool coroutine — the handler records
    CancelledError rather than running to completion."""
    port, driver = slow
    sid_headers, _ = await initialize_session(port)
    call = asyncio.create_task(
        raw_post(
            port,
            rpc_body("tools/call", {"name": "open_app", "arguments": {"name": "x"}}, 42),
            extra_headers=sid_headers,
            read_timeout=20,
        )
    )
    try:
        await _wait_until(lambda: "start" in driver.events)
        status, _, _ = await raw_post(
            port,
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 42}}
            ).encode(),
            extra_headers=sid_headers,
        )
        assert status == 202, "the cancellation notification is accepted"
        await _wait_until(lambda: "cancelled" in driver.events)
        assert "finish" not in driver.events
    finally:
        await _retire(call)


async def test_the_cancelled_request_is_answered_exactly_once_with_minus_32800(
    slow: tuple[int, SlowDriver],
) -> None:
    """An abandoned request must not leave the client stream hanging open: the SDK
    era answers it once with REQUEST_CANCELLED and closes the stream."""
    port, driver = slow
    sid_headers, _ = await initialize_session(port)
    call = asyncio.create_task(
        raw_post(
            port,
            rpc_body("tools/call", {"name": "open_app", "arguments": {"name": "x"}}, 43),
            extra_headers=sid_headers,
            read_timeout=20,
        )
    )
    try:
        await _wait_until(lambda: "start" in driver.events)
        await raw_post(
            port,
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 43}}
            ).encode(),
            extra_headers=sid_headers,
        )
        status, _, body = await call
        messages = jsonrpc_messages(body)
        assert status == 200
        assert [m["id"] for m in messages] == [43], "answered exactly once"
        assert messages[0]["error"]["code"] == REQUEST_CANCELLED
    finally:
        await _retire(call)


async def test_the_session_survives_cancellation_of_its_own_request(
    slow: tuple[int, SlowDriver],
) -> None:
    port, driver = slow
    sid_headers, _ = await initialize_session(port)
    call = asyncio.create_task(
        raw_post(
            port,
            rpc_body("tools/call", {"name": "open_app", "arguments": {"name": "x"}}, 44),
            extra_headers=sid_headers,
            read_timeout=20,
        )
    )
    try:
        await _wait_until(lambda: "start" in driver.events)
        await raw_post(
            port,
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 44}}
            ).encode(),
            extra_headers=sid_headers,
        )
        await _wait_until(lambda: "cancelled" in driver.events, timeout=10)
    finally:
        await _retire(call)
    status, _, body = await raw_post(
        port, rpc_body("tools/call", {"name": "media_status"}, 45), extra_headers=sid_headers
    )
    assert status == 200
    assert any(m.get("id") == 45 and "result" in m for m in jsonrpc_messages(body))


async def test_cancelling_a_request_id_that_never_existed_is_inert(
    slow: tuple[int, SlowDriver],
) -> None:
    """A hostile client spamming cancellations for unknown ids must not disturb the
    session: no crash, no wedged queue, later calls still answered."""
    port, _ = slow
    sid_headers, _ = await initialize_session(port)
    for unknown in (9999, "string-id", -1):
        status, _, _ = await raw_post(
            port,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": unknown},
                }
            ).encode(),
            extra_headers=sid_headers,
        )
        assert status == 202
    status, _, body = await raw_post(
        port, rpc_body("tools/call", {"name": "media_status"}, 46), extra_headers=sid_headers
    )
    assert status == 200
    assert any(m.get("id") == 46 for m in jsonrpc_messages(body))
