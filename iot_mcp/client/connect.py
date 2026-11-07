"""Discovery-aware MCP client plumbing.

Uses the official SDK's Streamable HTTP client. Two version-compatibility notes, both about
names rather than behaviour:

* the transport function is ``streamable_http_client`` in mcp 2.x and ``streamablehttp_client``
  in 1.x — imported once below so the rest of the package has one name;
* it yields ``(read, write)`` in 2.x and ``(read, write, get_session_id)`` in 1.x — the unpack
  below takes the first two, which is correct for both.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import inspect
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession

from ..config import DEFAULT_MDNS_TYPE
from ..discovery import DiscoveredEndpoint, browse

try:  # mcp >= 2.0
    from mcp.client.streamable_http import streamable_http_client as _http_client
except ImportError:  # pragma: no cover - only taken on mcp 1.x installs
    from mcp.client.streamable_http import streamablehttp_client as _http_client

try:  # present in both lines; lets us set headers on the transport's own HTTP client
    from mcp.shared._httpx_utils import create_mcp_http_client as _create_http_client
except ImportError:  # pragma: no cover - defensive against SDK reshuffles
    _create_http_client = None

log = logging.getLogger("iot_mcp.client")

__all__ = [
    "CallResult",
    "auth_headers",
    "call_tool",
    "connect",
    "describe_exception",
    "list_tools",
    "pick_endpoint",
    "resolve_url",
]


@dataclass(frozen=True)
class CallResult:
    """Normalised result of a tool call.

    Attributes:
        tool: Tool that was called.
        ok: ``False`` when the server reported an execution error (``isError``) or the call
            could not be made at all.
        payload: Structured content when the server provided it, otherwise the decoded text
            content, otherwise the raw text.
        raw: The untouched ``CallToolResult`` fields worth logging.
        images: Paths the ``image`` content blocks were written to, when an image directory was
            given. A terminal cannot render a picture, and a text-only model cannot read one, so
            the honest thing a CLI can do with a screenshot is say where it is.
    """

    tool: str
    ok: bool
    payload: Any
    raw: dict[str, Any]
    images: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view for printing or feeding back to a model."""
        out: dict[str, Any] = {
            "tool": self.tool,
            "ok": self.ok,
            "payload": self.payload,
            "raw": self.raw,
        }
        if self.images:
            out["images"] = list(self.images)
        return out


_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _block_field(block: Any, name: str) -> Any:
    """One field of a content block, whether it arrived as an object or as a mapping."""
    value = getattr(block, name, None)
    if value is None and isinstance(block, Mapping):
        value = block.get(name)
    return value


def image_blocks(result: Any) -> list[Any]:
    """The ``image`` content blocks of a ``CallToolResult`` (possibly empty)."""
    return [
        block
        for block in getattr(result, "content", None) or []
        if _block_field(block, "type") == "image"
    ]


def save_image_blocks(result: Any, dest_dir: str | Path, *, tool: str = "tool") -> list[Path]:
    """Write a call's image blocks to disk and return the paths written.

    Args:
        result: The raw ``CallToolResult`` from the session.
        dest_dir: Directory to write into; created if missing.
        tool: Tool name, used to prefix the file names so a screenshot is recognisable in a
            directory listing.

    Returns:
        Paths written, in block order. Undecodable blocks are skipped rather than failing the
        call — a corrupt image is not a reason to lose the text result.
    """
    blocks = image_blocks(result)
    if not blocks:
        return []
    directory = Path(dest_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", tool)[:60] or "tool"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    written: list[Path] = []
    for index, block in enumerate(blocks):
        data = _block_field(block, "data")
        if not isinstance(data, str) or not data:
            continue
        try:
            payload = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            continue
        mime = str(_block_field(block, "mimeType") or _block_field(block, "mime_type") or "")
        suffix = _IMAGE_EXTENSIONS.get(mime.lower(), ".bin")
        path = directory / f"{stem}-{stamp}-{index:02d}{suffix}"
        path.write_bytes(payload)
        written.append(path)
    return written


def _payload_from(result: Any) -> Any:
    """Pull a usable payload out of a ``CallToolResult``.

    Prefers ``structuredContent`` (what our server returns), falls back to parsing the text
    content as JSON, and finally to the raw text — so the client also works against servers that
    only emit text.
    """
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if structured is not None:
        return structured
    texts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, Mapping):
            text = block.get("text")
        if text:
            texts.append(text)
    joined = "\n".join(texts).strip()
    if not joined:
        return None
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


