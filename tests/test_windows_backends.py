"""Window listing and focus: Hyprland, X11 tools, and honest degradation elsewhere.

Every case here runs against :class:`FakeRun`. That matters beyond hygiene: focusing a
window on the operator's live compositor would steal their input focus, so the tests must
never dispatch a real focus change.
"""

from __future__ import annotations

from typing import Any

import pytest

import iot_mcp.drivers._linux_wayland as wayland
from iot_mcp.drivers._winutil import Window, select_window
from iot_mcp.drivers.linux import LinuxDriver
from iot_mcp.errors import CommandFailedError, NotFoundError, UnsupportedCapabilityError

# ``hyprctl -j clients`` answers with a top-level JSON array, not an envelope.
HYPRACTIVE = [
    {
        "address": "0x000062d0",
        "class": "firefox",
        "initialClass": "firefox",
        "title": "IoT-MCP — Mozilla Firefox",
        "workspace": {"id": 1, "name": "1:www"},
        "pid": 4242,
        "focused": True,
        "floating": False,
        "fullscreen": 0,
    },
    {
        "address": "0x00007a10",
        "class": "org.kde.dolphin",
        "initialClass": "dolphin",
        "title": "projects",
        "workspace": {"id": 2, "name": "2:files"},
        "pid": 4300,
        "focused": False,
        "floating": True,
        "fullscreen": 0,
    },
]


@pytest.fixture
def hyprland_env(desktop_env) -> Any:
    """A Wayland session that *is* Hyprland — the one backend with real window control."""

    def build(**overrides: str | None) -> dict[str, str]:
        return desktop_env(HYPRLAND_INSTANCE_SIGNATURE="test-instance", **overrides)

    return build


def driver_for(fake: Any, environ: dict[str, str], tools: tuple[str, ...]) -> LinuxDriver:
    from conftest import make_which

    return LinuxDriver(
        environ={
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "XDG_DATA_DIRS": "/none",
            "XDG_DATA_HOME": "/none",
            **environ,
        },
        which_fn=make_which(tools),
    )


# --- backend detection (pure: no subprocesses) ---------------------------------


def test_hyprland_session_with_hyprctl_is_the_full_backend(hyprland_env) -> None:
    backend = wayland.detect_backend(hyprland_env(), which_fn=lambda tool: f"/usr/bin/{tool}")
    assert backend.kind == "hyprland" and backend.supported is True
    assert backend.details["compositor"] == "hyprland"


def test_hyprland_without_hyprctl_names_the_package_to_install(hyprland_env) -> None:
    backend = wayland.detect_backend(hyprland_env(), which_fn=lambda tool: None)
    assert backend.kind == "hyprland" and backend.supported is False
    assert backend.dependency == "hyprctl"


def test_x11_session_with_wmctrl_is_supported(desktop_env) -> None:
    backend = wayland.detect_backend(
        desktop_env(WAYLAND_DISPLAY=None, DISPLAY=":1"),
        which_fn=lambda tool: "/usr/bin/wmctrl" if tool == "wmctrl" else None,
    )
    assert backend.kind == "x11" and backend.supported is True
    assert backend.details["tools"] == ["wmctrl"]


def test_wayland_only_session_refuses_to_pretend_x11_tools_work(desktop_env) -> None:
    """wmctrl on a Wayland-only session lists almost nothing — saying so beats a silent 0."""
    backend = wayland.detect_backend(
        desktop_env(DISPLAY=""),
        which_fn=lambda tool: "/usr/bin/wmctrl" if tool in ("wmctrl", "xdotool") else None,
    )
    assert backend.kind == "x11-tools-only" and backend.supported is False
    assert "XWayland" in backend.reason


def test_a_hybrid_session_records_the_xwayland_caveat(desktop_env) -> None:
    backend = wayland.detect_backend(
        desktop_env(DISPLAY=":0"),
        which_fn=lambda tool: "/usr/bin/wmctrl" if tool == "wmctrl" else None,
    )
    assert backend.kind == "x11" and backend.supported is True
    assert "XWayland" in backend.details["wayland_caveat"]


