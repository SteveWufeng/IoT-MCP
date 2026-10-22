"""Timeout, capture-cap and cancellation behaviour of the one subprocess gateway.

:mod:`iot_mcp.drivers._proc` is the only module that puts children on the machine, and
its docstring promises three things: every call has a deadline, capture is size-capped,
and hangs degrade to a typed error instead of wedging the server. This module holds it
to those promises *without ever really spawning*: it swaps the module's own
``asyncio`` reference for a shim whose ``create_subprocess_exec`` answers from inert
fakes, so the tripwire's real-``asyncio`` net stays armed and nothing here can create a
process under any test bug. The function under test is captured at import time, before
any fixture rebinds the module attribute.

The cancellation group is the interesting seam: an MCP client can abort a running
``tools/call`` via ``notifications/cancelled`` (see test_mcp_cancellation.py), which
cancels the surrounding task — what happens to the *child process* in that window?
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

import iot_mcp.drivers._proc as proc_module
from iot_mcp.app import build_server
from iot_mcp.capabilities import ALL_CAPABILITIES
from iot_mcp.drivers.base import AutomationDriver, Support
from iot_mcp.errors import CommandTimeoutError, DriverError

#: The untouched original, grabbed before the tripwire fixture ever rebinds the
#: module attribute. Its ``__globals__`` is the live module dict, so shimming
#: ``proc_module.asyncio`` below controls exactly what this function can reach.
REAL_RUN = proc_module.run
assert getattr(REAL_RUN, "__module__", None) == "iot_mcp.drivers._proc"

MAX = proc_module.MAX_CAPTURE


class HangProc:
    """A child that never produces output and never exits (the hung-helper model)."""

    def __init__(self) -> None:
        self.kill_calls = 0
        self.waits = 0
        self.communicated = asyncio.Event()
        self.returncode: int | None = None
        #: A pid that cannot exist, so _kill_group's getpgid hits ProcessLookupError.
        self.pid = 2**30

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        self.communicated.set()
        await asyncio.sleep(3600)  # cancelled by the deadline or by task cancellation
        return b"", b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.waits += 1
        return -9


class DoneProc:
    """A child that exits immediately with canned byte streams."""

    def __init__(self, out: bytes = b"", err: bytes = b"", code: int = 0) -> None:
        self.out, self.err, self.code = out, err, code
        self.stdin_bytes: bytes | None = None
        self.returncode: int | None = None
        self.pid = 2**30 + 1

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_bytes = payload
        self.returncode = self.code
        return self.out, self.err

    def kill(self) -> None:  # pragma: no cover - never killed in these tests
        raise AssertionError("this child exits instantly; nothing should kill it")

    async def wait(self) -> int:  # pragma: no cover
        return self.code


def shim(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    """Replace only the ``asyncio`` handle ``_proc`` can see; spawn becomes ``factory``.

    ``factory(*argv, **kwargs)`` returns the fake process *object*; the shim awaits
    it into existence, matching how ``create_subprocess_exec`` is really used.
    """

    async def create_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
        return factory(*args, **kwargs)

    monkeypatch.setattr(
        proc_module,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=create_subprocess_exec,
            subprocess=asyncio.subprocess,
            timeout=asyncio.timeout,
            wait_for=asyncio.wait_for,
        ),
    )


# ------------------------------------------------------------------ deadline


async def test_a_hung_helper_is_killed_and_reported_as_a_typed_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The promised deadline is real: expiry raises CommandTimeoutError, with context."""
    child = HangProc()
    shim(monkeypatch, lambda *a, **k: child)
    started = time.monotonic()
    with pytest.raises(CommandTimeoutError) as excinfo:
        await REAL_RUN(["iotmcpfake", "--hang"], timeout=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, "the timeout fired, not the child"
    body = excinfo.value.to_dict()
    assert body["error"]["type"] == "timeout"
    assert body["error"]["details"]["timeout_seconds"] == 0.2
    assert body["error"]["details"]["argv"] == ["iotmcpfake", "--hang"]
    assert child.waits >= 1, "the killed child was reaped, not left a zombie"


async def test_a_timeout_on_one_call_does_not_wedge_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a kill, the next run through the same code path still works."""
    children = [HangProc(), DoneProc(out=b"fine\n")]
    shim(monkeypatch, lambda *a, **k: children.pop(0))
    with pytest.raises(CommandTimeoutError):
        await REAL_RUN(["iotmcpfake"], timeout=0.2)
    done = await REAL_RUN(["iotmcpfake", "--ok"], timeout=1)
    assert done.ok and done.stdout == "fine\n"


# ------------------------------------------------------------------ capture caps


async def test_oversized_stdout_is_clipped_at_max_capture_with_a_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chatty CLI must not blow up memory or the LLM context."""
    flood = b"A" * 200_000
    shim(monkeypatch, lambda *a, **k: DoneProc(out=flood))
    result = await REAL_RUN(["iotmcpfake"], timeout=1)
    assert result.stdout.startswith("A" * 100)
    assert "[truncated 167232 chars]" in result.stdout
    assert len(result.stdout) < 200_000


async def test_oversized_stderr_is_clipped_as_well(monkeypatch: pytest.MonkeyPatch) -> None:
    shim(monkeypatch, lambda *a, **k: DoneProc(err=b"E" * 100_000, code=3))
    result = await REAL_RUN(["iotmcpfake"], timeout=1)
    assert "[truncated" in result.stderr
    assert len(result.stderr) < 100_000
    assert result.returncode == 3


async def test_undecodable_output_never_raises_it_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid UTF-8 becomes U+FFFD, not a codec crash inside a tool call."""
    shim(monkeypatch, lambda *a, **k: DoneProc(out=b"ok\xff\xfe"))
    result = await REAL_RUN(["iotmcpfake"], timeout=1)
    assert result.stdout.startswith("ok")


async def test_stdin_scripts_are_delivered_as_utf8_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PowerShell script channel: text in, exact UTF-8 bytes out — no quoting."""
    child = DoneProc()
    shim(monkeypatch, lambda *a, **k: child)
    await REAL_RUN(["powershell.exe", "-Command", "-"], stdin="Write-Output '✓'", timeout=1)
    assert child.stdin_bytes == "Write-Output '✓'".encode()


# ------------------------------------------------------------- cancellation seam


async def test_cancelling_a_run_leaves_the_child_running_current_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the fact: an external task cancellation (what notifications/cancelled
    triggers upstream) cancels the ``communicate`` await but does NOT kill the child.
    ``asyncio.timeout`` expiry kills via the group; caller cancellation does not.
    """
    child = HangProc()
    shim(monkeypatch, lambda *a, **k: child)
    task = asyncio.create_task(REAL_RUN(["iotmcpfake"], timeout=30))
    await asyncio.wait_for(child.communicated.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.kill_calls == 0, "fact: nothing killed the orphan"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "finding: _proc.run kills children on its own deadline but not on external "
        "cancellation, so a cancelled tools/call orphans the helper process"
    ),
)
async def test_cancelling_a_run_should_kill_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = HangProc()
    shim(monkeypatch, lambda *a, **k: child)
    task = asyncio.create_task(REAL_RUN(["iotmcpfake"], timeout=30))
    await asyncio.wait_for(child.communicated.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.kill_calls >= 1, "the child should have been killed"


# ------------------------------------------------------------ envelope honesty


class FlakyDriver(AutomationDriver):
    """Every capability raises; the exception type is parameterised per call."""

    name = "flaky"

    def __init__(self, error: Exception) -> None:
        self.error = error

    def _probe(self) -> dict[Any, Support]:
        return {capability: Support.yes("test double") for capability in ALL_CAPABILITIES}

    async def _boom(self) -> dict[str, Any]:  # pragma: no cover - never returns
        raise self.error

    async def list_apps(self) -> dict[str, Any]:
        return await self._boom()

    async def open_app(self, name: str) -> dict[str, Any]:
        return await self._boom()

    async def close_app(self, name: str) -> dict[str, Any]:
        return await self._boom()

    async def open_url(self, url: str) -> dict[str, Any]:
        return await self._boom()

    async def media_play_pause(self) -> dict[str, Any]:
        return await self._boom()

    async def media_next(self) -> dict[str, Any]:
        return await self._boom()

    async def media_previous(self) -> dict[str, Any]:
        return await self._boom()

    async def media_status(self) -> dict[str, Any]:
        return await self._boom()

    async def list_windows(self) -> dict[str, Any]:
        return await self._boom()

    async def focus_window(self, app_or_title: str) -> dict[str, Any]:
        return await self._boom()


async def _payload(server: Any, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    import json

    result = await server.call_tool(name, args or {})
    assert result.content and result.content[0].type == "text"
    return json.loads(result.content[0].text)


async def test_a_timeout_reaches_the_agent_as_typed_error_data_not_a_crash() -> None:
    server = build_server(
        FlakyDriver(CommandTimeoutError("playerctl hung", capability="media.status"))
    )
    payload = await _payload(server, "media_status")
    assert payload["ok"] is False
    assert payload["error"]["type"] == "timeout"
    assert payload["error"]["capability"] == "media.status"
    assert "Traceback" not in payload["error"]["message"]


async def test_an_unexpected_crash_becomes_internal_error_naming_the_exception() -> None:
    """Pin the current contract: unexpected exceptions are converted (session safe)
    but the type name and message ARE reflected to the caller — an unauthenticated
    protocol surface should not advertise internal exception classes. (Finding.)
    """
    server = build_server(FlakyDriver(ValueError("bad value at apps.py:42")))
    payload = await _payload(server, "list_apps")
    assert payload["ok"] is False
    assert payload["error"]["type"] == "internal_error"
    assert "ValueError" in payload["error"]["message"]
    assert "Traceback" not in payload["error"]["message"]


@pytest.mark.xfail(
    strict=True,
    reason="finding: internal_error reflects the python exception type to unauthenticated callers",
)
async def test_internal_errors_should_not_leak_exception_class_names() -> None:
    server = build_server(FlakyDriver(ValueError("secret internal detail")))
    payload = await _payload(server, "list_apps")
    assert "ValueError" not in payload["error"]["message"]
    assert "secret internal detail" not in payload["error"]["message"]


async def test_typed_driver_errors_never_leak_python_types() -> None:
    server = build_server(FlakyDriver(DriverError("plain failure", hint="try another capability")))
    payload = await _payload(server, "list_apps")
    assert payload["error"]["type"] == "driver_error"
    assert payload["error"]["hint"] == "try another capability"
    assert "FlakyDriver" not in str(payload) and "Traceback" not in str(payload)
