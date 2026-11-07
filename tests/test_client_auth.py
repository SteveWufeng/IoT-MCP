"""Authenticated Streamable HTTP transport: raw 401s, SDK handshake success, token precedence."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from iot_mcp.app import build_server
from iot_mcp.auth import TokenAuth, bearer_auth_middleware, generate_token
from iot_mcp.client.cli import _print_endpoints, _resolve_token
from iot_mcp.client.connect import auth_headers, connect, list_tools
from iot_mcp.discovery import DiscoveredEndpoint
from iot_mcp.drivers.base import NullDriver
from tests._http import initialize_body, pick_port, raw_post


@contextlib.asynccontextmanager
async def _serving(app: Any, port: int | None = None) -> AsyncIterator[int]:
    """Serve an already-wrapped ASGI app on loopback without spawning a process."""
    import uvicorn

    chosen = pick_port(port)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=chosen,
        log_level="critical",
        timeout_graceful_shutdown=1,
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


@pytest.fixture
def token() -> str:
    return generate_token()


@pytest.fixture
def protected_app(token: str) -> Any:
    server = build_server(NullDriver("auth-test"))
    app = server.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1")
    return bearer_auth_middleware(app, TokenAuth(token=token))


async def test_raw_http_without_a_token_is_refused_before_the_mcp_handshake(
    protected_app: Any,
) -> None:
    async with _serving(protected_app) as port:
        status, headers, body = await raw_post(port, initialize_body())

    assert status == 401
    assert "application/json" in headers.get("content-type", "")
    assert b"unauthorized" in body
    assert b"bearer token" in body


async def test_the_real_client_connects_and_lists_tools_with_the_bearer_token(
    protected_app: Any, token: str
) -> None:
    async with _serving(protected_app) as port:
        url = f"http://127.0.0.1:{port}/mcp"
        async with connect(url, token=token) as session:
            tools = await list_tools(session)

    assert "list_apps" in {tool["name"] for tool in tools}


async def test_raw_http_with_a_wrong_token_is_refused(protected_app: Any) -> None:
    async with _serving(protected_app) as port:
        status, _, body = await raw_post(
            port,
            initialize_body(),
            extra_headers={"Authorization": f"Bearer {generate_token()}"},
        )

    assert status == 401
    assert b"unauthorized" in body
    assert b"not valid" in body


def test_auth_headers_put_the_bearer_token_in_the_authorization_header() -> None:
    headers = auth_headers("s" * 24, headers={"X-Trace": "test"})

    assert headers == {"X-Trace": "test", "Authorization": "Bearer " + "s" * 24}


def test_explicit_authorization_header_beats_the_token_argument() -> None:
    headers = auth_headers("s" * 24, headers={"Authorization": "Bearer custom"})

    assert headers["Authorization"] == "Bearer custom"


def test_client_token_precedence_is_flag_file_env_file_env_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import argparse

    file = tmp_path / "token"
    file.write_text("f" * 24 + "\n", encoding="utf-8")
    monkeypatch.delenv("IOT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("IOT_MCP_TOKEN_FILE", raising=False)

    assert _resolve_token(argparse.Namespace(token="a" * 24, token_file=str(file))) == "a" * 24
    assert _resolve_token(argparse.Namespace(token=None, token_file=str(file))) == "f" * 24

    monkeypatch.setenv("IOT_MCP_TOKEN_FILE", str(file))
    monkeypatch.setenv("IOT_MCP_TOKEN", "e" * 24)
    assert _resolve_token(argparse.Namespace(token=None, token_file=None)) == "f" * 24

    monkeypatch.delenv("IOT_MCP_TOKEN_FILE")
    assert _resolve_token(argparse.Namespace(token=None, token_file=None)) == "e" * 24


def test_client_token_precedence_falls_back_to_environment_token_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    monkeypatch.delenv("IOT_MCP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("IOT_MCP_TOKEN", "e" * 24)

    assert _resolve_token(argparse.Namespace(token=None, token_file=None)) == "e" * 24


def test_browse_table_shows_advertised_auth_and_browser_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    endpoint = DiscoveredEndpoint(
        instance="Studio",
        service_type="_appmcp._tcp.local.",
        target="Studio.local",
        addresses=("192.0.2.10",),
        port=8000,
        name="Studio Box",
        properties={"auth": "token", "browser": "on"},
    )

    _print_endpoints([endpoint])
    text = capsys.readouterr().out

    assert "auth     token" in text
    assert "browser  on" in text
    assert "401" in text


def test_browse_table_omits_the_token_hint_when_a_client_token_is_configured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    endpoint = DiscoveredEndpoint(
        instance="Studio",
        service_type="_appmcp._tcp.local.",
        target="Studio.local",
        addresses=("192.0.2.10",),
        port=8000,
        name="Studio Box",
        properties={"auth": "token", "browser": "off"},
    )

    _print_endpoints([endpoint], token="t" * 24)
    text = capsys.readouterr().out

    assert "auth     token" in text
    assert "browser  off" in text
    assert "401" not in text
