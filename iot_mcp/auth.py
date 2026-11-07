"""Bearer-token authentication for the Streamable HTTP endpoint.

Design, stated where an auditor will look for it:

* The secret is a single static token, compared in constant time (``hmac.compare_digest``)
  against every presentation the caller is allowed to use. There is no user database, no
  expiry, no rotation protocol — this is the thinnest layer that stops "any TCP peer is an
  operator", which is what made the unauthenticated MVP dangerous on a shared network.
* The token travels in a request header, never in a query string by default: URLs leak into
  access logs, history and ``Referer``. ``--allow-query-token`` exists for embedders that
  genuinely cannot set headers and is loudly documented as the weaker path.
* A rejected request gets a JSON 401 with a *hint*, because an agent that sees a bare 401
  retries forever; it is also throttled per source address so the endpoint is not an
  offline token oracle.
* A token over plain HTTP is visible to anyone who can sniff the path. The honest
  recommendation remains an SSH tunnel; the token raises the bar on a LAN, it does not
  replace transport security.

The middleware wraps the *whole* mounted application (which is mounted at ``/`` by
``MCPServer.streamable_http_app``), so no endpoint of the server is reachable without the
token — including the health/root routes the SDK adds.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_TOKEN_LENGTH",
    "MIN_TOKEN_LENGTH",
    "TOKEN_FILE_RECOMMENDED_MODE",
    "AuthConfigError",
    "TokenAuth",
    "TokenThrottle",
    "bearer_auth_middleware",
    "extract_token",
    "generate_token",
    "token_fingerprint",
    "write_token_file",
]

#: Shortest accepted token. Below this a brute-force search is feasible on a laptop, and
#: the mistake being corrected is almost always a typo'd short secret.
MIN_TOKEN_LENGTH = 24

#: Longest accepted token, bounding header sizes and the memory of the throttle map.
MAX_TOKEN_LENGTH = 256

#: ``0o600``; anything group- or world-readable on the token file earns a warning.
TOKEN_FILE_RECOMMENDED_MODE = 0o600

_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no whitespace


class AuthConfigError(ValueError):
    """The configured token is unusable; the server must refuse to start rather than
    serve a broken or trivially guessable authentication layer."""


def validate_token(token: str, *, source: str = "token") -> str:
    """Normalise and sanity-check a configured token.

    Args:
        token: The secret as configured (flag, environment or file).
        source: Where it came from, used in the error message.

    Returns:
        The token, stripped of surrounding whitespace/newlines (file readers and shells
        add them; the comparison must not).

    Raises:
        AuthConfigError: Empty, too short, too long, or containing whitespace or
            non-printable bytes (which header encoders handle inconsistently).
    """
    candidate = (token or "").strip()
    if not candidate:
        raise AuthConfigError(f"{source} is empty; an empty token must never start the server")
    if len(candidate) < MIN_TOKEN_LENGTH:
        raise AuthConfigError(
            f"{source} is {len(candidate)} characters; the minimum is {MIN_TOKEN_LENGTH}. "
            "Generate one with `python -m iot_mcp --generate-token`."
        )
    if len(candidate) > MAX_TOKEN_LENGTH:
        raise AuthConfigError(
            f"{source} is {len(candidate)} characters; the maximum is {MAX_TOKEN_LENGTH}."
        )
    if not _TOKEN_RE.match(candidate):
        raise AuthConfigError(
            f"{source} must be printable ASCII without whitespace; control characters and "
            "spaces are handled inconsistently across HTTP header encoders."
        )
    return candidate


def generate_token(nbytes: int = 32) -> str:
    """Return a fresh URL-safe secret of at least :data:`MIN_TOKEN_LENGTH` characters."""
    return secrets.token_urlsafe(max(16, min(128, nbytes)))


def token_fingerprint(token: str) -> str:
    """Short non-reversible identifier, safe to print in logs and banners."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def read_token_file(path: str | Path, *, stream: Any = None) -> str:
    """Read a token from ``path`` (first non-empty line), warning on loose permissions.

    Args:
        path: File containing the token.
        stream: Where warnings go; defaults to ``sys.stderr``.

    Returns:
        The validated token.

    Raises:
        AuthConfigError: The file is missing, unreadable, or its content fails
            :func:`validate_token`.
    """
    file = Path(path).expanduser()
    try:
        raw = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuthConfigError(f"cannot read token file {file}: {exc}") from exc
    token = ""
    for line in raw.splitlines():
        if line.strip():
            token = line
            break
    validated = validate_token(token, source=f"token file {file}")
    _warn_if_world_readable(file, stream)
    return validated