def describe_exception(exc: BaseException) -> str:
    """Render a connection failure as one readable line.

    The MCP client runs its transport inside an anyio task group, so a plain "HTTP 421 Misunderstood
    Host" or "connection refused" arrives as ``ExceptionGroup: unhandled errors in a TaskGroup`` —
    which tells an operator nothing. This walks the group and the ``__cause__``/``__context__`` chain
    to surface the innermost actionable message.

    Args:
        exc: The exception caught by the caller.

    Returns:
        ``"TypeName: message"``, with nested causes joined by ``" <- "``.
    """
    seen: list[str] = []
    pending: list[BaseException] = [exc]
    budget = 12  # bound the walk: a pathological chain must not loop here
    while pending and budget > 0:
        current = pending.pop(0)
        budget -= 1
        message = str(current).strip() or current.__class__.__name__
        # TaskGroup boilerplate carries no information once its children are shown.
        if "unhandled errors in a TaskGroup" not in message:
            label = f"{type(current).__name__}: {message}"
            if label not in seen:
                seen.append(label)
        for sub in getattr(current, "exceptions", ()) or ():  # ExceptionGroup / TaskGroup
            pending.append(sub)
        for linked in (current.__cause__, current.__context__):
            if linked is not None and linked not in (exc, current):
                pending.append(linked)
    return " <- ".join(seen) or f"{type(exc).__name__}"


async def call_tool(
    session: ClientSession,
    name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    timeout: float = 30.0,
    image_dir: str | Path | None = None,
) -> CallResult:
    """Call one tool on a connected session.

    Args:
        session: An initialized :class:`ClientSession`.
        name: Tool name, e.g. ``"media_status"``.
        arguments: Tool arguments; most of this server's tools take none.
        timeout: Seconds to wait for this call before giving up. A hung server must not hang the
            client.
        image_dir: Where to save ``image`` content blocks. ``None`` drops them (the count is
            still reported in ``raw``), because silently discarding them would look like the
            server never sent a screenshot.

    Returns:
        A :class:`CallResult`. Transport-level failures come back as ``ok=False`` with an
        ``error`` payload rather than raising, so a REPL loop survives them.
    """
    try:
        async with asyncio.timeout(timeout):
            result = await session.call_tool(name, dict(arguments or {}))
    except TimeoutError:
        return CallResult(
            tool=name,
            ok=False,
            payload={
                "ok": False,
                "error": {
                    "type": "client_timeout",
                    "message": f"no response within {timeout:.0f}s",
                },
            },
            raw={},
        )
    except Exception as exc:  # transport/protocol failure reported, not raised
        return CallResult(
            tool=name,
            ok=False,
            payload={
                "ok": False,
                "error": {"type": "transport_error", "message": describe_exception(exc)},
            },
            raw={},
        )
    payload = _payload_from(result)
    is_error = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    # A tool can report ok:false without setting isError (this server does: failures are data),
    # so surface both signals in CallResult.ok.
    payload_failed = isinstance(payload, Mapping) and payload.get("ok") is False
    blocks = image_blocks(result)
    images = save_image_blocks(result, image_dir, tool=name) if image_dir is not None else []
    raw: dict[str, Any] = {"is_error": is_error}
    if blocks:
        raw["image_blocks"] = len(blocks)
        if image_dir is None:
            raw["image_note"] = f"{len(blocks)} image block(s) dropped: no --save-images dir"
    return CallResult(
        tool=name,
        ok=not (is_error or payload_failed),
        payload=payload,
        raw=raw,
        images=tuple(str(path) for path in images),
    )


async def list_tools(session: ClientSession) -> list[dict[str, Any]]:
    """List the server's tools with their schemas and annotations.

    Args:
        session: An initialized :class:`ClientSession`.

    Returns:
        One dict per tool: ``name``, ``description``, ``input_schema``, ``annotations``.
    """
    result = await session.list_tools()
    tools: list[dict[str, Any]] = []
    for tool in result.tools:
        schema = getattr(tool, "input_schema", None)
        if schema is None:  # pragma: no cover - mcp 1.x attribute name
            schema = getattr(tool, "inputSchema", None)
        annotations = getattr(tool, "annotations", None)
        tools.append(
            {
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "input_schema": schema,
                "annotations": annotations.model_dump(exclude_none=True) if annotations else None,
            }
        )
    return tools


