"""Shared fixtures.

The theme of this suite is that no test may execute a real desktop command: every test
that touches a driver installs :class:`FakeRun` in place of
:func:`iot_mcp.drivers._proc.run`, which records the exact argument vectors the driver
*would* have run and answers from canned output. That lets the suite assert on argv —
which is where the "no shell, no injection" guarantee actually lives — on a machine
that has none of the target tooling.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from iot_mcp.drivers import _linux_wayland as wayland_module
from iot_mcp.drivers import _proc as proc_module
from iot_mcp.drivers import linux as linux_module
from iot_mcp.drivers import macos as macos_module
from iot_mcp.drivers import windows as windows_module
from iot_mcp.drivers._proc import Completed

#: Every module that can put a process on the machine.
GUARDED_MODULES = (linux_module, wayland_module, macos_module, windows_module, proc_module)

#: Executables the drivers look up by name; used to build a believable ``which``.
TOOL_PATHS = {
    "hyprctl": "/usr/bin/hyprctl",
    "playerctl": "/usr/bin/playerctl",
    "busctl": "/usr/bin/busctl",
    "wmctrl": "/usr/bin/wmctrl",
    "xdotool": "/usr/bin/xdotool",
    "xdg-open": "/usr/bin/xdg-open",
    "xdg-mime": "/usr/bin/xdg-mime",
    "rg": "/usr/bin/rg",
    "pgrep": "/usr/bin/pgrep",
    "osascript": "/usr/bin/osascript",
    "open": "/usr/bin/open",
    "powershell.exe": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
}


def make_which(tools: Iterable[str]) -> Any:
    """Build a ``which_fn`` that reports exactly ``tools`` as installed.

    Anything not listed is reported missing, which is how the tests exercise the
    graceful-degradation paths.
    """
    known = set(tools)

    def finder(name: str) -> str | None:
        return TOOL_PATHS.get(name, f"/usr/bin/{name}") if name in known else None

    return finder


ALL_TOOLS = make_which(TOOL_PATHS.keys())

#: First pid handed out by :meth:`FakeRun._spawn`; each subsequent launch takes the next.
SPAWN_PID_BASE = 4242


@dataclass
class RunCall:
    """One recorded subprocess invocation."""

    argv: tuple[str, ...]
    timeout: float | None = None
    stdin: str | None = None
    env: dict[str, str] | None = None

    @property
    def joined(self) -> str:
        return " ".join(self.argv)

    @property
    def program(self) -> str:
        return os.path.basename(self.argv[0]) if self.argv else ""

    def env_value(self, needle: str) -> str | None:
        """First environment value containing ``needle`` (Windows arg channel)."""
        for value in (self.env or {}).values():
            if needle in value:
                return value
        return None


@dataclass
class FakeRun:
    """Stand-in for :func:`iot_mcp.drivers._proc.run`.

    Args:
        rules: Ordered ``(needle, stdout)`` pairs; the first whose ``needle`` appears in
            the joined argv answers the call.
        default: ``(stdout, returncode, stderr)`` used when no rule matches.
    """

    rules: list[tuple[str, Completed]] = field(default_factory=list)
    default: tuple[str, int, str] = ("", 0, "")
    calls: list[RunCall] = field(default_factory=list)
    spawns: list[tuple[RunCall, FakeProcess]] = field(default_factory=list)
    #: What a launched app's stderr tail should say (see :meth:`_spawn_monitored`).
    spawn_stderr_tail: str = ""
    #: When set, a spawned pid is created inside this ``/proc`` stand-in, so a driver that
    #: checks liveness through ``/proc`` sees the fake process really running.
    spawn_proc_root: Path | None = None
    #: Exit status to put on the fake process, for "it died immediately" scenarios.
    spawn_returncode: int | None = None

    def on(self, needle: str, stdout: str = "", *, code: int = 0, stderr: str = "") -> FakeRun:
        """Add a rule answering any argv whose join contains ``needle``."""
        self.rules.append((needle, Completed((), code, stdout, stderr)))
        return self

    def on_json(self, needle: str, payload: Any, *, code: int = 0) -> FakeRun:
        """Add a rule answering with ``payload`` serialised as JSON."""
        import json

        return self.on(needle, json.dumps(payload), code=code)

    async def __call__(self, argv: Sequence[str], **kwargs: Any) -> Completed:
        call = RunCall(
            argv=tuple(str(item) for item in argv),
            timeout=kwargs.get("timeout"),
            stdin=kwargs.get("stdin"),
            env=dict(kwargs["env"]) if kwargs.get("env") is not None else None,
        )
        self.calls.append(call)
        for needle, canned in self.rules:
            if needle in call.joined:
                return Completed(call.argv, canned.returncode, canned.stdout, canned.stderr)
        stdout, code, stderr = self.default
        return Completed(call.argv, code, stdout, stderr)

    # -- assertions helpers -------------------------------------------------------

    def programs(self) -> list[str]:
        return [call.program for call in self.calls]

    def calls_for(self, needle: str) -> list[RunCall]:
        return [call for call in self.calls if needle in call.joined]

    @property
    def spawned_argv(self) -> list[str] | None:
        """Argument vector of the first detached launch, if any."""
        return list(self.spawns[0][0].argv) if self.spawns else None

    @property
    def last(self) -> RunCall:
        assert self.calls, "no subprocess was invoked"
        return self.calls[-1]

    def install(self, monkeypatch: pytest.MonkeyPatch, *modules: Any) -> FakeRun:
        """Replace the ``run``/``spawn_*`` symbols inside each driver module."""
        for module in modules or GUARDED_MODULES:
            monkeypatch.setattr(module, "run", self)
            if hasattr(module, "spawn_detached"):
                monkeypatch.setattr(module, "spawn_detached", self._spawn)
            if hasattr(module, "spawn_monitored"):
                monkeypatch.setattr(module, "spawn_monitored", self._spawn_monitored)
        return self

    def install_everywhere(self, monkeypatch: pytest.MonkeyPatch) -> FakeRun:
        """Patch every module that can launch a process.

        ``LinuxDriver`` imports ``run`` into its own namespace while the window code calls
        it through :mod:`iot_mcp.drivers._linux_wayland`, so a test that patches only the
        driver module would still drive the real compositor.
        """
        return self.install(monkeypatch, *GUARDED_MODULES)

    async def _spawn(self, argv: Sequence[str], **kwargs: Any) -> FakeProcess:
        """Record a detached launch and hand back a process that stays alive."""
        call = RunCall(
            tuple(str(item) for item in argv),
            env=dict(kwargs["env"]) if kwargs.get("env") is not None else None,
        )
        self.calls.append(call)
        process = FakeProcess(
            pid=SPAWN_PID_BASE + len(self.spawns), returncode=self.spawn_returncode
        )
        self.spawns.append((call, process))
        if self.spawn_proc_root is not None:
            # Mirror the process into the fixture's /proc so a driver that reads liveness
            # from there sees the same story as the fake object hands back. An exited child
            # is a zombie until reaped, which is exactly how /proc would report it.
            directory = self.spawn_proc_root / str(process.pid)
            directory.mkdir(parents=True, exist_ok=True)
            state = "Z" if process.returncode is not None else "S"
            (directory / "stat").write_text(f"{process.pid} (iotmcptest) {state} 1 1 0 0\n")
        return process

    async def _spawn_monitored(self, argv: Sequence[str], **kwargs: Any) -> tuple[Any, Any]:
        """Same as :meth:`_spawn`, for the driver path that also captures stderr."""
        process = await self._spawn(argv, **kwargs)
        return process, FakeTail(self.spawn_stderr_tail)


@dataclass
class FakeProcess:
    """The slice of ``asyncio.subprocess.Process`` that the drivers touch."""

    pid: int
    returncode: int | None = None


@dataclass
class FakeTail:
    """Stand-in for :class:`iot_mcp.drivers._proc.StderrTail`."""

    text: str = ""


@pytest.fixture(autouse=True)
def no_real_subprocesses(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Tripwire: no test may launch a real process unless it opts out by marker.

    The window-control code path is the dangerous one — a real ``hyprctl dispatch
    focuswindow`` would move the operator's keyboard focus while the suite runs. The
    drivers import ``run`` into their own namespaces, so every module has to be fenced
    separately; patching only ``iot_mcp.drivers.linux`` leaves
    :mod:`iot_mcp.drivers._linux_wayland` live.

    Opt out with ``@pytest.mark.allow_real_processes`` for the single test that needs a
    real process; a marker is used rather than a fixture because fixture ordering between
    an autouse guard and the fixture that lifts it is not something to rely on.
    """
    if request.node.get_closest_marker("allow_real_processes") is not None:
        return

    def forbidden(argv: Any, **kwargs: Any) -> Any:  # pragma: no cover - tripwire
        raise AssertionError(
            f"a test tried to run a real command: {list(argv)!r}. Install FakeRun "
            "(fake_run() with no arguments patches every driver module)."
        )

    for module in GUARDED_MODULES:
        monkeypatch.setattr(module, "run", forbidden)
        for name in ("spawn_detached", "spawn_monitored"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, forbidden)
        # Last-resort net: anything that reaches asyncio's process API at all.
        monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)


