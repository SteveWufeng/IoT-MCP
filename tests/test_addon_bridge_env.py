"""Headed add-ons get the compositor handle back; headless ones never do."""

from __future__ import annotations

import pytest

from iot_mcp.addon_bridge import _desktop_env

WAYLAND = {
    "XDG_SESSION_TYPE": "wayland",
    "WAYLAND_DISPLAY": "wayland-1",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "DISPLAY": ":1",
    "HOME": "/home/tester",
    "PATH": "/usr/bin",
}

X11 = {
    "XDG_SESSION_TYPE": "x11",
    "DISPLAY": ":0",
    "XAUTHORITY": "/home/tester/.Xauthority",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "HOME": "/home/tester",
    "PATH": "/usr/bin",
}

HEADLESS = ("npx", "-y", "@playwright/mcp@latest", "--headless", "--browser", "chromium")
HEADED = ("npx", "-y", "@playwright/mcp@latest", "--browser", "chromium")


@pytest.fixture
def scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from an environment with nothing but the SDK's own defaults present."""
    for key in (
        "XDG_SESSION_TYPE",
        "WAYLAND_DISPLAY",
        "WAYLAND_SOCKET",
        "XDG_RUNTIME_DIR",
        "DISPLAY",
        "XAUTHORITY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("PATH", "/usr/bin")


def _seed(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_headless_keeps_the_sdk_scrub(scrubbed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(monkeypatch, WAYLAND)
    env, desktop = _desktop_env(HEADLESS)
    assert desktop == "none"
    assert "WAYLAND_DISPLAY" not in env
    assert "DISPLAY" not in env
    assert env["PATH"] == "/usr/bin"
    assert "PLAYWRIGHT_MCP_HEADLESS" not in env


def test_wayland_session_forwards_the_socket_only(
    scrubbed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(monkeypatch, WAYLAND)
    env, desktop = _desktop_env(HEADED)
    assert desktop == "wayland"
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    # Upstream force-heads any Linux launch with no DISPLAY; the override pins it headed.
    assert env["PLAYWRIGHT_MCP_HEADLESS"] == "false"
    # An X11 handle would grant sight of every other window: never on a Wayland session.
    assert "DISPLAY" not in env
    assert "XAUTHORITY" not in env


def test_x11_session_forwards_the_display(scrubbed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(monkeypatch, X11)
    env, desktop = _desktop_env(HEADED)
    assert desktop == "x11"
    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"] == "/home/tester/.Xauthority"
    assert env["PLAYWRIGHT_MCP_HEADLESS"] == "false"
    assert "WAYLAND_DISPLAY" not in env


def test_shell_functions_are_never_forwarded(
    scrubbed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(monkeypatch, {**WAYLAND, "WAYLAND_DISPLAY": "() { :;}; /bin/rm -rf /"})
    env, desktop = _desktop_env(HEADED)
    assert desktop == "none"
    assert "WAYLAND_DISPLAY" not in env
    assert "PLAYWRIGHT_MCP_HEADLESS" not in env


def test_headed_without_any_display_reports_none(
    scrubbed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    env, desktop = _desktop_env(HEADED)
    assert desktop == "none"
    assert "WAYLAND_DISPLAY" not in env
    assert "PLAYWRIGHT_MCP_HEADLESS" not in env