def auth_headers(
    token: str | None = None, *, headers: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Merge an explicit bearer token into a header mapping.

    Args:
        token: The shared secret, or ``None`` for an unauthenticated client.
        headers: Additional headers; an ``Authorization`` key here wins over ``token``, so a
            caller can present the secret however its environment expects.

    Returns:
        A new dict (never the caller's), possibly empty.
    """
    merged = dict(headers or {})
    if token and not any(key.lower() == "authorization" for key in merged):
        merged["Authorization"] = f"Bearer {token}"
    return merged


@contextlib.asynccontextmanager
async def connect(
    url: str,
    *,
    token: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> AsyncIterator[ClientSession]:
    """Open a Streamable HTTP MCP session against ``url``.

    Args:
        url: Absolute endpoint, e.g. ``http://192.168.1.20:8000/mcp``. Use
            :func:`resolve_url` to obtain one from mDNS.
        token: Bearer secret for a server started with ``--token``/``--token-file``. Sent as a
            header, never in the query string: the query form is only accepted by servers whose
            operator passed ``--allow-query-token``, precisely because URLs get logged.
        headers: Extra HTTP headers, merged with (and overriding) the token header.

    Yields:
        An initialized session, closed on exit.

    Raises:
        Exception: Connection or handshake failures propagate — callers that want a REPL should
            catch them and re-resolve. A 401 arrives as an exception whose text contains
            ``401``; :func:`describe_exception` unwraps it out of the transport's task group.
    """
    merged = auth_headers(token, headers=headers)
    kwargs: dict[str, Any] = {}
    client: Any = None
    if merged:
        if (
            _create_http_client is not None
            and "http_client" in inspect.signature(_http_client).parameters
        ):
            # mcp 2.x moved header/auth configuration onto the injected HTTP client.
            client = _create_http_client(headers=merged)
            kwargs["http_client"] = client
        else:  # pragma: no cover - mcp 1.x shape
            kwargs["headers"] = merged

    async def _open() -> AsyncIterator[ClientSession]:
        async with _http_client(url, **kwargs) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    if client is not None:
        # The SDK only manages the lifecycle of a client it created itself, so closing ours is
        # this function's job — otherwise every connect leaks a connection pool.
        async with client:
            async for session in _open():
                yield session
    else:
        async for session in _open():
            yield session


def pick_endpoint(
    endpoints: Sequence[DiscoveredEndpoint], instance: str | None = None
) -> DiscoveredEndpoint | None:
    """Choose which discovered service to connect to.

    Args:
        endpoints: Results of :func:`iot_mcp.discovery.browse`.
        instance: Preferred instance (or friendly ``name=`` TXT value), matched
            case-insensitively. When absent or unmatched, the first endpoint wins.

    Returns:
        The chosen endpoint, or ``None`` when nothing was discovered.
    """
    if not endpoints:
        return None
    if instance:
        needle = instance.strip().lower()
        for endpoint in endpoints:
            if needle in {endpoint.instance.lower(), endpoint.name.lower()}:
                return endpoint
        log.debug("no match for %r among %s", instance, [item.instance for item in endpoints])
    return endpoints[0]


async def resolve_url(
    *,
    url: str | None = None,
    mdns_type: str = DEFAULT_MDNS_TYPE,
    browse_timeout: float = 2.0,
    instance: str | None = None,
) -> str:
    """Return the endpoint URL to connect to, via mDNS when not given explicitly.

    Args:
        url: Explicit URL; wins over discovery.
        mdns_type: Service type to browse; defaults to
            :data:`iot_mcp.config.DEFAULT_MDNS_TYPE` (both ends of a connection must agree on it).
        browse_timeout: Seconds to wait for mDNS replies.
        instance: Instance name to prefer when several servers answer.

    Returns:
        An absolute Streamable HTTP URL.

    Raises:
        LookupError: Nothing was advertised (or ``zeroconf`` is unavailable). The message tells
            the operator to start a server or pass ``--url``.
    """
    if url:
        return url.strip()
    endpoints = await asyncio.to_thread(browse, mdns_type, browse_timeout)
    endpoint = pick_endpoint(endpoints, instance)
    if endpoint is None:
        raise LookupError(
            f"no MCP server found via mDNS for {mdns_type!r} within {browse_timeout:.1f}s. "
            "Start one (python -m iot_mcp --expose), check it did not print 'mDNS disabled', or "
            "pass --url http://<host>:8000/mcp."
        )
    log.debug("resolved %s -> %s", endpoint.instance, endpoint.url)
    return endpoint.url
