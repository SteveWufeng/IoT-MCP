"""Windows backend unit tests.

This host cannot run Windows. What *is* executed here is the part that carries the
security property: caller-supplied text must never appear inside the PowerShell script
that is piped to stdin, only inside ``IOT_MCP_ARG<n>`` environment values. The PowerShell
bodies themselves (``EnumWindows``, ``SendInput``, Start Menu enumeration) are reviewed by
reading, not verified.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import iot_mcp.drivers.windows as windows
from iot_mcp.capabilities import Capability
from iot_mcp.drivers._proc import Completed
from iot_mcp.drivers.windows import WindowsDriver, quote_single
from iot_mcp.errors import (
    CommandFailedError,
    DependencyMissingError,
    InvalidInputError,
    NotFoundError,
    UnsupportedCapabilityError,
)

POWERSHELL = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"

#: Text shaped to break out of a quoted literal and run a command instead.
HOSTILE = "Notepad'); Remove-Item -Recurse -Force C:\\; ("

WINENUM_ROWS = [
    {"Id": "0x1000", "Title": "Report.docx - Word", "Process": "winword", "Focused": False},
    {"Id": "0x2000", "Title": "iotmcp - Visual Studio Code", "Process": "code", "Focused": True},
]


class PowerShellRunner:
    """Records the static script and environment of each PowerShell child, and answers it.

    Args:
        responses: Substring of the script text → stdout (a ``str``, or an object to dump).
        failing: ``(needle, stderr)`` answered with exit 1.
    """

    def __init__(
        self, responses: dict[str, Any] | None = None, *, failing: tuple[str, str] | None = None
    ) -> None:
        self.responses = responses or {}
        self.failing = failing
        self.scripts: list[str] = []
        self.envs: list[dict[str, str]] = []
        self.argvs: list[tuple[str, ...]] = []

    @property
    def arg_values(self) -> list[str]:
        """Every value that travelled through the ``IOT_MCP_ARG<n>`` channel."""
        return [
            value
            for env in self.envs
            for key, value in env.items()
            if key.startswith("IOT_MCP_ARG")
        ]

    async def __call__(self, argv: Any, **kwargs: Any) -> Completed:
        vector = tuple(str(item) for item in argv)
        self.argvs.append(vector)
        stdin = kwargs.get("stdin") or ""
        env = dict(kwargs.get("env") or {})
        self.scripts.append(stdin)
        self.envs.append(env)
        if self.failing and self.failing[0] in stdin:
            return Completed(vector, 1, "", self.failing[1])
        for needle, payload in self.responses.items():
            if needle in stdin:
                text = payload if isinstance(payload, str) else json.dumps(payload)
                return Completed(vector, 0, text, "")
        return Completed(vector, 0, "", "")


def driver(
    monkeypatch: pytest.MonkeyPatch,
    runner: PowerShellRunner,
    *,
    with_powershell: bool = True,
    with_start_menu: bool = True,
    environ: dict[str, str] | None = None,
) -> WindowsDriver:
    """A Windows driver whose PowerShell child is simulated."""
    monkeypatch.setattr(windows, "run", runner)
    return WindowsDriver(
        environ=environ or {"PUBLIC_ROOT": "C:\\ProgramData"},
        which_fn=lambda tool: (
            POWERSHELL if (with_powershell and tool.startswith("powershell")) else None
        ),
        path_exists=lambda path: with_start_menu,
    )


# --- quoting -------------------------------------------------------------------


def test_single_quotes_are_neutralised_for_a_powershell_literal() -> None:
    """The escape closes the current literal and opens a fresh one, so the quote is data."""
    assert quote_single("plain") == "plain"
    assert quote_single("it's") == "it''')\n('s"
    assert quote_single("'") == "''')\n('"


def test_nul_bytes_are_removed_and_non_strings_refused() -> None:
    assert "\x00" not in quote_single("a\x00b")
    with pytest.raises(InvalidInputError):
        quote_single(7)  # type: ignore[arg-type]


# --- the injection defence -----------------------------------------------------


async def test_the_app_name_never_appears_in_the_script_text(monkeypatch) -> None:
    runner = PowerShellRunner({"Start-Process": ""})
    await driver(monkeypatch, runner).open_app(HOSTILE)

    assert HOSTILE not in runner.scripts[-1]
    assert "Remove-Item" not in runner.scripts[-1]
    assert HOSTILE in runner.arg_values, "the value must still reach PowerShell, via $env:"


async def test_a_hostile_url_stays_in_the_environment(monkeypatch) -> None:
    hostile_url = "https://example.com/?x=1&next=';Stop-Process#'"
    runner = PowerShellRunner({"Start-Process": '{"opened": true}'})
    await driver(monkeypatch, runner).open_url(hostile_url)

    assert hostile_url not in runner.scripts[-1]
    assert "Stop-Process" not in runner.scripts[-1]
    assert hostile_url in runner.arg_values


async def test_a_hostile_window_query_stays_in_the_environment(monkeypatch) -> None:
    rows = [{"Id": "0x3000", "Title": HOSTILE, "Process": "notepad", "Focused": False}]
    runner = PowerShellRunner({"WinEnum": rows, "WinFocus": '{"focused": true}'})
    await driver(monkeypatch, runner).focus_window(HOSTILE)

    # the title is matched in-process; only the winning window handle is ever sent
    assert all(HOSTILE not in script for script in runner.scripts)
    assert all(HOSTILE not in value for value in runner.arg_values)
    assert runner.arg_values == ["3000"]


async def test_a_hostile_close_target_stays_in_the_environment(monkeypatch) -> None:
    runner = PowerShellRunner({"Get-Process": {"terminated": [1], "force_killed": []}})
    await driver(monkeypatch, runner).close_app(HOSTILE)

    assert all(HOSTILE not in script for script in runner.scripts)
    assert HOSTILE in runner.arg_values


async def test_scripts_are_piped_on_stdin_and_argv_never_carries_caller_data(monkeypatch) -> None:
    """``-Command -`` plus a fixed argv is what takes shell parsing out of the picture."""
    runner = PowerShellRunner({"Start-Process": ""})
    await driver(monkeypatch, runner).open_app(HOSTILE)

    assert runner.argvs[-1] == (POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", "-")
    assert all(HOSTILE not in token for token in runner.argvs[-1])


async def test_the_argument_channel_is_additive_on_top_of_the_inherited_environment(
    monkeypatch,
) -> None:
    """The child inherits the server's environment (it must, to run PowerShell) and gains
    exactly one positional slot per caller-supplied value."""
    runner = PowerShellRunner({"Start-Process": ""})
    base = {"SYSTEMROOT": "C:\\Windows", "PUBLIC_ROOT": "C:\\ProgramData"}
    await driver(monkeypatch, runner, environ=base).open_app(HOSTILE)

    sent = runner.envs[-1]
    assert sent["SYSTEMROOT"] == "C:\\Windows"
    args = {key: value for key, value in sent.items() if key.startswith("IOT_MCP_ARG")}
    assert list(args) == ["IOT_MCP_ARG0"]
    assert args["IOT_MCP_ARG0"] == HOSTILE


async def test_the_url_allowlist_still_applies_on_windows(monkeypatch) -> None:
    """The injection defence must not become a substitute for the URL policy."""
    runner = PowerShellRunner({})
    with pytest.raises(InvalidInputError):
        await driver(monkeypatch, runner).open_url("file:///C:/Windows/system32/cmd.exe")
    assert runner.scripts == []


# --- capabilities --------------------------------------------------------------


def test_media_keys_are_supported_and_now_playing_is_not(monkeypatch) -> None:
    instance = driver(monkeypatch, PowerShellRunner({"MediaKeys": ""}))
    matrix = instance.capability_matrix()
    assert matrix["capabilities"]["media.play_pause"]["supported"] is True
    assert matrix["capabilities"]["media.next"]["supported"] is True
    status = matrix["capabilities"]["media.status"]
    assert status["supported"] is False
    assert "details" not in status, "an empty details dict must not reach the wire"
    assert "SMTC" in status["reason"], "the refusal must say what would be needed"


def test_a_windows_machine_without_powershell_degrades_rather_than_crashing() -> None:
    instance = WindowsDriver(environ={}, which_fn=lambda tool: None, path_exists=lambda path: False)
    matrix = instance.capability_matrix()
    assert matrix["summary"]["supported"] == 0
    assert matrix["capabilities"]["apps.open"]["details"]["dependency"] == "powershell"


def test_a_missing_start_menu_only_kills_enumeration_not_launching() -> None:
    """The probe distinguishes "cannot list" from "cannot launch"."""
    instance = WindowsDriver(
        environ={},
        which_fn=lambda tool: POWERSHELL,
        path_exists=lambda path: False,
    )
    support = instance.support()
    assert support[Capability.APPS_LIST].supported is False
    assert support[Capability.APPS_OPEN].supported is True


# --- behaviour -----------------------------------------------------------------


async def test_media_keys_send_the_documented_virtual_keys(monkeypatch) -> None:
    runner = PowerShellRunner({"MediaKeys": '{"sent_vk": 179}'})
    instance = driver(monkeypatch, runner)
    results = [
        await instance.media_play_pause(),
        await instance.media_next(),
        await instance.media_previous(),
    ]
    assert [result["virtual_key"] for result in results] == ["0xB3", "0xB0", "0xB1"]
    assert all(result["mechanism"] == "SendInput" for result in results)
    assert [value for value in runner.arg_values] == ["179", "176", "177"], "VK travels as a number"


async def test_media_status_refuses_instead_of_inventing_a_value(monkeypatch) -> None:
    instance = driver(monkeypatch, PowerShellRunner({"MediaKeys": ""}))
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await instance.media_status()
    assert "SMTC" in excinfo.value.hint


async def test_list_windows_reads_the_json_document(monkeypatch) -> None:
    instance = driver(monkeypatch, PowerShellRunner({"WinEnum": WINENUM_ROWS}))
    result = await instance.list_windows()
    assert result["count"] == 2
    assert result["windows"][0] == {
        "id": "0x1000",
        "app": "winword",
        "title": "Report.docx - Word",
        "focused": False,
    }
    assert result["windows"][1]["focused"] is True


async def test_focusing_a_window_passes_the_handle_as_an_argument(monkeypatch) -> None:
    runner = PowerShellRunner({"WinEnum": WINENUM_ROWS, "WinFocus": '{"focused": true}'})
    result = await driver(monkeypatch, runner).focus_window("Report.docx")
    assert result["mechanism"] == "SetForegroundWindow"
    assert result["already_focused"] is False
    assert runner.arg_values == ["1000"], "the 0x prefix is stripped for [Convert]::ToInt64 base 16"


async def test_unparsable_powershell_output_is_an_empty_answer_not_a_crash(monkeypatch) -> None:
    instance = driver(monkeypatch, PowerShellRunner({"WinEnum": "not json at all"}))
    assert (await instance.list_windows())["count"] == 0


async def test_a_powershell_failure_is_surfaced_with_its_stderr(monkeypatch) -> None:
    instance = driver(monkeypatch, PowerShellRunner(failing=("WinEnum", "Access is denied")))
    with pytest.raises(CommandFailedError) as excinfo:
        await instance.list_windows()
    assert "Access is denied" in str(excinfo.value)


async def test_list_apps_returns_start_menu_shortcuts(monkeypatch) -> None:
    rows = [
        {"name": "Notepad", "path": "C:\\Start\\Notepad.lnk", "source": "start-menu"},
        {"name": "", "path": "C:\\Start\\broken.lnk", "source": "start-menu"},
    ]
    instance = driver(monkeypatch, PowerShellRunner({"Get-ChildItem": rows}))
    result = await instance.list_apps()
    assert result["count"] == 1, "an unnamed shortcut is not an app"
    assert result["apps"][0]["id"] == "Notepad"


async def test_close_app_reports_graceful_and_forced_pids(monkeypatch) -> None:
    runner = PowerShellRunner({"Get-Process": {"terminated": [11, 12], "force_killed": [12]}})
    result = await driver(monkeypatch, runner).close_app("notepad")
    assert result["terminated_pids"] == [11, 12]
    assert result["force_killed_pids"] == [12]


async def test_close_app_translates_windows_no_match_into_not_found(monkeypatch) -> None:
    instance = driver(
        monkeypatch, PowerShellRunner(failing=("Get-Process", "no running process matches notepad"))
    )
    with pytest.raises(NotFoundError) as excinfo:
        await instance.close_app("notepad")
    assert excinfo.value.to_dict()["error"]["type"] == "not_found"


async def test_a_missing_powershell_is_a_dependency_error(monkeypatch) -> None:
    instance = driver(monkeypatch, PowerShellRunner({}), with_powershell=False)
    with pytest.raises(DependencyMissingError) as excinfo:
        await instance.open_app("notepad")
    assert excinfo.value.to_dict()["error"]["details"]["dependency"] == "powershell"


def test_the_capability_names_on_the_wire_are_the_documented_ones() -> None:
    """Guards against the enum values and the protocol drifting apart."""
    assert Capability.MEDIA_STATUS.value == "media.status"
    assert Capability.APPS_CLOSE.value == "apps.close"
