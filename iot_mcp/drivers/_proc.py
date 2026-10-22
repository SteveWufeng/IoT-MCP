"""Subprocess plumbing shared by all drivers.

This is the only module in the package that talks to ``asyncio``'s process API, so the
"never ``shell=True``, always a deadline, never wedge the event loop" rules are enforced in
exactly one auditable place.

Guarantees provided here:

* Only ``create_subprocess_exec`` is used, with an argument **list**. There is no code path
  that hands a string to a shell, so a tool argument containing ``; rm -rf /`` is data.
* Every call has a timeout. On expiry the child's process group is killed and a typed
  :class:`~iot_mcp.errors.CommandTimeoutError` is raised, so a hung helper (a media player
  that stopped answering D-Bus, say) degrades one tool call instead of freezing the server.
* Captured stdout/stderr are size-capped, because a chatty CLI must not exhaust memory or
  blow up an LLM context.
* Detached children are reaped in the background, so launched GUI apps never become zombies.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import (
    CommandFailedError,
    CommandTimeoutError,
    DependencyMissingError,
    InvalidInputError,
)

__all__ = [
    "Completed",
    "DEFAULT_TIMEOUT",
    "StderrTail",
    "adopt",
    "process_alive",
    "require_tool",
    "run",
    "spawn_detached",
    "spawn_monitored",
    "which",
]

#: Default wall-clock deadline for a helper CLI. Deliberately short: OS automation helpers
#: answer in milliseconds, so anything slower is already pathological.
DEFAULT_TIMEOUT: float = 10.0

#: Hard cap on how much stdout/stderr is retained per stream.
MAX_CAPTURE: int = 32_768

#: Holds strong references to background reaper tasks (asyncio only keeps weak refs).
_REAPERS: set[asyncio.Task[None]] = set()
#: Background stderr drains. Held by strong reference so they are not garbage collected,
#: exactly like the reapers above.
_DRAINS: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class Completed:
    """Result of a finished subprocess.

    Attributes:
        argv: The exact argument vector that was executed (useful in error details).
        returncode: Process exit status.
        stdout: Captured, size-capped standard output.
        stderr: Captured, size-capped standard error.
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the process exited zero."""
        return self.returncode == 0

    @property
    def argv_str(self) -> str:
        """Space-joined command for human-facing messages (never used for execution)."""
        return " ".join(self.argv)

    def lines(self, stream: str = "stdout") -> list[str]:
        """Non-empty, stripped lines of one captured stream.

        Args:
            stream: ``"stdout"`` (default) or ``"stderr"``.
        """
        text = self.stdout if stream == "stdout" else self.stderr
        return [line.strip() for line in text.splitlines() if line.strip()]


def _clip(text: str, limit: int = MAX_CAPTURE) -> str:
    """Truncate ``text`` to ``limit`` bytes-ish, marking that truncation happened."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Reject anything that is not a non-empty vector of ``str``.

    Raises:
        InvalidInputError: ``argv`` is empty or contains a non-string, which would make
            ``create_subprocess_exec`` raise ``TypeError`` deep inside asyncio.
    """
    if not argv:
        raise InvalidInputError("empty argv", hint="Internal error: driver built an empty command.")
    out: list[str] = []
    for item in argv:
        if not isinstance(item, str):
            raise InvalidInputError(
                f"argv items must be str, got {type(item).__name__}",
                hint="Internal error: driver built a non-string argument.",
            )
        if "\x00" in item:
            raise InvalidInputError("argv must not contain NUL bytes")
        out.append(item)
    return tuple(out)


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill a child and, if possible, its whole process group.

    Children we launch with ``start_new_session=True`` own a group, so grandchild
    processes (``playerctl`` spawning nothing, a browser launcher spawning a browser)
    die with them instead of being re-parented and left running.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if hasattr(os, "killpg") and proc.pid is not None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover - non-POSIX fallback
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=2.0)


async def run(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
    capability: str | None = None,
    hint: str | None = None,
) -> Completed:
    """Run ``argv`` without a shell and return its captured output.

    Args:
        argv: Executable followed by its arguments. Passed to ``execvp`` verbatim.
        timeout: Wall-clock deadline in seconds. The child is killed on expiry.
        stdin: Text to feed on stdin (used to hand PowerShell scripts over without any
            shell quoting), or ``None`` for ``/dev/null``.
        env: Full environment mapping for the child; ``None`` inherits the server's.
        capability: Capability name to attach to any raised error.
        hint: Operator-facing remediation text to attach to any raised error.

    Returns:
        A :class:`Completed`; non-zero exits are *not* raised here unless you want them.

    Raises:
        InvalidInputError: ``argv`` is unusable.
        CommandTimeoutError: The child outlived ``timeout`` and was killed.
        FileNotFoundError: The executable does not exist. Callers are expected to have
            probed with :func:`which` first, and translate this into a
            :class:`~iot_mcp.errors.DependencyMissingError` if it can happen.
    """
    vector = _validate_argv(argv)
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if env is not None:
        kwargs["env"] = dict(env)
    if hasattr(os, "setsid"):  # POSIX: own process group so we can kill the tree
        kwargs["start_new_session"] = True

    proc = await asyncio.create_subprocess_exec(*vector, **kwargs)
    try:
        async with asyncio.timeout(timeout):
            raw_out, raw_err = await proc.communicate(
                stdin.encode("utf-8") if stdin is not None else None
            )
    except TimeoutError as exc:
        await _kill_group(proc)
        raise CommandTimeoutError(
            f"{vector[0]!r} did not finish within {timeout:.1f}s and was killed",
            capability=capability,
            hint=hint or "The helper is hung; retry, or check the service it talks to.",
            details={"argv": list(vector), "timeout_seconds": timeout},
        ) from exc

    return Completed(
        argv=vector,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=_clip(raw_out.decode("utf-8", errors="replace")),
        stderr=_clip(raw_err.decode("utf-8", errors="replace")),
    )


def check(
    result: Completed, *, capability: str | None = None, hint: str | None = None
) -> Completed:
    """Raise :class:`~iot_mcp.errors.CommandFailedError` if ``result`` exited non-zero.

    Args:
        result: The result to inspect.
        capability: Capability name to attach to the error.
        hint: Extra remediation text appended after the captured stderr.

    Returns:
        ``result``, so calls can be chained inline.
    """
    if not result.ok:
        message = f"{result.argv_str!r} exited {result.returncode}"
        if result.stderr.strip():
            message = f"{message}: {result.stderr.strip()[:400]}"
        raise CommandFailedError(
            message,
            capability=capability,
            hint=hint,
            details={"argv": list(result.argv), "returncode": result.returncode},
        )
    return result


def which(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    path: str | None = None,
) -> str | None:
    """Locate an executable, or ``None`` if it is not installed.

    Capability detection is *lazy* by design: nothing in this package imports a CLI tool
    at module import time, so a missing binary degrades a capability instead of crashing
    the server at startup.

    Args:
        name: Executable name, e.g. ``"playerctl"``.
        environ: Environment to read ``PATH`` from; defaults to ``os.environ``.
        path: Explicit search path, overriding ``environ["PATH"]``.

    Returns:
        Absolute path to the executable, or ``None``.
    """
    if path is None:
        source = os.environ if environ is None else environ
        path = source.get("PATH", "")
    return shutil.which(name, path=path)


def require_tool(
    name: str,
    *,
    capability: str | None,
    hint: str,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Like :func:`which` but raises a typed error when the tool is absent.

    Args:
        name: Executable name.
        capability: Capability the tool backs.
        hint: Install/start instructions surfaced to the agent.
        environ: Environment used for the ``PATH`` lookup.

    Raises:
        DependencyMissingError: ``name`` is not on ``PATH``.
    """
    found = which(name, environ=environ)
    if found is None:
        raise DependencyMissingError(
            f"{name!r} is not installed or not on PATH",
            capability=capability,
            hint=hint,
            details={"dependency": name},
        )
    return found


