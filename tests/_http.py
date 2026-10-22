"""Shared in-process Streamable-HTTP harness (helper module — not a test module).

Adversarial MCP-layer tests need to speak raw HTTP: a well-behaved SDK client cannot
send a forged ``Host``, a wrong ``Accept``, a truncated body or a 5 MB request. This
module boots the *real* server (uvicorn in this process — no subprocess is spawned, so
the suite's no-real-process tripwire stays armed) bound strictly to loopback on a port
from the operator-sanctioned 8300-8399 range, and provides a small raw-socket client.

Nothing here performs discovery, binds non-loopback, or launches anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

PORT_MIN = 8300
PORT_MAX = 8399

BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    # Host is injected per-request with the bound port; pass it in ``headers`` to forge.
}


def pick_port(preferred: int | None = None) -> int:
    """Return a free TCP port inside the sanctioned 8300-8399 loopback range.

    ``SO_REUSEADDR`` mirrors uvicorn's own bind, so TIME_WAIT sockets from a previous
    run do not look like occupancy.
    """
    if preferred is not None and not (PORT_MIN <= preferred <= PORT_MAX):
        raise ValueError(f"port {preferred} is outside the sanctioned test range")
    start = preferred if preferred is not None else PORT_MIN
    candidates = list(range(start, PORT_MAX + 1))
    if preferred is not None:
        candidates += list(range(PORT_MIN, start))
    for port in candidates:
        with contextlib.suppress(OSError), socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
            return port
    raise RuntimeError("no free port in 8300-8399")


@contextlib.asynccontextmanager
async def serving(server: Any, port: int | None = None) -> AsyncIterator[int]:
    """Serve ``server`` (an MCPServer) over Streamable HTTP on 127.0.0.1, then stop it.

    Yields the bound port. Teardown always asks uvicorn to exit and awaits the serve
    task so a hung test fails loudly instead of leaking the listener.
    """
    import uvicorn

    chosen = pick_port(port)
    app = server.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1")
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=chosen,
        log_level="critical",
        timeout_graceful_shutdown=1,  # abusive connections (truncated bodies) must not stall teardown
    )
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve())
    deadline = asyncio.get_running_loop().time() + 10.0
    while not instance.started:
        if instance.should_exit or asyncio.get_running_loop().time() > deadline:
            task.cancel()
            with contextlib.suppress(Exception):
                await task
            raise RuntimeError(f"uvicorn did not start on 127.0.0.1:{chosen}")
        await asyncio.sleep(0.02)
    try:
        yield chosen
    finally:
        instance.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, 5)
        task.cancel()
        with contextlib.suppress(Exception):
            await task


# --------------------------------------------------------------------------- client


def rpc_body(method: str, params: dict[str, Any] | None, req_id: int | None = 1) -> bytes:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if req_id is not None:
        message["id"] = req_id
    return json.dumps(message).encode()


def initialize_body(req_id: int = 1) -> bytes:
    return rpc_body(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "sqa-raw", "version": "0"},
        },
        req_id,
    )


async def raw_post(
    port: int,
    body: bytes,
    *,
    extra_headers: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,  # replaces BASE_HEADERS entirely
    read_timeout: float = 10.0,
    truncate_at: int | None = None,
    close_early: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    """POST ``body`` to ``/mcp`` on loopback and read the response.

    Reads until EOF or until a JSON-RPC message is parsed from the (possibly SSE)
    stream, whichever comes first — never a fixed socket read. ``truncate_at`` sends
    only that many bytes of the declared-length body (truncation attacks);
    ``close_early`` closes the connection immediately after writing.
    """
    sent = body if truncate_at is None else body[:truncate_at]
    merged = {
        **(headers or BASE_HEADERS),
        **(extra_headers or {}),
    }
    if not any(name.lower() == "host" for name in merged):
        merged["Host"] = f"127.0.0.1:{port}"
    lines = ["POST /mcp HTTP/1.1"]
    for name, value in merged.items():
        if value is not None:
            lines.append(f"{name}: {value}")
    lines.append(f"Content-Length: {len(body)}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode() + sent
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        if close_early:
            return 0, {}, b""
        status, parsed, raw = await asyncio.wait_for(_read_http(reader), timeout=read_timeout)
        return status, parsed, raw
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _read_http(reader: asyncio.StreamReader) -> tuple[int, dict[str, str], bytes]:
    """Read status line + headers, then the body (Content-Length or chunked/SSE EOF)."""
    header_blob = await reader.readuntil(b"\r\n\r\n")
    head_text = header_blob.decode("latin-1")
    lines = head_text.split("\r\n")
    parts = lines[0].split()
    status = int(parts[1]) if len(parts) > 2 else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    body = b""
    if "content-length" in headers and "chunked" not in headers.get("transfer-encoding", ""):
        body = await reader.readexactly(int(headers["content-length"]))
    else:
        # chunked or SSE: keep reading until the server closes the stream.
        with contextlib.suppress(asyncio.IncompleteReadError):
            body = await reader.read(-1)
    return status, headers, body


def jsonrpc_messages(payload: bytes) -> list[dict[str, Any]]:
    """Parse a JSON or ``text/event-stream`` payload into JSON-RPC message dicts."""
    text = payload.decode("utf-8", errors="replace")
    messages: list[dict[str, Any]] = []
    for chunk in text.split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data:"):
                text_piece = line[5:].strip()
                if text_piece:
                    with contextlib.suppress(json.JSONDecodeError):
                        messages.append(json.loads(text_piece))
    if not messages and text.strip().startswith("{"):
        with contextlib.suppress(json.JSONDecodeError):
            messages.append(json.loads(text))
    return messages


async def initialize_session(port: int, req_id: int = 1) -> tuple[dict[str, str], bytes]:
    """Drive the full handshake and return ``(headers_with_session, init_payload)``.

    The returned headers dict already carries ``Mcp-Session-Id`` and is the value to
    pass to :func:`raw_post` via ``extra_headers`` for follow-up requests.
    """
    status, headers, body = await raw_post(port, initialize_body(req_id))
    assert status == 200, f"initialize failed with {status}: {body[:200]!r}"
    sid = headers.get("mcp-session-id")
    assert sid, f"no session id in {headers}"
    notify = rpc_body("notifications/initialized", None, None)
    status, _, _ = await raw_post(port, notify, extra_headers={"Mcp-Session-Id": sid})
    assert status in (200, 202), f"initialized notification got {status}"
    return {"Mcp-Session-Id": sid}, body