def _warn_if_world_readable(file: Path, stream: Any) -> None:
    """Warn (never refuse) when the token file is readable beyond its owner."""
    out = stream or sys.stderr
    try:
        mode = stat.S_IMODE(file.stat().st_mode)
    except OSError:  # pragma: no cover - the read above already succeeded
        return
    if mode & 0o077:
        try:
            out.write(
                f"iot-mcp: warning: token file {file} is mode {mode:o}; "
                f"chmod {TOKEN_FILE_RECOMMENDED_MODE:o} keeps other users from reading it\n"
            )
            out.flush()
        except Exception:  # pragma: no cover - a closed stderr must not kill the server
            pass


def write_token_file(path: str | Path, *, nbytes: int = 32) -> tuple[Path, str]:
    """Create a fresh token file with owner-only permissions. Never overwrites.

    Returns:
        ``(path, token)``.

    Raises:
        AuthConfigError: The file already exists (an operator must delete it deliberately
            rather than have a generated secret silently replace the live one).
    """
    file = Path(path).expanduser()
    if file.exists():
        raise AuthConfigError(
            f"{file} already exists; delete it first if you really mean to replace the token"
        )
    file.parent.mkdir(parents=True, exist_ok=True)
    token = generate_token(nbytes)
    descriptor = os.open(
        str(file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, TOKEN_FILE_RECOMMENDED_MODE
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    try:
        os.chmod(file, TOKEN_FILE_RECOMMENDED_MODE)
    except OSError:  # pragma: no cover - exotic filesystems
        pass
    return file, token


@dataclass
class TokenAuth:
    """Validated server-side authentication settings.

    Attributes:
        token: The shared secret.
        allow_query_token: Also accept ``?token=`` (logged-in-stone weaker; opt-in).
        max_failures: Wrong-token attempts accepted per source per window.
        window_seconds: Sliding window for the throttle.
    """

    token: str
    allow_query_token: bool = False
    max_failures: int = 20
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        self.token = validate_token(self.token, source="IOT_MCP_TOKEN/--token")

    @property
    def fingerprint(self) -> str:
        """Printable identifier for banners and logs; never the secret itself."""
        return token_fingerprint(self.token)

    def verify(self, presented: str | None) -> bool:
        """Constant-time check of a presented secret against the configured one."""
        if not presented:
            return False
        return hmac.compare_digest(presented.encode("utf-8"), self.token.encode("utf-8"))


class TokenThrottle:
    """Sliding-window limiter for failed authentications, keyed by source address.

    Deliberately in-memory and per-process: the MVP has one server process, and a
    persistent ban list would be a state surface nobody audits. When the budget is spent
    the middleware answers 429 with a ``Retry-After``, which also caps log noise.
    """

    def __init__(self, max_failures: int = 20, window_seconds: float = 60.0) -> None:
        self.max_failures = max(1, int(max_failures))
        self.window_seconds = max(1.0, float(window_seconds))
        self._failures: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Whether ``key`` may attempt an authentication right now."""
        now = time.monotonic()
        hits = self._failures.get(key)
        if hits is None:
            return True
        self._prune(hits, now)
        return len(hits) < self.max_failures

    def record_failure(self, key: str) -> None:
        """Note a rejected attempt for ``key``."""
        now = time.monotonic()
        hits = self._failures.setdefault(key, deque())
        self._prune(hits, now)
        hits.append(now)
        # Bound the map: an attacker spraying source addresses must not grow memory.
        if len(self._failures) > 4096:
            for stale in [k for k, v in self._failures.items() if not v]:
                self._failures.pop(stale, None)

    def seconds_until_open(self, key: str) -> float:
        """How long until ``key``'s oldest recorded failure ages out."""
        hits = self._failures.get(key)
        if not hits:
            return 0.0
        self._prune(hits, time.monotonic())
        if len(hits) < self.max_failures:
            return 0.0
        return max(1.0, self.window_seconds - (time.monotonic() - hits[0]))

    def _prune(self, hits: deque[float], now: float) -> None:
        horizon = now - self.window_seconds
        while hits and hits[0] < horizon:
            hits.popleft()


def extract_token(scope: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pull the presented secret out of an ASGI scope.

    Accepted presentations, in order: ``Authorization: Bearer <t>``,
    ``Authorization: <t>``, ``X-Api-Key: <t>``, ``X-IoT-MCP-Token: <t>``, and — only when
    ``allow_query`` was requested by the operator — ``?token=<t>``.

    Returns:
        ``(token, method)`` where ``method`` names the header/query that carried it, for
        logging; ``(None, None)`` when nothing was presented.
    """
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1").strip()
        for key, value in scope.get("headers", []) or []
    }
    authorization = headers.get("authorization", "")
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip(), "authorization:bearer"
        return authorization, "authorization"
    for header in ("x-api-key", "x-iot-mcp-token"):
        value = headers.get(header, "")
        if value:
            return value, header
    query = scope.get("query_string", b"")
    if query:
        from urllib.parse import parse_qs

        values = parse_qs(query.decode("latin-1")).get("token") or []
        if values and bool(scope.get("iot_mcp.allow_query_token")):
            return values[0], "query:token"
        if values:
            # Present but refused: report it so the 401 hint can name the fix precisely.
            return None, "query:token-refused"
    return None, None


def bearer_auth_middleware(app: Any, auth: TokenAuth) -> Any:
    """Wrap an ASGI app so every request must present ``auth.token``.

    Args:
        app: The application to protect (the one returned by
            ``MCPServer.streamable_http_app``).
        auth: Validated settings. The token is never stored in the closure's repr and is
            never echoed back in a response.

    Returns:
        A plain ASGI 3 callable — no framework import, matching the minimal-dependency
        style of this package.
    """
    throttle = TokenThrottle(auth.max_failures, auth.window_seconds)

    async def guarded(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await app(scope, receive, send)
            return
        scope = dict(scope)
        scope["iot_mcp.allow_query_token"] = auth.allow_query_token
        client = scope.get("client")
        key = client[0] if client else "unknown"
        if not throttle.allow(key):
            retry = int(throttle.seconds_until_open(key)) or 1
            await _respond(
                send,
                429,
                {
                    "ok": False,
                    "error": {
                        "type": "rate_limited",
                        "message": "too many failed authentication attempts from this address",
                        "hint": f"Wait {retry}s and retry with the correct token; repeated "
                        "failures keep extending the lockout window.",
                    },
                },
                extra_headers=[(b"retry-after", str(retry).encode("ascii"))],
            )
            return
        presented, method = extract_token(scope)
        if method == "query:token-refused":
            await _respond(
                send,
                401,
                {
                    "ok": False,
                    "error": {
                        "type": "unauthorized",
                        "message": "query-string tokens are not accepted",
                        "hint": "Send 'Authorization: Bearer <token>' instead, or start the "
                        "server with --allow-query-token if you truly cannot set headers.",
                    },
                },
            )
            throttle.record_failure(key)
            return
        if auth.verify(presented):
            await app(scope, receive, send)
            return
        throttle.record_failure(key)
        await _respond(
            send,
            401,
            {
                "ok": False,
                "error": {
                    "type": "unauthorized",
                    "message": "a bearer token is required"
                    if presented is None
                    else "the presented token is not valid",
                    "hint": "Send 'Authorization: Bearer <token>'. The token is the one the "
                    "server was started with (--token / IOT_MCP_TOKEN / --token-file). "
                    "The client accepts --token or IOT_MCP_TOKEN.",
                },
            },
        )

    return guarded


async def _respond(
    send: Any,
    status: int,
    payload: dict[str, Any],
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Send a small JSON response without pulling in a framework."""
    import json

    body = json.dumps(payload).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"www-authenticate", b'Bearer error="invalid_token"'),
        (b"cache-control", b"no-store"),
    ] + (extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