async def spawn_detached(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> asyncio.subprocess.Process:
    """Start a long-lived GUI app without blocking, and start reaping it.

    stdout/stderr go to ``DEVNULL`` and the child gets its own session, so an app launched
    through the server survives a server restart and cannot spam our pipes. The returned
    process is adopted immediately; the caller may poll ``returncode`` for a moment to
    detect an immediate exec failure, but never has to wait on it.

    Args:
        argv: Executable and arguments, passed to ``execvp`` verbatim.
        env: Environment for the child; ``None`` inherits the server's.

    Returns:
        The running child process.
    """
    vector = _validate_argv(argv)
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.DEVNULL,
    }
    if env is not None:
        kwargs["env"] = dict(env)
    if hasattr(os, "setsid"):
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(*vector, **kwargs)
    adopt(proc)
    return proc


def adopt(proc: asyncio.subprocess.Process) -> None:
    """Reap ``proc`` in the background so a detached child cannot become a zombie.

    Args:
        proc: A process the caller has decided to stop waiting on.
    """

    async def _reap() -> None:
        with contextlib.suppress(Exception):
            await proc.wait()

    task = asyncio.create_task(_reap())
    _REAPERS.add(task)
    task.add_done_callback(_REAPERS.discard)


#: How much of a launched app's stderr is kept for diagnostics.
STDERR_TAIL_BYTES = 4096