@pytest.fixture
def fake_run(no_real_subprocesses: None, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A :class:`FakeRun` installer; call it with no modules to patch every driver.

    Declares ``no_real_subprocesses`` so the tripwire is always armed first and restored
    afterwards.
    """

    class Installer:
        def __init__(self) -> None:
            self.fakes: list[FakeRun] = []

        def __call__(
            self,
            *modules: Any,
            rules: Mapping[str, str] | None = None,
            default: tuple[str, int, str] = ("", 0, ""),
        ) -> FakeRun:
            fake = FakeRun(default=default)
            for needle, stdout in (rules or {}).items():
                fake.on(needle, stdout)
            fake.install(monkeypatch, *modules)
            self.fakes.append(fake)
            return fake

    return Installer()


@pytest.fixture
def desktop_env() -> Any:
    """Factory for a session environment, with the session bus pointed away from real D-Bus.

    ``DBUS_SESSION_BUS_ADDRESS`` is left unset on purpose: a test that wants the bus probe
    to run sets it explicitly, so no test can talk to the operator's real session bus.
    """

    def build(**overrides: str | None) -> dict[str, str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/tester",
            "USER": "tester",
            "XDG_DATA_DIRS": "/usr/local/share:/usr/share",
            "XDG_DATA_HOME": "/home/tester/.local/share",
            "WAYLAND_DISPLAY": "wayland-1",
            "DISPLAY": "",
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    return build


@pytest.fixture
def tool_bin(tmp_path: Path) -> Any:
    """Create executable stubs on a real PATH and return that PATH.

    The window helpers resolve their tools from the *driver's* ``PATH`` at call time, so a
    test that wants ``wmctrl`` to be "installed" has to put a file on a PATH, not merely
    inject a ``which_fn``. Nothing here is ever executed: FakeRun answers the calls.
    """

    def build(*tools: str) -> str:
        bindir = tmp_path / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        for name in tools:
            stub = bindir / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        return str(bindir)

    return build


@pytest.fixture
def proc_tree(tmp_path: Path) -> Any:
    """Factory for a ``/proc`` stand-in containing the given pids.

    The tree carries the three files the driver reads — ``comm``, ``exe`` and ``cmdline``
    — so path-based process matching is exercised rather than stubbed away.
    """

    def build(
        pids: Iterable[int],
        *,
        names: Mapping[int, str] | None = None,
        exes: Mapping[int, str] | None = None,
    ) -> Path:
        root = tmp_path / "proc"
        root.mkdir(parents=True, exist_ok=True)
        for pid in pids:
            directory = root / str(pid)
            directory.mkdir(parents=True, exist_ok=True)
            name = (names or {}).get(pid, "sleep")
            (directory / "comm").write_text(name + "\n")
            (directory / "cmdline").write_text(f"{name}\0--flag\0")
            target = (exes or {}).get(pid, f"/usr/bin/{name}")
            exe_link = directory / "exe"
            if not exe_link.exists():
                exe_link.symlink_to(target)
        return root

    return build


@pytest.fixture
def desktop_entries(tmp_path: Path) -> Any:
    """Factory that writes ``.desktop`` files into an XDG data directory layout."""

    def build(entries: Mapping[str, Mapping[str, Any]], *, subdir: str = "applications") -> Path:
        root = tmp_path / "share"
        target = root / subdir
        target.mkdir(parents=True, exist_ok=True)
        for app_id, fields in entries.items():
            lines = ["[Desktop Entry]", "Type=Application"]
            for key, value in fields.items():
                lines.append(f"{key}={value}")
            (target / f"{app_id}.desktop").write_text("\n".join(lines) + "\n")
        return root

    return build


@pytest.fixture
def bundle_root(tmp_path: Path) -> Path:
    """Root for macOS-style ``.app`` bundles; ``Contents/Info.plist`` is written per app."""
    root = tmp_path / "Applications"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def info_plist() -> Any:
    """Factory writing a macOS ``Info.plist`` for a bundle."""
    import plistlib

    def write(
        bundle: Path, *, name: str, bundle_id: str, executable: str, hidden: bool = False
    ) -> Path:
        contents = bundle / "Contents"
        contents.mkdir(parents=True, exist_ok=True)
        payload = {
            "CFBundleName": name,
            "CFBundleIdentifier": bundle_id,
            "CFBundleExecutable": executable,
        }
        if hidden:
            payload["LSUIElement"] = "true"
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump(payload, handle)
        return bundle

    return write
