"""Bearer-token authentication: validation, token files, header extraction, middleware."""

from __future__ import annotations

import json
import stat
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from iot_mcp.auth import (
    AuthConfigError,
    TokenAuth,
    TokenThrottle,
    bearer_auth_middleware,
    extract_token,
    generate_token,
    read_token_file,
    token_fingerprint,
    validate_token,
    write_token_file,
)

GOOD = "a" * 24


def _scope(
    *,
    headers: dict[str, str] | None = None,
    query: str = "",
    allow_query: bool = False,
    scope_type: str = "http",
) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": scope_type,
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or {}).items()
        ],
        "query_string": query.encode("latin-1"),
        "client": ("203.0.113.7", 51234),
    }
    if allow_query:
        scope["iot_mcp.allow_query_token"] = True
    return scope


async def _pass_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 204,
            "headers": [(b"x-inner-app", b"yes")],
        }
    )
    await send({"type": "http.response.body", "body": b""})


async def _call_asgi(
    app: Any,
    *,
    headers: dict[str, str] | None = None,
    query: str = "",
    allow_query: bool = False,
    client: tuple[str, int] = ("203.0.113.7", 51234),
    scope_type: str = "http",
) -> dict[str, Any]:
    scope = _scope(headers=headers, query=query, allow_query=allow_query, scope_type=scope_type)
    scope["client"] = client
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = messages[0] if messages else {}
    body = b"".join(message.get("body", b"") for message in messages if "body" in message)
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        payload = None
    response_headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start.get("headers", []) or []
    }
    return {
        "status": start.get("status"),
        "body": body,
        "json": payload,
        "headers": response_headers,
    }


# --- token shape ---------------------------------------------------------------


def test_generated_tokens_are_valid_and_url_safe() -> None:
    token = generate_token()
    assert validate_token(token, source="generated") == token
    assert len(token) >= 24
    assert " " not in token


def test_validate_token_trims_surrounding_whitespace_only() -> None:
    assert validate_token(f"\n  {GOOD}  \n") == GOOD


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        ("", "empty"),
        ("a" * 23, "minimum"),
        ("a" * 257, "maximum"),
        ("short token with spaces" * 2, "whitespace"),
        ("line\nbreak" + ("x" * 30), "printable"),
    ],
)
def test_validate_token_refuses_secrets_that_would_be_broken_in_headers(
    bad: str, needle: str
) -> None:
    with pytest.raises(AuthConfigError) as exc:
        validate_token(bad, source="test-token")
    assert needle in str(exc.value).lower()


def test_fingerprint_is_stable_and_does_not_leak_the_token() -> None:
    fingerprint = token_fingerprint(GOOD)
    assert fingerprint == token_fingerprint(GOOD)
    assert GOOD not in fingerprint


# --- token files ----------------------------------------------------------------


def test_read_token_file_uses_first_non_blank_line_and_warns_on_loose_mode(
    tmp_path: Path,
) -> None:
    file = tmp_path / "token"
    file.write_text(f"\n{GOOD}\nsecond line\n")
    file.chmod(0o644)
    stream = StringIO()

    assert read_token_file(file, stream=stream) == GOOD
    assert "warning" in stream.getvalue()
    assert "chmod" in stream.getvalue()


def test_read_token_file_does_not_warn_on_owner_only_mode(tmp_path: Path) -> None:
    file = tmp_path / "token"
    file.write_text(GOOD + "\n")
    file.chmod(0o600)
    stream = StringIO()

    assert read_token_file(file, stream=stream) == GOOD
    assert stream.getvalue() == ""


def test_read_token_file_reports_missing_file_as_auth_config_error(tmp_path: Path) -> None:
    with pytest.raises(AuthConfigError, match="cannot read token file"):
        read_token_file(tmp_path / "missing")


def test_write_token_file_creates_owner_only_file_and_never_overwrites(
    tmp_path: Path,
) -> None:
    target = tmp_path / "token"
    path, token = write_token_file(target)

    assert path == target
    assert len(token) >= 24
    assert validate_token(token) == token
    assert target.read_text() == token + "\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    with pytest.raises(AuthConfigError, match="already exists"):
        write_token_file(target)


# --- verification --------------------------------------------------------------


def test_token_auth_verify_accepts_only_exact_token() -> None:
    auth = TokenAuth(token=GOOD)
    assert auth.verify(GOOD) is True
    assert auth.verify(GOOD + "x") is False
    assert auth.verify(None) is False
    assert auth.verify("") is False