class StderrTail:
    """The last few kilobytes of a child's stderr, kept by a background drain task.

    Launched apps are deliberately not waited on, which means their stderr has to be
    consumed continuously — a GUI app that writes more than the ~64 KiB pipe buffer would
    otherwise *block on write*, and a hung app is precisely what this package promises not
    to create. Only the tail is retained, because the tail is what explains a crash.
    """

    def __init__(self, limit: int = STDERR_TAIL_BYTES) -> None:
        self._limit = max(256, limit)
        self._chunks: deque[str] = deque()
        self._size = 0

    def _feed(self, text: str) -> None:
        """Append one decoded read, dropping from the front to stay within the limit."""
        self._chunks.append(text)
        self._size += len(text)
        while self._size > self._limit and len(self._chunks) > 1:
            self._size -= len(self._chunks.popleft())

    @property
    def text(self) -> str:
        """Captured output so far, newest at the end. Empty when the app said nothing."""
        return "".join(self._chunks).strip()


async def _drain_stderr(stream: asyncio.StreamReader, tail: StderrTail) -> None:
    """Pump ``stream`` into ``tail`` until EOF, never raising into the caller's task."""
    with contextlib.suppress(Exception):
        while chunk := await stream.read(4096):
            tail._feed(chunk.decode("utf-8", "replace"))


async def spawn_monitored(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    tail_bytes: int = STDERR_TAIL_BYTES,
) -> tuple[asyncio.subprocess.Process, StderrTail]:
    """Start a detached child *and* keep its stderr, so a failed launch can be explained.

    Identical to :func:`spawn_detached` except that stderr is captured rather than thrown
    away. "The app exited with 127" is nearly useless to an agent; "error while loading
    shared libraries: libgtk-4.so.1" tells it exactly what is wrong.

    Args:
        argv: Executable and arguments, passed to ``execvp`` verbatim.
        env: Environment for the child; ``None`` inherits the server's.
        tail_bytes: How much stderr to retain.

    Returns:
        The child process and a live view of its stderr tail.
    """
    vector = _validate_argv(argv)
    tail = StderrTail(tail_bytes)
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.PIPE,
        # A single read that is larger than the buffer would raise; the drain loop reads
        # smaller chunks, this only has to be bigger than one of them.
        "limit": max(8192, tail_bytes * 2),
    }
    if env is not None:
        kwargs["env"] = dict(env)
    if hasattr(os, "setsid"):
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(*vector, **kwargs)
    adopt(proc)
    if proc.stderr is not None:
        pump = asyncio.create_task(_drain_stderr(proc.stderr, tail))
        _DRAINS.add(pump)
        pump.add_done_callback(_DRAINS.discard)
    return proc, tail


def process_alive(pid: int, proc_root: str | Path = "/proc") -> str:
    """Classify ``pid`` as ``"alive"``, ``"dead"`` or ``"unknown"`` by reading ``/proc``.

    Three states, because "it is gone" and "I cannot tell" are different answers. This is
    the only liveness check that does not also *wait* for the process, which matters for
    launched GUI apps: ``asyncio`` fills in ``returncode`` only once the child has been
    reaped, and the reaper above runs on its own schedule, so a freshly dead app can still
    report ``None`` there.

    Args:
        pid: Process id to inspect.
        proc_root: Root to read from; injectable so the check is testable off Linux.

    Returns:
        ``"unknown"`` when nothing can be learned here — no ``/proc``, an unreadable root,
        or a path that is not a proc filesystem at all (which includes every test fixture).
    """
    root = Path(proc_root)
    try:
        stat = (root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # Either the process is gone or this was never a proc filesystem. A live pid 1 is
        # what tells the two apart.
        return "dead" if (root / "1").is_dir() else "unknown"
    except OSError:
        return "unknown"
    # Field 3 of /proc/<pid>/stat is the state letter, and the comm field before it may
    # itself contain parentheses — hence splitting on ")" rather than whitespace. A zombie
    # has already exited and only awaits its parent's wait(), so calling it alive would hide
    # exactly the "the app crashed on startup" case this is used to detect.
    fields = stat.split(")")
    state = fields[1].split()[0] if len(fields) > 1 else ""
    return "dead" if state == "Z" else "alive"