@pytest.mark.parametrize(
    "desktop,session,expected_kind",
    [
        ("KDE", "plasma", "wayland-kwin"),
        ("GNOME", "gnome", "wayland-gnome"),
        ("sway", "sway", "wayland"),
    ],
)
def test_unsupported_wayland_compositors_are_named_not_guessed(
    desktop_env, desktop: str, session: str, expected_kind: str
) -> None:
    backend = wayland.detect_backend(
        desktop_env(DISPLAY="", XDG_CURRENT_DESKTOP=desktop, DESKTOP_SESSION=session),
        which_fn=lambda tool: None,
    )
    assert backend.kind == expected_kind and backend.supported is False
    assert backend.reason


def test_a_headless_environment_says_where_to_run_the_server() -> None:
    backend = wayland.detect_backend({"PATH": "/usr/bin"}, which_fn=lambda tool: None)
    assert backend.kind == "none" and backend.supported is False
    assert "desktop session" in backend.details["hint"]


def test_detection_spawns_nothing(monkeypatch) -> None:
    """list_actions() calls this on every request; it must stay pure."""

    def exploding(*a: Any, **k: Any) -> Any:  # pragma: no cover
        raise AssertionError("detect_backend must not spawn a subprocess")

    monkeypatch.setattr(wayland, "run", exploding)
    wayland.detect_backend(
        {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"}, which_fn=lambda tool: None
    )


# --- Hyprland ------------------------------------------------------------------


async def test_hyprland_windows_are_mapped_onto_the_neutral_shape(fake_run, hyprland_env) -> None:
    fake = fake_run()
    fake.on_json("-j clients", HYPRACTIVE)
    driver = driver_for(fake, hyprland_env(), ("hyprctl", "playerctl"))

    result = await driver.list_windows()

    assert result["backend"] == "hyprland" and result["count"] == 2
    first, second = result["windows"]
    assert first == {
        "id": "0x000062d0",
        "app": "firefox",
        "title": "IoT-MCP — Mozilla Firefox",
        "focused": True,
        "workspace": "1:www",
        "pid": 4242,
        "floating": False,
        "fullscreen": 0,
    }
    assert second["focused"] is False


async def test_focusing_a_window_passes_the_address_as_one_argument(fake_run, hyprland_env) -> None:
    fake = fake_run()
    fake.on_json("-j clients", HYPRACTIVE)
    driver = driver_for(fake, hyprland_env(), ("hyprctl",))

    result = await driver.focus_window("dolphin")

    call = fake.calls_for("dispatch focuswindow")[-1]
    assert call.argv[-2:] == ("focuswindow", "address:0x00007a10")
    assert result["mechanism"] == "hyprctl dispatch focuswindow"
    assert result["already_focused"] is False
    assert result["window"]["title"] == "projects"


async def test_focusing_the_already_focused_window_reports_it(fake_run, hyprland_env) -> None:
    fake = fake_run()
    fake.on_json("-j clients", HYPRACTIVE)
    driver = driver_for(fake, hyprland_env(), ("hyprctl",))
    result = await driver.focus_window("IoT-MCP")
    assert result["already_focused"] is True


async def test_a_title_with_injection_shaped_text_is_a_single_argument(
    fake_run, hyprland_env
) -> None:
    """The window address is what is dispatched, and it never passes through a shell."""
    hostile = {
        "address": "0xdead",
        "class": "xterm",
        "title": '"; hyprctl dispatch exec rm -rf / #',
        "focused": False,
    }
    fake = fake_run()
    fake.on_json("-j clients", [hostile])
    driver = driver_for(fake, hyprland_env(), ("hyprctl",))

    result = await driver.focus_window("rm -rf")

    call = fake.calls_for("dispatch focuswindow")[-1]
    assert call.argv[-1] == "address:0xdead"
    assert all(";" not in token for token in call.argv)
    assert result["ok"] is True


async def test_a_refused_dispatch_is_reported_with_a_retry_hint(fake_run, hyprland_env) -> None:
    fake = fake_run()
    fake.on_json("-j clients", HYPRACTIVE)
    fake.on("dispatch focuswindow", "", code=1, stderr="no such window")
    driver = driver_for(fake, hyprland_env(), ("hyprctl",))
    with pytest.raises(CommandFailedError) as excinfo:
        await driver.focus_window("dolphin")
    assert "list_windows()" in excinfo.value.hint


async def test_a_stale_compositor_socket_names_the_instance_signature(
    fake_run, hyprland_env
) -> None:
    fake = fake_run()
    fake.on("-j clients", "", code=1, stderr="Could not connect to socket: No such file")
    driver = driver_for(fake, hyprland_env(), ("hyprctl",))
    with pytest.raises(CommandFailedError) as excinfo:
        await driver.list_windows()
    assert "HYPRLAND_INSTANCE_SIGNATURE" in excinfo.value.hint


async def test_non_json_output_is_reported_with_the_captured_stdout(fake_run, hyprland_env) -> None:
    """A zero exit that is not JSON is still a failure, and the text must be shown."""
    fake = fake_run()
    fake.on("-j clients", "hyprctl: command not found")
    driver = driver_for(fake, hyprland_env(), ("hyprctl",))
    with pytest.raises(CommandFailedError) as excinfo:
        await driver.list_windows()
    assert "as JSON" in str(excinfo.value)
    assert "command not found" in excinfo.value.details["stdout"]


# --- X11 -----------------------------------------------------------------------


def _without_wmctrl(name: str, *, environ=None) -> str | None:
    """Resolve every helper except wmctrl, which has 'just been uninstalled'."""
    import shutil

    return None if name == "wmctrl" else shutil.which(name, path=(environ or {}).get("PATH", ""))


WMCTRL_LINES = """0x03400003  0 4242     localhost Mozilla Firefox firefox
0x03600007 -1 4300     localhost projects dolphin
"""


async def test_x11_listing_marks_the_focused_window(fake_run, desktop_env, tool_bin) -> None:
    fake = fake_run()
    fake.on("wmctrl -l -p -x", WMCTRL_LINES)
    fake.on("xdotool getwindowfocus", "0x03600007")
    driver = driver_for(
        fake,
        desktop_env(
            PATH=tool_bin("wmctrl", "xdotool"),
            DISPLAY=":1",
            WAYLAND_DISPLAY=None,
            HYPRLAND_INSTANCE_SIGNATURE=None,
        ),
        ("wmctrl", "xdotool"),
    )
    result = await driver.list_windows()
    assert result["backend"] == "x11"
    by_id = {window["id"]: window for window in result["windows"]}
    assert by_id["0x03600007"]["focused"] is True
    assert by_id["0x03400003"]["focused"] is False
    assert by_id["0x03400003"]["app"] == "firefox"


async def test_x11_focus_prefers_wmctrl_and_uses_the_window_id(
    fake_run, desktop_env, tool_bin
) -> None:
    fake = fake_run()
    fake.on("wmctrl -l -p -x", WMCTRL_LINES)
    fake.on("xdotool getwindowfocus", "")
    driver = driver_for(
        fake,
        desktop_env(
            PATH=tool_bin("wmctrl", "xdotool"),
            DISPLAY=":1",
            WAYLAND_DISPLAY=None,
            HYPRLAND_INSTANCE_SIGNATURE=None,
        ),
        ("wmctrl", "xdotool"),
    )
    result = await driver.focus_window("projects")
    assert fake.calls_for("-i -a")[-1].argv[-3:] == ("-i", "-a", "0x03600007")
    assert result["mechanism"] == "wmctrl -i -a"


async def test_x11_focus_falls_back_to_xdotool(fake_run, desktop_env, tool_bin) -> None:
    """With only xdotool installed, activation goes through it instead of wmctrl."""
    fake = fake_run()
    fake.on("wmctrl -l -p -x", WMCTRL_LINES)
    fake.on("xdotool getwindowfocus", "")
    driver = driver_for(
        fake,
        desktop_env(
            PATH=tool_bin("wmctrl", "xdotool"),
            DISPLAY=":1",
            WAYLAND_DISPLAY=None,
            HYPRLAND_INSTANCE_SIGNATURE=None,
        ),
        ("wmctrl", "xdotool"),
    )
    monkey = pytest.MonkeyPatch()

    # The window list is already in hand and wmctrl disappears before the activation call;
    # listing is stubbed so this test exercises only the focus fallback.
    async def already_listed() -> list[Window]:
        return [Window(id="0x03600007", app="dolphin", title="projects")]

    monkey.setattr(driver, "_enumerate_windows", already_listed)
    monkey.setattr(wayland, "which", _without_wmctrl)
    try:
        result = await driver.focus_window("projects")
    finally:
        monkey.undo()
    assert result["mechanism"] == "xdotool windowactivate"
    assert fake.calls_for("windowactivate")[-1].argv[-3:] == (
        "windowactivate",
        "--sync",
        "0x03600007",
    )


def test_wmctrl_line_parsing_handles_the_lossy_format() -> None:
    window = wayland.parse_wmctrl_line("0x03400003  0 4242     localhost Mozilla Firefox firefox")
    assert window is not None
    assert (window.id, window.app, window.title, window.extra["pid"]) == (
        "0x03400003",
        "firefox",
        "Mozilla Firefox",
        4242,
    )


@pytest.mark.parametrize("line", ["", "no windows", "0x", "garbage line here now"])
def test_unparsable_wmctrl_lines_are_skipped(line: str) -> None:
    assert wayland.parse_wmctrl_line(line) is None


# --- honest degradation --------------------------------------------------------


async def test_kwin_is_detected_over_dbus_and_explains_the_fix(
    fake_run, desktop_env, tool_bin
) -> None:
    fake = fake_run()
    fake.on("busctl --user", "org.kde.KWin\norg.freedesktop.DBus\n")
    driver = driver_for(
        fake,
        desktop_env(
            DISPLAY="",
            XDG_CURRENT_DESKTOP="KDE",
            DESKTOP_SESSION="plasma",
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/none",
        ),
        ("busctl",),
    )
    matrix = driver.capability_matrix()
    assert matrix["capabilities"]["windows.list"]["supported"] is False

    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await driver.list_windows()
    assert "KWin" in excinfo.value.hint
    assert excinfo.value.details["detected"] == "KWin (KDE) is running"


async def test_a_wayland_session_without_a_bridge_says_the_protocol_is_the_problem(
    fake_run, desktop_env
) -> None:
    fake = fake_run()
    driver = driver_for(fake, desktop_env(DISPLAY="", XDG_CURRENT_DESKTOP="sway"), ())
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        await driver.list_windows()
    assert "core protocol" in str(excinfo.value) or "no supported compositor" in str(excinfo.value)
    assert excinfo.value.to_dict()["error"]["type"] == "unsupported"


async def test_the_dbus_probe_never_breaks_the_answer() -> None:
    """A wedged or absent session bus must degrade silently, not raise from the probe.

    Deliberately installs no fakes: the probe is a plain socket connect, so this really is
    the "the bus address points at nothing" path.
    """
    assert (
        await wayland.detect_dbus_compositor(
            {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nope", "PATH": "/usr/bin"}
        )
        is None
    )
    assert (
        await wayland.detect_dbus_compositor(
            {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nope", "PATH": "/usr/bin"}
        )
        is None
    )
    assert await wayland.detect_dbus_compositor({}) is None


# --- window selection ----------------------------------------------------------

WINDOWS = [
    Window(id="0x1", app="firefox", title="IoT-MCP — Mozilla Firefox", focused=True),
    Window(id="0x2", app="dolphin", title="projects"),
    Window(id="0x3", app="firefox", title="Bugs — Mozilla Firefox"),
]


def test_selection_prefers_the_title_over_a_loose_app_match() -> None:
    assert select_window("projects", WINDOWS).id == "0x2"


def test_selection_is_case_insensitive_and_matches_the_app() -> None:
    assert select_window("DOLPHIN", WINDOWS).id == "0x2"


def test_an_ambiguous_query_chooses_the_focused_window() -> None:
    """When two windows match equally, the one the user is looking at is the safer bet."""
    assert select_window("firefox", WINDOWS).id == "0x1"


def test_no_match_lists_the_real_titles() -> None:
    with pytest.raises(NotFoundError) as excinfo:
        select_window("blender", WINDOWS)
    details = excinfo.value.to_dict()["error"]["details"]
    assert {"title": "projects", "app": "dolphin"} in details["candidates"]
    assert "list_windows()" in excinfo.value.hint


@pytest.mark.parametrize("query", ["", "  "])
def test_an_empty_window_query_is_an_input_error(query: str) -> None:
    from iot_mcp.errors import InvalidInputError

    with pytest.raises(InvalidInputError):
        select_window(query, WINDOWS)
