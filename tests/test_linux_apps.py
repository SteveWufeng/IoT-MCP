"""Linux app lifecycle: discovery through launch through termination.

Two kinds of test live here. Most use :class:`FakeRun` so the exact argv can be asserted
without touching the machine. One — :func:`test_a_launched_process_is_actually_terminated`
— really launches and really kills a process, because the SIGTERM/SIGKILL path is the one
place where a subtly wrong implementation is invisible in a mock.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path
from typing import Any

import pytest

import iot_mcp.drivers.linux as linux_module
from iot_mcp.drivers.linux import LinuxDriver
from iot_mcp.errors import (
    CommandFailedError,
    DependencyMissingError,
    InvalidInputError,
    NotFoundError,
    UnsupportedCapabilityError,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux/POSIX process semantics")


@pytest.fixture
def apps_env(desktop_entries, tmp_path: Path) -> dict[str, Any]:
    """A private XDG tree containing a real launchable app plus two decoys."""
    root = desktop_entries(
        {
            "iotmcptest-sleeper": {
                "Name": "IoT MCP Test Sleeper",
                "Exec": "PLACEHOLDER %U",
                "StartupWMClass": "iotmcptest-sleeper",
            },
            "iotmcptest-nobinary": {
                "Name": "IoT MCP Test NoBinary",
                "Exec": "iotmcptest-definitely-not-installed",
            },
            "iotmcptest-noexec": {"Name": "IoT MCP Test NoExec"},
        },
        subdir="applications",
    )
    # A uniquely named copy of `sleep` so the test can never hit a real user process.
    binary = tmp_path / "iotmcptest-sleeper"
    shutil.copy(shutil.which("sleep") or "/bin/sleep", binary)
    entry = root / "applications" / "iotmcptest-sleeper.desktop"
    entry.write_text(entry.read_text().replace("PLACEHOLDER %U", f"{binary} 300"))
    return {
        "dirs": [root / "applications"],
        "binary": binary,
        "environ": {
            "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "XDG_DATA_HOME": str(root / "unset"),
            "XDG_DATA_DIRS": str(root),
        },
    }


#: A path that is not a proc filesystem, so `process_alive()` reports "unknown" and the
#: driver falls back to `returncode`. Unit tests must not read the host's real /proc: their
#: fake pids would be judged dead by it.
NO_PROC = "/iotmcp-tests/no-proc"


def build(
    environ: dict[str, str],
    tools: tuple[str, ...] = (),
    proc_root: str | Path = NO_PROC,
) -> LinuxDriver:
    """A driver whose app scan is pointed at nothing unless the test supplies a tree.

    Without this the driver would scan the host's real ``/usr/share/applications`` on
    every call, which is slow and makes results depend on the machine running the tests.
    """
    from conftest import make_which

    hermetic = {
        "XDG_DATA_HOME": "/iotmcp-tests/none",
        "XDG_DATA_DIRS": "/iotmcp-tests/none",
    } | environ
    return LinuxDriver(environ=hermetic, which_fn=make_which(tools), proc_root=proc_root)


# --- list_apps -----------------------------------------------------------------


async def test_list_apps_reports_what_it_searched_and_found(
    apps_env, desktop_env, fake_run
) -> None:
    fake_run(linux_module, rules={})
    driver = build(apps_env["environ"], tools=("xdg-open",))
    result = await driver.list_apps()
    assert result["ok"] is True
    names = {app["name"] for app in result["apps"]}
    assert {"IoT MCP Test Sleeper", "IoT MCP Test NoBinary", "IoT MCP Test NoExec"} <= names
    assert result["count"] == len(result["apps"])
    assert str(apps_env["dirs"][0]) in " ".join(result["searched_directories"])
    assert all(app["id"] and app["name"] for app in result["apps"])


# --- open_app: the argv guarantee ---------------------------------------------


async def test_open_app_launches_the_exec_without_a_shell(apps_env, fake_run) -> None:
    """No shell exists anywhere in this path, so no metacharacter can be interpreted."""
    fake = fake_run(linux_module)
    driver = build(apps_env["environ"], tools=("xdg-open",))

    result = await driver.open_app("iotmcptest-sleeper")

    assert result["ok"] is True
    assert fake.spawned_argv[0] == str(apps_env["binary"])
    assert fake.spawned_argv[1:] == ["300"]
    assert not any(token in " ".join(fake.spawned_argv) for token in ("-c", "/bin/sh", "%U"))
    assert result["still_running"] is True


@pytest.fixture
def live_proc(proc_tree) -> Any:
    """Factory for a ``/proc`` stand-in the driver will trust as authoritative.

    A proc root is only authoritative if it contains a live pid 1; without that, a missing
    pid directory proves nothing (see :func:`iot_mcp.drivers._proc.process_alive`).
    """

    def build(*pids: int) -> Path:
        root = proc_tree([1, *pids])
        for pid in (1, *pids):
            (root / str(pid) / "stat").write_text(f"{pid} (init) S 0 0 0 0\n")
        return root

    return build


async def test_launch_liveness_is_read_from_proc(apps_env, fake_run, live_proc) -> None:
    """``/proc`` decides ``still_running``, not asyncio's ``returncode``.

    ``returncode`` is only filled in once the child has been reaped, and launched GUI apps
    are deliberately not waited on, so it would report ``None`` forever for a healthy app.
    """
    fake = fake_run(linux_module)
    root = live_proc()
    fake.spawn_proc_root = root
    driver = build(apps_env["environ"], tools=("xdg-open",), proc_root=root)

    result = await driver.open_app("iotmcptest-sleeper")

    assert result["still_running"] is True


async def test_a_launch_that_already_vanished_is_not_reported_running(
    apps_env, fake_run, live_proc
) -> None:
    """A pid missing from a real proc root is dead, even with ``returncode is None``."""
    fake = fake_run(linux_module)
    root = live_proc()  # the spawned pid is deliberately never added to it
    driver = build(apps_env["environ"], tools=("xdg-open",), proc_root=root)

    result = await driver.open_app("iotmcptest-sleeper")

    assert fake.spawned_argv is not None, "the launch was attempted"
    assert result["still_running"] is False


async def test_an_app_that_crashes_on_startup_fails_with_its_stderr(
    apps_env, fake_run, live_proc
) -> None:
    """The case a ``returncode``-only check cannot see: dead before asyncio reaped it.

    "exited with code 127" is useless to an agent; the stderr line that says a shared
    library is missing is the actual answer.
    """
    fake = fake_run(linux_module)
    root = live_proc()
    fake.spawn_proc_root = root
    fake.spawn_returncode = 127
    fake.spawn_stderr_tail = "error while loading shared libraries: libgtk-4.so.1"
    driver = build(apps_env["environ"], tools=("xdg-open",), proc_root=root)

    with pytest.raises(CommandFailedError) as excinfo:
        await driver.open_app("iotmcptest-sleeper")

    details = excinfo.value.to_dict()["error"]["details"]
    assert details["returncode"] == 127
    assert "libgtk-4" in details["stderr"]
    assert details["argv"][0].endswith("iotmcptest-sleeper")


async def test_open_app_rejects_an_entry_whose_binary_is_missing(apps_env, fake_run) -> None:
    fake = fake_run(linux_module)
    driver = build(apps_env["environ"], tools=("xdg-open",))
    with pytest.raises(NotFoundError) as excinfo:
        await driver.open_app("IoT MCP Test NoBinary")
    assert "not on PATH" in str(excinfo.value)
    assert fake.spawned_argv is None


async def test_open_app_rejects_an_entry_with_no_exec_line(apps_env, fake_run) -> None:
    fake_run(linux_module)
    driver = build(apps_env["environ"], tools=("xdg-open",))
    with pytest.raises(InvalidInputError) as excinfo:
        await driver.open_app("IoT MCP Test NoExec")
    assert "Exec" in str(excinfo.value)


async def test_open_app_of_an_unknown_name_returns_candidates(apps_env, fake_run) -> None:
    fake_run(linux_module)
    driver = build(apps_env["environ"], tools=("xdg-open",))
    with pytest.raises(NotFoundError) as excinfo:
        await driver.open_app("definitely-not-an-app")
    details = excinfo.value.to_dict()["error"]["details"]
    assert "IoT MCP Test Sleeper" in details["candidates"]


async def test_a_vanished_binary_becomes_a_typed_error(apps_env, fake_run, monkeypatch) -> None:
    """``which`` then ``exec`` is a race; a mid-flight uninstall must not raise OSError."""
    fake = fake_run(linux_module)
    monkeypatch.setattr(linux_module, "spawn_monitored", _raise_file_not_found)
    driver = build(apps_env["environ"], tools=("xdg-open",))
    with pytest.raises(NotFoundError) as excinfo:
        await driver.open_app("iotmcptest-sleeper")
    assert excinfo.value.code == "not_found"
    assert fake.spawned_argv is None


async def _raise_file_not_found(argv, **kwargs):
    raise FileNotFoundError(argv[0])


# --- open_url ------------------------------------------------------------------


async def test_open_url_hands_one_validated_url_to_xdg_open(desktop_env, fake_run) -> None:
    fake = fake_run(linux_module)
    driver = build(desktop_env(), tools=("xdg-open",))
    result = await driver.open_url("https://example.com/a?b=1")
    assert result["ok"] is True and result["launcher"] == "xdg-open"
    assert fake.spawned_argv == ["/usr/bin/xdg-open", "https://example.com/a?b=1"]


async def test_open_url_refuses_a_dangerous_scheme_before_touching_a_launcher(
    desktop_env, fake_run
) -> None:
    fake = fake_run(linux_module)
    driver = build(desktop_env(), tools=("xdg-open",))
    with pytest.raises(InvalidInputError):
        await driver.open_url("file:///etc/shadow")
    assert fake.calls == []


async def test_open_url_without_xdg_open_is_a_dependency_error(desktop_env, fake_run) -> None:
    fake_run(linux_module)
    driver = build(desktop_env(), tools=())
    matrix = driver.capability_matrix()
    assert matrix["capabilities"]["url.open"]["supported"] is False
    assert matrix["capabilities"]["url.open"]["details"]["dependency"] == "xdg-open"
    with pytest.raises(DependencyMissingError):
        await driver.open_url("https://example.com")


# --- close_app -----------------------------------------------------------------


async def test_close_app_signals_the_matching_pids(proc_tree, desktop_env) -> None:
    root = proc_tree(
        [4242, 4243, 9999], names={4242: "iotmcptest", 4243: "iotmcptest", 9999: "other"}
    )
    driver = build(desktop_env(), proc_root=root)
    signalled: list[int] = []
    alive: set[int] = {4242, 4243, 9999}

    def fake_kill(pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            signalled.append(pid)
            alive.discard(pid)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(os, "kill", fake_kill)
    monkey.setattr(LinuxDriver, "_is_alive", lambda self, pid: pid in alive)
    try:
        result = await driver.close_app("iotmcptest")
    finally:
        monkey.undo()

    assert sorted(signalled) == [4242, 4243]
    assert result["terminated_pids"] == [4242, 4243]
    assert result["force_killed_pids"] == []
    assert result["count"] == 2


async def test_close_app_force_kills_survivors(proc_tree, desktop_env) -> None:
    root = proc_tree([4242], names={4242: "iotmcptest"})
    driver = build(desktop_env(), proc_root=root)
    signalled: list[int] = []

    monkey = pytest.MonkeyPatch()
    monkey.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    monkey.setattr(LinuxDriver, "_is_alive", lambda self, pid: True)  # never dies
    monkey.setattr(linux_module, "_TERMINATE_GRACE_SECONDS", 0.05)
    try:
        result = await driver.close_app("iotmcptest")
    finally:
        monkey.undo()

    assert (4242, signal.SIGTERM) in signalled
    assert (4242, signal.SIGKILL) in signalled
    assert result["force_killed_pids"] == [4242]


async def test_close_app_never_signals_itself(proc_tree, desktop_env) -> None:
    """A request to close "python" must not take the MCP server down with it."""
    root = proc_tree(
        [os.getpid(), os.getppid(), 1, 4242], names={4242: "python", os.getpid(): "python"}
    )
    driver = build(desktop_env(), proc_root=root)
    signalled: list[int] = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(os, "kill", lambda pid, sig: signalled.append(pid))
    monkey.setattr(LinuxDriver, "_is_alive", lambda self, pid: False)
    try:
        await driver.close_app("python")
    finally:
        monkey.undo()
    assert signalled == [4242]


async def test_close_app_of_a_name_that_is_not_running_is_not_found(proc_tree, desktop_env) -> None:
    driver = build(desktop_env(), proc_root=proc_tree([4242], names={4242: "firefox"}))
    with pytest.raises(NotFoundError) as excinfo:
        await driver.close_app("gimp")
    assert excinfo.value.to_dict()["error"]["details"]["looked_for"]
    assert "list_windows()" in excinfo.value.hint


@pytest.mark.parametrize("name", ["", "   ", "\n"])
async def test_close_app_rejects_an_empty_name(name: str, desktop_env, tmp_path: Path) -> None:
    # A readable (but empty) proc root: the capability probe requires one, and the input
    # check must fire before anything is looked up in it.
    driver = build(desktop_env(), proc_root=tmp_path)
    with pytest.raises(InvalidInputError):
        await driver.close_app(name)


async def test_a_long_process_name_still_matches_the_kernel_truncated_comm(
    proc_tree, desktop_env
) -> None:
    """``/proc/<pid>/comm`` is cut to 15 chars, so that is what must be compared.

    Matching stays exact against the truncated form: a short query must not sweep up every
    process that merely shares a prefix.
    """
    long_name = "iotmcptestverylongbinary"  # kernel reports comm as the first 15 characters
    root = proc_tree(
        [4242, 4243],
        names={4242: long_name[:15], 4243: "iotmcptestother"},
    )
    driver = build(desktop_env(), proc_root=root)
    signalled: list[int] = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(os, "kill", lambda pid, sig: signalled.append(pid))
    monkey.setattr(LinuxDriver, "_is_alive", lambda self, pid: False)
    try:
        result = await driver.close_app(long_name)
        signalled.clear()
        too_short = await _try_close(driver, "iotmcp")  # shares a 6-character prefix
    finally:
        monkey.undo()
    assert result["terminated_pids"] == [4242]
    assert too_short is False, "matching is equality against the truncated comm, never a prefix"
    assert signalled == [], "a prefix query must not sweep up every similarly named process"


async def _try_close(driver: LinuxDriver, name: str) -> bool:
    from iot_mcp.errors import DriverError

    try:
        await driver.close_app(name)
    except DriverError:
        return False
    return True


# --- the real round trip -------------------------------------------------------


@pytest.mark.allow_real_processes
async def test_a_launched_process_is_actually_terminated(apps_env, tmp_path: Path) -> None:
    """End to end on the real kernel: spawn, find by name, terminate, confirm it is gone.

    This is the only test in the suite that creates a real process. It uses a uniquely
    named copy of ``sleep`` so it can never hit anything belonging to the operator. It is
    also the only test that opts out of the suite-wide ``no_real_subprocesses`` tripwire,
    via ``@pytest.mark.allow_real_processes``.
    """
    driver = build(apps_env["environ"], tools=("xdg-open",), proc_root="/proc")
    launched = await driver.open_app("iotmcptest-sleeper")
    pid = launched["pid"]
    assert launched["still_running"] is True

    try:
        result = await driver.close_app("iotmcptest-sleeper")
    finally:
        # never leave an orphan behind if an assertion below fails
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    assert pid in result["terminated_pids"]
    # give the kernel a moment, then require the pid to be genuinely gone
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"pid {pid} survived close_app")

    with pytest.raises(NotFoundError):
        await driver.close_app("iotmcptest-sleeper")


async def test_unsupported_when_nothing_supports_apps(desktop_env, fake_run) -> None:
    """An empty XDG tree means the app capabilities are honestly reported as unavailable."""
    fake_run(linux_module)
    driver = LinuxDriver(
        environ={
            **desktop_env(),
            "XDG_DATA_HOME": "/nonexistent-a",
            "XDG_DATA_DIRS": "/nonexistent-b",
        },
        which_fn=lambda tool: None,
    )
    matrix = driver.capability_matrix()
    assert matrix["capabilities"]["apps.list"]["supported"] is False
    with pytest.raises(UnsupportedCapabilityError):
        await driver.list_apps()
