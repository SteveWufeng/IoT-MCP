"""Bridge between an approved stdio MCP add-on and this Streamable HTTP server.

Shape (the one that costs nothing to keep honest): the upstream ``@playwright/mcp`` server
is spawned over stdio with the *official SDK's* ``stdio_client``, an MCP ``ClientSession``
speaks to it, and its tools are re-registered natively on this server as pass-throughs.
No JSON-RPC is hand-rolled: framing, cancellation and process teardown are the SDK's
business, including its bounded, shielded kill sequence — a wedged browser helper cannot
outlive the server.

The subprocess bypasses :mod:`iot_mcp.drivers._proc` by design: ``_proc`` owns *helper CLI*
invocations (short-lived argv→stdout calls); this is a long-lived protocol peer the SDK
owns end-to-end. The compensating control is that the argv can only ever be an
operator-approved fingerprint (:mod:`iot_mcp.addons`), and only the operator's own
``install`` command writes one.

Threading: ``stdio_client`` enters anyio cancellable scopes, so the context managers must
be entered *and exited* inside one task. That task (``_supervise``) pulls operations off a
queue and resolves futures; callers stay on the server's event loop. This is the standard
same-loop worker pattern and keeps every await on the caller's loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .addons import AddonEntry
from .errors import CommandFailedError, DependencyMissingError, InvalidInputError, NotFoundError

log = logging.getLogger("iot_mcp.addons.bridge")

__all__ = ["AddonBridge", "BridgeTool", "STOP"]

#: Sentinel operation that asks the supervisor task to tear the upstream session down.
STOP = "__iot_mcp_stop__"

#: States the bridge moves through; reported verbatim by ``browser_status``.
STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"


@dataclass(frozen=True)
class BridgeTool:
    """One upstream tool, already shaped for native registration.

    Attributes:
        name: Public tool name (namespaced, e.g. ``browser_navigate``).
        upstream_name: Name to call on the upstream session.
        description: Upstream description, verbatim — it is written for models.
        input_schema: Upstream JSON schema, published unchanged.
        annotations: Upstream ``ToolAnnotations`` re-attached unchanged so a client's
            confirmation gating (destructiveHint, ...) keeps working through the bridge.
    """

    name: str
    upstream_name: str
    description: str
    input_schema: dict[str, Any]
    annotations: Any = None


@dataclass
class _Operation:
    """One unit of work handed to the supervisor task."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    future: asyncio.Future[Any] = field(default_factory=lambda: _new_future())


def _new_future() -> asyncio.Future[Any]:
    loop = asyncio.get_running_loop()
    return loop.create_future()


