"""End-to-end over real HTTP: the real server process, the real SDK client, loopback only.

Only read-only tools are called here, plus one call the URL policy must reject before it can
launch anything. ``focus_window`` is deliberately absent: it would move the keyboard focus of
whoever is sitting at this machine, and ``open_app`` would start a program on their desktop.

The server runs with ``--no-mdns``, so this test needs no responder and sends no mDNS traffic;
discovery itself is covered in :mod:`tests.test_discovery` and was confirmed on real hardware
(see "What is actually verified" in ``docs/internals.md``).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

from iot_mcp.client.connect import call_tool, connect, list_tools

# These tests boot a real server process; the marker is the suite's opt-in for that.
pytestmark = pytest.mark.allow_real_processes


def free_port() -> int:
    """Ask the OS for an unused TCP port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def wait_for_port(port: int, process: subprocess.Popen[bytes], timeout: float = 25.0) -> None:
    """Block until the server accepts connections, or fail with its log output."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"server exited early with {process.returncode}:\n{process.stderr.read().decode(errors='replace')}"
            )
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.1)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        _ = reader
        return
    raise AssertionError(f"server did not start listening on 127.0.0.1:{port} within {timeout}s")


@pytest.fixture
def live_server() -> Iterator[str]:
    """Boot ``python -m iot_mcp`` on loopback and yield its Streamable HTTP URL."""
    port = free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "iot_mcp",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-mdns",
            "--log-level",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        asyncio.run(wait_for_port(port, process))
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - hung server
            process.kill()
            process.wait(timeout=5)


async def test_the_server_offers_exactly_the_documented_tools(live_server: str) -> None:
    async with connect(live_server) as session:
        tools = await list_tools(session)
    names = sorted(tool["name"] for tool in tools)
    assert names == sorted(
        [
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
    )
    assert all(tool["description"] for tool in tools)


async def test_list_actions_over_the_wire_describes_this_machine(live_server: str) -> None:
    async with connect(live_server) as session:
        result = await call_tool(session, "list_actions")
    assert result.ok is True
    payload = result.payload
    assert payload["ok"] is True
    assert payload["platform"] == sys.platform
    assert payload["summary"]["total"] == 10
    for name, record in payload["capabilities"].items():
        assert isinstance(record["supported"], bool), name
        assert record["reason"], name


async def test_read_only_tools_return_real_data_over_http(live_server: str) -> None:
    """These two touch the live session; both are observers, so nothing changes state."""
    async with connect(live_server) as session:
        windows = await call_tool(session, "list_windows")
        media = await call_tool(session, "media_status")
    assert windows.ok is True
    assert isinstance(windows.payload["windows"], list)
    assert media.payload["ok"] is True
    assert "players" in media.payload


async def test_the_url_policy_is_enforced_through_the_protocol(live_server: str) -> None:
    """A rejected URL must never reach a launcher, and the agent must get a usable reason."""
    async with connect(live_server) as session:
        result = await call_tool(session, "open_url", {"url": "file:///etc/passwd"})
    assert result.ok is False
    error = result.payload["error"]
    assert error["type"] == "invalid_input"
    assert error["capability"] == "url.open"
    assert "file" in error["hint"] or "Allowed" in error["hint"]


async def test_an_unknown_tool_fails_the_protocol_not_the_session(live_server: str) -> None:
    async with connect(live_server) as session:
        result = await call_tool(session, "shutdown_the_machine")
    assert result.ok is False
    # the session must still be usable after a protocol-level refusal
    async with connect(live_server) as second:
        assert (await call_tool(second, "list_actions")).ok is True


async def test_a_missing_argument_is_refused_by_schema_validation(live_server: str) -> None:
    async with connect(live_server) as session:
        result = await call_tool(session, "open_app", {})
    assert result.ok is False


async def test_loopback_keeps_dns_rebinding_protection_on() -> None:
    """A server bound to loopback must reject a foreign Host header (DNS-rebinding defence)."""
    port = free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "iot_mcp",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-mdns",
            "--log-level",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        await wait_for_port(port, process)
        response = await _post(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Host": "evil.example", "Origin": "http://evil.example"},
        )
        assert response[0] in (400, 421), f"expected the Host check to reject, got {response}"
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


async def _post(url: str, *, headers: dict[str, str]) -> tuple[int, str]:
    """Minimal HTTP POST so the test can forge the Host header the client library would send."""
    import json as _json

    body = _json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        }
    ).encode()
    host, _, rest = url.split("//", 1)[1].partition("/")
    host_header, _, port_text = host.partition(":")
    reader, writer = await asyncio.open_connection("127.0.0.1", int(port_text or 80))
    request = (
        f"POST /{rest} HTTP/1.1\r\nHost: {host_header}\r\nContent-Type: application/json\r\n"
        f"Accept: application/json, text/event-stream\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    head, _, _ = data.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0
    return code, head.decode(errors="replace")