def test_token_auth_fingerprint_is_safe_to_print() -> None:
    auth = TokenAuth(token=GOOD)
    assert auth.fingerprint == token_fingerprint(GOOD)
    assert GOOD not in repr(auth.fingerprint)


# --- extraction -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Authorization": f"Bearer {GOOD}"}, (GOOD, "authorization:bearer")),
        ({"Authorization": GOOD}, (GOOD, "authorization")),
        ({"X-Api-Key": GOOD}, (GOOD, "x-api-key")),
        ({"X-IoT-MCP-Token": GOOD}, (GOOD, "x-iot-mcp-token")),
    ],
)
def test_extract_token_understands_supported_presentations(
    headers: dict[str, str], expected: tuple[str | None, str | None]
) -> None:
    assert extract_token(_scope(headers=headers)) == expected


def test_extract_token_requires_opt_in_for_query_string() -> None:
    assert extract_token(_scope(query=f"token={GOOD}")) == (None, "query:token-refused")
    assert extract_token(_scope(query=f"token={GOOD}", allow_query=True)) == (
        GOOD,
        "query:token",
    )


def test_extract_token_returns_none_when_nothing_is_presented() -> None:
    assert extract_token(_scope()) == (None, None)


# --- middleware -----------------------------------------------------------------


async def test_middleware_passes_valid_bearer_token_to_inner_app() -> None:
    app = bearer_auth_middleware(_pass_app, TokenAuth(token=GOOD))
    result = await _call_asgi(app, headers={"Authorization": f"Bearer {GOOD}"})

    assert result["status"] == 204
    assert result["headers"]["x-inner-app"] == "yes"


async def test_middleware_rejects_missing_and_wrong_tokens_with_json_401() -> None:
    app = bearer_auth_middleware(_pass_app, TokenAuth(token=GOOD))

    missing = await _call_asgi(app)
    wrong = await _call_asgi(app, headers={"Authorization": "Bearer " + "b" * 24})

    assert missing["status"] == wrong["status"] == 401
    assert missing["json"]["error"]["message"] == "a bearer token is required"
    assert wrong["json"]["error"]["message"] == "the presented token is not valid"
    assert "Authorization: Bearer" in wrong["json"]["error"]["hint"]
    assert "invalid_token" in wrong["headers"]["www-authenticate"]
    assert "cache-control" in wrong["headers"]


async def test_middleware_refuses_query_tokens_unless_configured() -> None:
    strict = bearer_auth_middleware(_pass_app, TokenAuth(token=GOOD))
    lenient = bearer_auth_middleware(_pass_app, TokenAuth(token=GOOD, allow_query_token=True))

    rejected = await _call_asgi(strict, query=f"token={GOOD}")
    accepted = await _call_asgi(lenient, query=f"token={GOOD}")

    assert rejected["status"] == 401
    assert rejected["json"]["error"]["message"] == "query-string tokens are not accepted"
    assert accepted["status"] == 204


async def test_middleware_rate_limits_failed_tokens_per_client() -> None:
    auth = TokenAuth(token=GOOD, max_failures=2, window_seconds=60)
    app = bearer_auth_middleware(_pass_app, auth)

    first = await _call_asgi(app, client=("203.0.113.8", 1))
    second = await _call_asgi(app, client=("203.0.113.8", 1))
    third = await _call_asgi(app, client=("203.0.113.8", 1))
    other = await _call_asgi(app, client=("203.0.113.9", 1))

    assert first["status"] == second["status"] == 401
    assert third["status"] == 429
    assert "Retry-After" in third["headers"] or "retry-after" in third["headers"]
    assert other["status"] == 401


async def test_middleware_leaves_non_http_asgi_scopes_to_the_inner_app() -> None:
    app = bearer_auth_middleware(_pass_app, TokenAuth(token=GOOD))
    result = await _call_asgi(app, scope_type="lifespan")

    assert result["status"] == 204


def test_throttle_opens_after_window_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0

    def monotonic() -> float:
        return now

    monkeypatch.setattr("iot_mcp.auth.time.monotonic", monotonic)
    throttle = TokenThrottle(max_failures=1, window_seconds=10)

    assert throttle.allow("host") is True
    throttle.record_failure("host")
    assert throttle.allow("host") is False

    now += 11
    assert throttle.allow("host") is True