class AddonBridge:
    """A supervised stdio MCP client for one approved add-on.

    Args:
        entry: The operator-approved record; its ``argv`` is the whole trust story.
        prefix: Namespace for re-exposed tool names (``browser`` → ``browser_navigate``).
        request_timeout: Per-call budget for upstream tools — a hung page must degrade one
            call, never the server (same rule as ``_proc.run`` deadlines).
        startup_timeout: Budget for spawn + MCP handshake.
    """

    def __init__(
        self,
        entry: AddonEntry,
        *,
        prefix: str = "browser",
        request_timeout: float = 60.0,
        startup_timeout: float = 30.0,
    ) -> None:
        self._entry = entry
        self._prefix = prefix
        self._request_timeout = request_timeout
        self._startup_timeout = startup_timeout
        self._queue: asyncio.Queue[_Operation] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._session: ClientSession | None = None
        self._state = STATE_IDLE
        self._last_error: str | None = None
        self._tools: tuple[BridgeTool, ...] = ()
        self._started_at: float | None = None

    # ----------------------------------------------------------------------- inspection

    @property
    def name(self) -> str:
        """Add-on key this bridge serves."""
        return self._entry.name

    @property
    def state(self) -> str:
        """One of idle/starting/ready/stopped/failed."""
        return self._state

    @property
    def running(self) -> bool:
        """Whether the upstream session is live."""
        return self._state == STATE_READY

    @property
    def last_error(self) -> str | None:
        """Why the bridge last failed (also what calls report)."""
        return self._last_error

    @property
    def tools(self) -> tuple[BridgeTool, ...]:
        """Upstream tools, populated after a successful start."""
        return self._tools

    def status(self) -> dict[str, Any]:
        """Machine-readable health for ``browser_status``."""
        return {
            "addon": self._entry.name,
            "state": self._state,
            "uptime_seconds": round(time.monotonic() - self._started_at, 1)
            if self._started_at and self.running
            else None,
            "tools": [tool.name for tool in self._tools],
            "last_error": self._last_error,
            "launch": list(self._entry.argv),
            "headless": self._entry.headless,
            "approved_by": self._entry.approved_by,
            "approved_at": self._entry.approved_at,
        }

    # ------------------------------------------------------------------- lifecycle

    async def start(self, *, wait: bool = True) -> None:
        """Spawn the upstream server and complete the MCP handshake.

        Args:
            wait: When true, block until the session is ready (or raise). When false,
                kick off and return; :meth:`ensure_started` and every call still wait.

        Raises:
            DependencyMissingError: Another start is mid-flight (caller should retry) or
                the spawn/handshake failed — the message carries the upstream's complaint.
        """
        if self._state == STATE_READY:
            return
        if self._task is not None and not self._task.done():
            if wait:
                await self._wait_ready(self._startup_timeout)
                return
            return
        self._state = STATE_STARTING
        self._last_error = None
        self._task = asyncio.create_task(self._supervise(), name=f"addon-bridge-{self._entry.name}")
        if wait:
            await self._wait_ready(self._startup_timeout)

    async def ensure_started(self) -> None:
        """Start on first use (lazy mode) and wait for readiness."""
        await self.start(wait=True)

    async def _wait_ready(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._state == STATE_STARTING:
            if self._task is None or self._task.done():
                break
            if asyncio.get_running_loop().time() > deadline:
                await self.stop()
                raise CommandFailedError(
                    f"add-on {self._entry.name!r} did not finish starting within {timeout:.0f}s",
                    capability=f"{self._prefix}.status",
                    hint="The upstream server may be downloading on first run (npx fetches "
                    f"{self._entry.package or 'the package'}); start the server again, or "
                    "run the approved command once by hand to warm it.",
                )
            await asyncio.sleep(0.05)
        if self._state != STATE_READY:
            raise DependencyMissingError(
                f"add-on {self._entry.name!r} is not running: {self._last_error or self._state}",
                capability=f"{self._prefix}.status",
                hint="Check `iot-mcp-addons list`; the approved command may need its runtime "
                "(node/npx) or the browser binary (`npx "
                f"{self._entry.package or '@playwright/mcp@latest'} install chromium`).",
            )

    async def stop(self) -> None:
        """Tear the session down (SDK-managed, bounded). Idempotent."""
        task = self._task
        self._task = None
        if task is None:
            self._session = None
            if self._state in {STATE_READY, STATE_STARTING}:
                self._state = STATE_STOPPED
            return
        with contextlib.suppress(asyncio.QueueFull):
            await self._queue.put(_Operation(kind=STOP))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except Exception as exc:  # noqa: BLE001 - teardown problems are reported, not raised
            log.warning("add-on bridge %s stopped with an error: %s", self._entry.name, exc)
        self._session = None
        if self._state in {STATE_READY, STATE_STARTING}:
            self._state = STATE_STOPPED

    # ------------------------------------------------------------------------ operations

    async def reload_tools(self) -> tuple[BridgeTool, ...]:
        """Fetch upstream ``tools/list`` and reshape for native registration."""
        tools = await self._submit("list", {})
        self._tools = tuple(tools)
        return self._tools

    async def call_upstream(self, upstream_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one upstream tool call and return its result as a JSON-safe dict.

        Raises:
            NotFoundError: The upstream does not expose that tool.
            DependencyMissingError / CommandFailedError: The bridge is not running.
            TimeoutError-family errors surface as CommandFailedError.
        """
        if self._state == STATE_FAILED:
            raise DependencyMissingError(
                f"add-on {self._entry.name!r} crashed: {self._last_error or 'unknown reason'}",
                capability=f"{self._prefix}.status",
                hint="Call browser_start() once to respawn it, or check browser_status.",
            )
        await self.ensure_started()
        if upstream_name not in {tool.upstream_name for tool in self._tools}:
            raise NotFoundError(
                f"upstream add-on has no tool {upstream_name!r}",
                capability=f"{self._prefix}.call",
                hint="Re-check browser_status for the live tool list; upstream versions move.",
            )
        try:
            return await asyncio.wait_for(
                self._submit("call", {"name": upstream_name, "arguments": arguments}),
                timeout=self._request_timeout,
            )
        except TimeoutError as exc:
            raise CommandFailedError(
                f"upstream tool {upstream_name!r} did not answer within "
                f"{self._request_timeout:.0f}s",
                capability=f"{self._prefix}.call",
                hint="The page may be hung; browser_tabs/browser_close can recover the session.",
            ) from exc

    async def _submit(self, kind: str, payload: dict[str, Any]) -> Any:
        if self._task is None or self._task.done():
            raise DependencyMissingError(
                f"add-on {self._entry.name!r} is not running ({self._state})",
                capability=f"{self._prefix}.status",
                hint="Call browser_start() (browser tools auto-start when the add-on is "
                "approved and enabled).",
            )
        operation = _Operation(kind=kind, payload=payload)
        await self._queue.put(operation)
        try:
            outcome = await operation.future
        except (CommandFailedError, DependencyMissingError, InvalidInputError, NotFoundError):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - upstream/transport noise becomes typed
            raise CommandFailedError(
                f"add-on {self._entry.name!r} call failed: {type(exc).__name__}: {exc}",
                capability=f"{self._prefix}.status",
                hint="browser_status shows the bridge state; browser_start respawns a dead one.",
            ) from exc
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    # ---------------------------------------------------------------------- supervisor

    async def _supervise(self) -> None:
        """Own the upstream session inside this task, serving the operation queue."""
        session: ClientSession | None = None
        try:
            params = StdioServerParameters(
                command=self._entry.argv[0],
                args=list(self._entry.argv[1:]),
            )
            async with stdio_client(params) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as client:
                    session = client
                    await asyncio.wait_for(client.initialize(), timeout=self._startup_timeout)
                    listed = await asyncio.wait_for(client.list_tools(), timeout=15.0)
                    self._tools = tuple(_shape_tools(listed, self._prefix))
                    self._session = client
                    self._state = STATE_READY
                    self._started_at = time.monotonic()
                    log.info("add-on %s ready with %d tool(s)", self._entry.name, len(self._tools))
                    await self._serve_queue(client)
        except Exception as exc:  # noqa: BLE001 - any upstream failure is state, not a crash
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._state = STATE_FAILED
            log.warning("add-on %s failed: %s", self._entry.name, self._last_error)
        finally:
            self._session = None
            self._drain_pending()
            if self._state == STATE_READY or self._state == STATE_STARTING:
                self._state = STATE_STOPPED
            _ = session  # the async-with above already closed it inside this task's scope

    async def _serve_queue(self, session: ClientSession) -> None:
        """Pop operations until STOP; a crashed client ends the supervisor."""
        while True:
            operation = await self._queue.get()
            if operation.kind == STOP:
                operation.future.set_result(None)
                return
            try:
                operation.future.set_result(await self._run(session, operation))
            except Exception as exc:  # noqa: BLE001 - the caller sees it on its own future
                if not operation.future.done():
                    operation.future.set_exception(exc)

    async def _run(self, session: ClientSession, operation: _Operation) -> Any:
        if operation.kind == "list":
            listed = await session.list_tools()
            return tuple(_shape_tools(listed, self._prefix))
        if operation.kind == "call":
            result = await session.call_tool(
                str(operation.payload["name"]), dict(operation.payload.get("arguments") or {})
            )
            return _result_to_dict(result)
        raise InvalidInputError(f"unknown bridge operation {operation.kind!r}")

    def _drain_pending(self) -> None:
        """Fail everything still queued so no caller waits on a dead supervisor."""
        while True:
            try:
                operation = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not operation.future.done():
                operation.future.set_exception(
                    DependencyMissingError(
                        f"add-on {self._entry.name!r} stopped while the call was queued",
                        capability=f"{self._prefix}.status",
                        hint="Call browser_start() and retry.",
                    )
                )


def _shape_tools(listed: Any, prefix: str) -> list[BridgeTool]:
    """Convert upstream ``ListToolsResult`` entries into :class:`BridgeTool` records."""
    shaped: list[BridgeTool] = []
    for tool in getattr(listed, "tools", []) or []:
        upstream_name = str(tool.name)
        if not upstream_name.replace("_", "").replace("-", "").isalnum():
            log.warning("skipping add-on tool with an unusable name %r", upstream_name)
            continue
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
        shaped.append(
            BridgeTool(
                name=_namespaced(prefix, upstream_name),
                upstream_name=upstream_name,
                description=str(tool.description or ""),
                input_schema=dict(schema),
                annotations=getattr(tool, "annotations", None),
            )
        )
    return shaped


def _namespaced(prefix: str, upstream_name: str) -> str:
    """``browser`` + ``navigate`` → ``browser_navigate``; ``browser_tab_select`` is untouched.

    Playwright's own names already carry several ``browser_`` prefixes; a second prefix
    would be noise, so a name that already starts with the namespace passes through.
    """
    if upstream_name.startswith(f"{prefix}_"):
        return upstream_name
    return f"{prefix}_{upstream_name}"


def _result_to_dict(result: Any) -> dict[str, Any]:
    """Normalise an upstream ``CallToolResult`` into the project's envelope.

    Content blocks are passed through verbatim (text, images as base64, resources) — the
    browser's answers *are* the payload; rewriting them would lose screenshots. Upstream
    tool errors (``isError``) become ``ok:false`` with the blocks preserved under
    ``content`` so the model still reads the reason.
    """
    blocks: list[dict[str, Any]] = []
    for block in getattr(result, "content", None) or []:
        if isinstance(block, dict):
            blocks.append(block)
        elif hasattr(block, "model_dump"):
            blocks.append(block.model_dump(mode="json", exclude_none=True))
        else:  # pragma: no cover - unknown SDK shape
            blocks.append({"type": "text", "text": str(block)})
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    is_error = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    if is_error:
        text = "\n".join(
            str(block.get("text", "")) for block in blocks if block.get("type") == "text"
        ).strip()
        return {
            "ok": False,
            "error": {
                "type": "upstream_tool_error",
                "message": text or "the browser add-on reported a tool error",
                "hint": "The upstream browser tool failed; the raw content is preserved under "
                "'content'. browser_status shows the live tool list.",
            },
            "content": blocks,
            "structuredContent": structured,
        }
    return {"ok": True, "content": blocks, "structuredContent": structured}
