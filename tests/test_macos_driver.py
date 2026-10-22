"""macOS backend unit tests — this host cannot run macOS, so these tests pin the
contract, the capability matrix, and above all the AppleScript injection defence.

They are honest about what they cannot prove: the AppleScript that *would* be sent to
``osascript`` is captured and asserted on, but whether AppleScript itself behaves as
intended on a real Mac is verified by reading, not execution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import iot_mcp.drivers.macos as macos
from iot_mcp.capabilities import Capability
from iot_mcp.drivers._proc import Completed
from iot_mcp.drivers.macos import MacApp, MacOSDriver, applescript_quote, escape_applescript
from iot_mcp.errors import (
    CommandFailedError,
    DependencyMissingError,
    InvalidInputError,
    NotFoundError,
)

US = "\x1f"


class MacRunner:
    """Answers ``osascript -e <script>`` and ``pgrep -x <name>`` from canned routes.

    Args:
        scripts: Substring → stdout for AppleScript bodies.
        running: Process names ``pgrep`` should report as running.
        failures: Substring → stderr, answered with exit 1 (permission errors etc.).
    """

    def __init__(
        self,
        scripts: dict[str, str] | None = None,
        *,
        running: tuple[str, ...] = (),
        failures: dict[str, str] | None = None,
    ) -> None:
        self.scripts = scripts or {}
        self.running = set(running)
        self.failures = failures or {}
        self.argvs: list[tuple[str, ...]] = []

    @property
    def scripts_sent(self) -> list[str]:
        return [argv[2] for argv in self.argvs if len(argv) > 2 and argv[1] == "-e"]

    def last_script(self, needle: str) -> str:
        for script in reversed(self.scripts_sent):
            if needle in script:
                return script
        raise AssertionError(f"no script containing {needle!r} was sent; got {self.scripts_sent!r}")

    async def __call__(self, argv: Any, **kwargs: Any) -> Completed:
        vector = tuple(str(item) for item in argv)
        self.argvs.append(vector)
        joined = " ".join(vector)
        if vector[0].endswith("pgrep"):
            name = vector[-1]
            if name in self.running:
                return Completed(vector, 0, "4242\n", "")
            return Completed(vector, 1, "", "")
        for needle, stderr in self.failures.items():
            if needle in joined:
                return Completed(vector, 1, "", stderr)
        for needle, stdout in self.scripts.items():
            if needle in joined:
                return Completed(vector, 0, stdout, "")
        return Completed(vector, 0, "", "")


def driver(
    monkeypatch: pytest.MonkeyPatch,
    runner: MacRunner,
    *,
    environ: dict[str, str] | None = None,
    tools: tuple[str, ...] = ("osascript", "open", "pgrep"),
) -> MacOSDriver:
    """A macOS driver whose subprocesses are simulated by ``runner``."""
    from conftest import make_which

    monkeypatch.setattr(macos, "run", runner)
    return MacOSDriver(
        environ=environ or {"HOME": "/Users/tester", "PATH": "/usr/bin"},
        which_fn=make_which(tools),
    )


# --- escaping: the security-critical primitive ---------------------------------


def test_quotes_are_escaped_so_the_caller_cannot_close_the_literal() -> None:
    hostile = '"; tell application "Finder" to quit #'
    escaped = escape_applescript(hostile)
    assert '"' not in escaped.replace('\\"', "")
    assert escaped.startswith('\\"')


def test_line_breaks_and_tabs_cannot_append_new_statements() -> None:
    """Newlines terminate an AppleScript literal, so they are flattened to spaces."""
    assert escape_applescript("a\nb") == "a b"
    assert escape_applescript("a\rb") == "a b"
    assert escape_applescript("a\tb") == "a b"
    assert escape_applescript(f"a{US}b") == "a b"


def test_backslash_is_escaped_first() -> None:
    """Escaping the quote first would turn a literal backslash into an escape sequence."""
    assert escape_applescript("back\\slash") == "back\\\\slash"
    assert escape_applescript('a\\"b') == 'a\\\\\\"b'


def test_nul_bytes_are_stripped() -> None:
    assert "\x00" not in escape_applescript("a\x00b")


def test_non_string_input_is_refused() -> None:
    with pytest.raises(InvalidInputError):
        escape_applescript(None)  # type: ignore[arg-type]


def test_quoting_produces_a_complete_literal() -> None:
    assert applescript_quote("Safari") == '"Safari"'
    assert applescript_quote('Sa"fa') == '"Sa\\"fa"'


async def test_a_hostile_window_title_stays_inside_the_literal(monkeypatch) -> None:
    payload = f'Terminal{US}" & (do shell script "rm -rf /") & "x'
    runner = MacRunner({"System Events": payload + "\n", "frontmost": "Terminal"})
    driver_instance = driver(monkeypatch, runner)

    await driver_instance.focus_window("rm -rf")

    script = runner.last_script("AXRaise")
    # the injected quote is escaped, so it cannot break out of the string
    assert '\\" & (do shell script \\"rm -rf /\\") & \\"x"' in script
    assert "do shell script" in script  # present only as inert literal text


async def test_a_hostile_app_name_cannot_reach_open_as_a_shell_argument(
    monkeypatch, tmp_path
) -> None:
    """``open`` is invoked with argv, never through a shell, so the name is one token."""
    home = tmp_path / "Users" / "tester"
    (home / "Applications").mkdir(parents=True)
    runner = MacRunner({})
    instance = driver(monkeypatch, runner, environ={"HOME": str(home)})
    with pytest.raises(NotFoundError):
        await instance.open_app('Safari"; do shell script "rm -rf /')
    assert not any("-c" in argv for argv in runner.argvs)


# --- capability matrix ---------------------------------------------------------


def test_macos_reports_applescript_backed_capabilities(monkeypatch) -> None:
    instance = driver(monkeypatch, MacRunner())
    matrix = instance.capability_matrix()
    assert matrix["driver"] == "macos"
    for name in (
        "media.play_pause",
        "media.next",
        "media.status",
        "apps.close",
        "windows.list",
        "windows.focus",
    ):
        assert matrix["capabilities"][name]["supported"] is True, name
    assert "AppleScript" in matrix["capabilities"]["media.play_pause"]["reason"]
    assert "Accessibility" in matrix["capabilities"]["windows.list"]["reason"]


def test_a_machine_without_osascript_degrades_every_applescript_capability() -> None:
    instance = MacOSDriver(environ={"HOME": "/Users/tester"}, which_fn=lambda tool: None)
    matrix = instance.capability_matrix()
    for name in (
        "apps.open",
        "apps.close",
        "url.open",
        "media.play_pause",
        "windows.list",
        "windows.focus",
    ):
        assert matrix["capabilities"][name]["supported"] is False, name


def test_app_discovery_reports_the_bundle_roots_it_will_search(tmp_path, info_plist) -> None:
    home = tmp_path / "Users" / "tester"
    (home / "Applications").mkdir(parents=True)
    instance = MacOSDriver(
        environ={"HOME": str(home)},
        which_fn=lambda tool: f"/usr/bin/{tool}" if tool in ("osascript", "open") else None,
    )
    support = instance.support()[Capability.APPS_LIST]
    assert support.supported is True
    assert "~/Applications" in support.details["directories"]


# --- apps ----------------------------------------------------------------------


def test_bundle_discovery_reads_info_plist_and_skips_agents(
    monkeypatch, tmp_path: Path, info_plist
) -> None:
    """The scan is real on any OS: plistlib parses the fixture bundles here."""
    home = tmp_path / "Users" / "tester"
    apps = home / "Applications"
    apps.mkdir(parents=True)
    info_plist(
        apps / "Safari.app", name="Safari", bundle_id="com.apple.Safari", executable="Safari"
    )
    info_plist(
        apps / "Agent.app", name="Agent", bundle_id="com.x.agent", executable="agent", hidden=True
    )
    (apps / "NoPlist.app").mkdir()

    instance = MacOSDriver(
        environ={"HOME": str(home)},
        which_fn=lambda tool: f"/usr/bin/{tool}" if tool in ("osascript", "open") else None,
    )
    found = {app.name: app for app in instance._discover_apps()}
    assert "Safari" in found and found["Safari"].bundle_id == "com.apple.Safari"
    assert "Agent" not in found, "LSUIElement agents have no UI to automate"
    assert "NoPlist" in found, "a bundle with an unreadable plist is still launchable by name"


async def test_open_app_launches_by_bundle_id_without_a_shell(monkeypatch, tmp_path) -> None:
    """``open -b <bundle id>`` is one argv token; there is no shell to interpret anything."""
    home = tmp_path / "Users" / "tester"
    (home / "Applications").mkdir(parents=True)
    runner = MacRunner({})
    instance = driver(monkeypatch, runner, environ={"HOME": str(home)})
    monkeypatch.setattr(
        instance,
        "_discover_apps",
        lambda: [
            MacApp(
                name="Safari",
                bundle_id="com.apple.Safari",
                executable="Safari",
                path="/Applications/Safari.app",
            )
        ],
    )
    result = await instance.open_app("Safari")
    assert runner.argvs[-1][:3] == ("/usr/bin/open", "-b", "com.apple.Safari")
    assert result["ok"] is True and result["app"]["bundle_id"] == "com.apple.Safari"


async def test_close_app_prefers_the_graceful_applescript_quit(monkeypatch) -> None:
    runner = MacRunner({"to quit": ""})
    instance = driver(monkeypatch, runner)
    monkeypatch.setattr(instance, "_discover_apps", lambda: [])

    result = await instance.close_app("TextEdit")
    assert result["method"] == "AppleScript quit"
    assert "quit" in runner.last_script("to quit")


async def test_close_app_falls_back_to_sigterm_when_applescript_refuses(monkeypatch) -> None:
    runner = MacRunner({}, running={"TextEdit"}, failures={"to quit": "-1743: Not authorized"})
    instance = driver(monkeypatch, runner)
    monkeypatch.setattr(instance, "_discover_apps", lambda: [])
    signalled: list[int] = []
    monkeypatch.setattr(macos.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    result = await instance.close_app("TextEdit")
    assert result["method"] == "SIGTERM"
    assert result["terminated_pids"] == [4242]
    assert signalled and signalled[0][1] == macos.signal.SIGTERM


async def test_close_app_of_something_not_running_is_not_found(monkeypatch) -> None:
    runner = MacRunner({}, failures={"to quit": "execution error"})
    instance = driver(monkeypatch, runner)
    monkeypatch.setattr(instance, "_discover_apps", lambda: [])
    with pytest.raises(NotFoundError) as excinfo:
        await instance.close_app("Ghost")
    assert excinfo.value.to_dict()["error"]["details"]["apple_script_result"] == "failed"


# --- media ---------------------------------------------------------------------


async def test_media_commands_drive_the_players_own_dictionary(monkeypatch) -> None:
    runner = MacRunner({"to playpause": ""}, running={"Spotify"})
    instance = driver(monkeypatch, runner)
    result = await instance.media_play_pause()
    assert result["player"] == "Spotify" and result["verb"] == "playpause"
    assert runner.last_script("playpause") == 'tell application "Spotify" to playpause'


async def test_a_player_without_the_verb_reports_which_one_is_missing(monkeypatch) -> None:
    runner = MacRunner({}, running={"QuickTime Player"})
    instance = driver(monkeypatch, runner)
    with pytest.raises(macos.UnsupportedVerbError) as excinfo:
        await instance.media_next()
    assert excinfo.value.details["player"] == "QuickTime Player"
    assert "QuickTime Player" in str(excinfo.value)


async def test_media_without_a_running_player_names_the_supported_ones(monkeypatch) -> None:
    instance = driver(monkeypatch, MacRunner(running=()))
    with pytest.raises(NotFoundError) as excinfo:
        await instance.media_play_pause()
    assert "Spotify" in excinfo.value.hint and "VLC" in excinfo.value.hint


async def test_media_status_reads_state_from_the_player_dictionary(monkeypatch) -> None:
    runner = MacRunner(
        {
            "to get player state": "playing",
            "to get name of current track": "Report",
            "to get artist of current track": "Artist",
        },
        running={"Spotify"},
    )
    instance = driver(monkeypatch, runner)
    result = await instance.media_status()
    assert result["playing"]["status"] == "playing"
    assert result["playing"]["title"] == "Report"
    assert result["playing"]["artist"] == "Artist"


async def test_media_status_without_a_player_is_a_calm_empty_answer(monkeypatch) -> None:
    instance = driver(monkeypatch, MacRunner(running=()))
    result = await instance.media_status()
    assert result["playing"] is None and result["players"] == []
    assert "No supported player" in result["note"]


async def test_a_player_that_errors_mid_status_does_not_break_the_call(monkeypatch) -> None:
    runner = MacRunner(
        {"to get player state": "playing"},
        running={"Spotify"},
        failures={"to get name of current track": "-1728: Can't get name"},
    )
    instance = driver(monkeypatch, runner)
    result = await instance.media_status()
    assert result["playing"]["status"] == "playing"
    assert "title" not in result["playing"], "a missing field is reported as absent, not fatal"


# --- windows -------------------------------------------------------------------


async def test_system_events_output_becomes_the_neutral_window_shape(monkeypatch) -> None:
    payload = f"Finder{US}Desktop\nSafari{US}Example Domain\n"
    runner = MacRunner({"every process": payload, "frontmost is true": "Safari\n"})
    instance = driver(monkeypatch, runner)
    result = await instance.list_windows()
    assert result["backend"] == "system-events"
    assert result["windows"] == [
        {"id": f"Finder{US}Desktop", "app": "Finder", "title": "Desktop", "focused": False},
        {
            "id": f"Safari{US}Example Domain",
            "app": "Safari",
            "title": "Example Domain",
            "focused": True,
        },
    ]


async def test_a_denied_automation_consent_tells_the_operator_where_to_click(monkeypatch) -> None:
    runner = MacRunner(
        {},
        failures={
            "System Events": "osascript: execution error: Not authorized to send Apple events (-1743)"
        },
    )
    instance = driver(monkeypatch, runner)
    with pytest.raises(CommandFailedError) as excinfo:
        await instance.list_windows()
    assert "Privacy & Security" in excinfo.value.hint


async def test_a_machine_without_osascript_names_the_missing_tool(monkeypatch) -> None:
    """Every AppleScript-backed refusal must name osascript, not just say "unsupported"."""
    instance = MacOSDriver(environ={"HOME": "/Users/tester"}, which_fn=lambda tool: None)
    for method, kwargs in (
        ("media_status", {}),
        ("list_windows", {}),
        ("focus_window", {"app_or_title": "x"}),
    ):
        with pytest.raises(DependencyMissingError) as excinfo:
            await getattr(instance, method)(**kwargs)
        assert excinfo.value.to_dict()["error"]["details"]["dependency"] == "osascript", method


async def test_junk_from_system_events_is_not_mistaken_for_windows(monkeypatch) -> None:
    runner = MacRunner({"every process": "no separator here\n\n", "frontmost is true": ""})
    instance = driver(monkeypatch, runner)
    result = await instance.list_windows()
    assert result["count"] == 0
