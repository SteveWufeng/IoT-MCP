"""The MCP server: tool definitions that delegate to a driver, and nothing else.

Kept deliberately thin. There is no capability logic in here — a tool either returns the
driver's result dict or the driver's error envelope, so all behaviour lives in the driver and
can be tested without a transport.

Tool docstrings are part of the public interface: they are sent to the model verbatim as the
tool description, so they state preconditions and failure modes rather than implementation
notes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .drivers import get_driver
from .drivers.base import AutomationDriver
from .errors import DriverError

__all__ = ["SERVER_INSTRUCTIONS", "build_server", "register_tools"]

SERVER_INSTRUCTIONS = """OS automation on the machine running this server.

Start with list_actions(). It returns the capability matrix for the driver actually running
here, and several capabilities are unavailable on purpose (for example windows.list on a
Wayland session with no compositor bridge, or media.status on Windows). Calling a tool whose
capability is unsupported is harmless but wasted: it returns {{'"ok": false'}} with a reason.

Every tool returns a JSON object with an "ok" key. On failure "ok" is false and "error"
carries a stable "type" — one of unsupported, dependency_missing, not_found, command_failed,
timeout, invalid_input — plus a "hint" describing what to do next. Nothing ever returns a
traceback. Retry only when the hint suggests a concrete alternative; unsupported capabilities
will not start working on retry.

App arguments are matched fuzzily against installed application names and ids; if a name
returns not_found, the error includes a candidate list to pick from. Prefer exact ids from
list_apps() over guessing."""


async def _invoke(action: Callable[[], Any]) -> dict[str, Any]:
    """Run one driver call and normalise the outcome to the tool result envelope.

    Args:
        action: Zero-argument callable returning either a result dict or an awaitable of
            one, so the async capability methods and the synchronous capability matrix
            share a single error-handling path.

    Returns:
        The driver's result dict, or :meth:`iot_mcp.errors.DriverError.to_dict` for any
        typed driver failure. Unexpected exceptions are converted too, because an
        unhandled traceback in a tool response is useless to an agent.
    """
    try:
        outcome = action()
        return await outcome if hasattr(outcome, "__await__") else outcome
    except DriverError as exc:
        return exc.to_dict()
    except Exception as exc:  # pragma: no cover - driver bug, reported not raised
        return {
            "ok": False,
            "error": {
                "type": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
                "hint": "The server raised an unexpected error; report it or try a different "
                "capability from list_actions().",
            },
        }


def _annotations(
    title: str, *, read_only: bool, destructive: bool = False, open_world: bool = False
) -> ToolAnnotations:
    """Build tool annotations, so a client can gate dangerous calls without parsing text.

    Args:
        title: Human-facing label.
        read_only: Whether the tool only observes.
        destructive: Whether it can disrupt user work (closing apps, stealing focus).
        open_world: Whether the call reaches outside the machine. ``open_url`` is the only
            one: it hands a URL to the browser, which then fetches it. Marking it honestly
            lets a client require confirmation without reading the description.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=read_only,
        openWorldHint=open_world,
    )


def register_tools(server: MCPServer, driver: AutomationDriver) -> None:
    """Attach the MVP tool set to ``server``, delegating every call to ``driver``.

    Args:
        server: The :class:`MCPServer` to register on.
        driver: Backend that actually performs the work.
    """

    @server.tool(
        name="list_actions",
        title="Capability matrix",
        description=(
            "Report which automation capabilities this server supports right now, and why the "
            "rest are unavailable. Call this before anything else."
        ),
        annotations=_annotations("Capability matrix", read_only=True),
    )
    async def list_actions() -> dict[str, Any]:
        """Report which capabilities this server can actually perform, and why not.

        Call this first. The answer is per-machine and per-session: a Linux box running
        Hyprland supports ``windows.list``, the same box under a plain Wayland session does
        not, and a headless server supports nothing.

        Returns:
            ``{"ok": true, "driver", "platform", "summary": {"supported", "total",
            "unsupported"}, "capabilities": {"<name>": {"supported", "reason", "tool",
            "details"}}}``. Each ``reason`` is written for a human: on success it names the
            mechanism in use, on failure it says what to install or configure.

        Notes:
            Results are cached per server process, so a dependency installed after startup is
            not picked up until the server restarts. This call never fails and never changes
            system state.
        """
        return await _invoke(driver.capability_matrix)

    @server.tool(
        name="list_apps",
        title="List installed applications",
        description=(
            "List applications that can be launched on this machine. Use the returned 'id' or "
            "'name' with open_app and close_app."
        ),
        annotations=_annotations("List installed applications", read_only=True),
    )
    async def list_apps() -> dict[str, Any]:
        """List installed, launchable applications on the server's machine.

        Returns:
            ``{"ok": true, "count", "apps": [{"id", "name", "exec", "comment", ...}],
            "searched_directories"}``. On Linux ``id`` is the desktop-entry id (e.g.
            ``org.mozilla.firefox``); on macOS it is the bundle id; on Windows it is the Start
            Menu shortcut name.

        Preconditions:
            A desktop session with an application index (``.desktop`` files, ``/Applications``,
            or the Start Menu). A headless server returns an empty list rather than an error.

        Failure modes:
            ``unsupported`` when the driver has no application index at all — check
            ``apps.list`` in :func:`list_actions`.
        """
        return await _invoke(driver.list_apps)

    @server.tool(
        name="open_app",
        title="Launch an application",
        description=(
            "Launch an installed application by name or id. Matching is fuzzy and "
            "case-insensitive. Returns immediately; it does not wait for the app to exit."
        ),
        annotations=_annotations("Launch an application", read_only=False),
    )
    async def open_app(name: str) -> dict[str, Any]:
        """Launch an application without waiting for it to exit.

        Args:
            name: App name or id from :func:`list_apps` (``"firefox"``, ``"Firefox"``, ``"org.mozilla.firefox"``).

        Returns:
            ``{"ok": true, "app": {...}, "launched_argv": [...], "pid", "still_running"}``.
            The command is executed directly (no shell), so quoting in ``name`` is never
            interpreted.

        Preconditions:
            The app must be installed; a display session must exist for GUI apps to be usable.

        Failure modes:
            ``not_found`` with a candidate list when nothing matches ``name`` or the entry
            points at a missing binary; ``command_failed`` when the process exits immediately
            with a non-zero status (often a missing display); ``unsupported`` per
            :func:`list_actions`.
        """
        return await _invoke(lambda: driver.open_app(name))

    @server.tool(
        name="close_app",
        title="Close an application",
        description=(
            "Ask a running application to terminate: graceful request first, then force. "
            "Unsaved work in that app can be lost."
        ),
        annotations=_annotations("Close an application", read_only=False, destructive=True),
    )
    async def close_app(name: str) -> dict[str, Any]:
        """Terminate a running application.

        Args:
            name: App name, id, or process name (e.g. ``"firefox"``).

        Returns:
            ``{"ok": true, "terminated_pids": [...], "force_killed_pids": [...], ...}`` — the
            ``force_killed`` list is non-empty when the app ignored the graceful request.

        Preconditions:
            The app must be running, and the server process must own it or be permitted to
            signal it (same user; not another user's process).

        Failure modes:
            ``not_found`` when no process matches — the error lists what was searched for;
            ``unsupported`` on a backend without process control.

        Safety:
            The server's own process, its parent and PID 1 are never signalled, so asking to
            close an interpreter that is running this server cannot take the server down.
        """
        return await _invoke(lambda: driver.close_app(name))

    @server.tool(
        name="open_url",
        title="Open a URL",
        description=(
            "Open an http/https/mailto/ftp URL in the machine's default handler. Returns "
            "immediately."
        ),
        annotations=_annotations("Open a URL", read_only=False, open_world=True),
    )
    async def open_url(url: str) -> dict[str, Any]:
        """Open a URL in the server machine's default browser/handler.

        Args:
            url: Absolute URL, e.g. ``https://example.com``. No whitespace; percent-encode
                spaces as ``%20``.

        Returns:
            ``{"ok": true, "url", "launcher", "pid"}``. The handler is launched detached, so a
            slow browser start does not delay this call.

        Failure modes:
            ``invalid_input`` for empty, oversized (>2048), whitespace-bearing, or
            credential-carrying URLs, and for any scheme outside http, https, mailto, ftp,
            ftps. ``file://``, ``javascript:`` and local/executable schemes are refused
            deliberately — this endpoint is unauthenticated, so the argument may originate
            from untrusted page content.
        """
        return await _invoke(lambda: driver.open_url(url))

    @server.tool(
        name="media_play_pause",
        title="Play/pause media",
        description=(
            "Toggle playback in the active media player. Idempotent per call: it toggles, so "
            "call media_status() first if you need a known state."
        ),
        annotations=_annotations("Play/pause media", read_only=False),
    )
    async def media_play_pause() -> dict[str, Any]:
        """Toggle play/pause on the active media player.

        Returns:
            ``{"ok": true, "player", "action": "play-pause", "status"}`` — ``status`` is the
            player's state after the command (``Playing``/``Paused``/``Unknown``).

        Preconditions:
            A media player must already be running (MPRIS on Linux, a supported player's
            AppleScript dictionary on macOS, or the system media transport on Windows).

        Failure modes:
            ``not_found`` when no player is running; ``dependency_missing`` when the helper CLI
            (``playerctl`` on Linux) is not installed; ``unsupported`` per :func:`list_actions`.
        """
        return await _invoke(driver.media_play_pause)

    @server.tool(
        name="media_next",
        title="Next track",
        description="Skip to the next track in the active media player.",
        annotations=_annotations("Next track", read_only=False),
    )
    async def media_next() -> dict[str, Any]:
        """Skip to the next track in the active media player.

        Returns:
            ``{"ok": true, "player", "action": "next", "status"}``.

        Preconditions and failure modes:
            Same as :func:`media_play_pause`. Some players do not implement next-track; that
            surfaces as ``command_failed`` or ``unsupported`` naming the player.
        """
        return await _invoke(driver.media_next)

    @server.tool(
        name="media_previous",
        title="Previous track",
        description="Go back to the previous track in the active media player.",
        annotations=_annotations("Previous track", read_only=False),
    )
    async def media_previous() -> dict[str, Any]:
        """Return to the previous track in the active media player.

        Returns:
            ``{"ok": true, "player", "action": "previous", "status"}``.

        Preconditions and failure modes:
            Same as :func:`media_play_pause`.
        """
        return await _invoke(driver.media_previous)

    @server.tool(
        name="media_status",
        title="What is playing",
        description=(
            "Report the current media player state and track metadata. Never starts or stops "
            "anything."
        ),
        annotations=_annotations("What is playing", read_only=True),
    )
    async def media_status() -> dict[str, Any]:
        """Report what is currently playing, including track metadata.

        Returns:
            ``{"ok": true, "playing": {...} | null, "players": [{"player", "status", "title",
            "artist", "album", "url", "length"}]}``. ``playing`` is the entry whose status is
            ``Playing``, or ``null`` when nothing is playing. An empty ``players`` list means no
            media application is running — that is a successful answer, not an error.

        Privacy note:
            Track titles and artists are user data; treat the response as sensitive.

        Failure modes:
            ``unsupported`` on Windows (now-playing state needs a WinRT client), or where the
            media mechanism is missing; ``timeout`` if a player has stopped answering D-Bus.
        """
        return await _invoke(driver.media_status)

    @server.tool(
        name="list_windows",
        title="List windows",
        description=(
            "List on-screen windows with their app id, title, workspace and focus state. Read-only."
        ),
        annotations=_annotations("List windows", read_only=True),
    )
    async def list_windows() -> dict[str, Any]:
        """List the visible windows on the server's desktop.

        Returns:
            ``{"ok": true, "count", "windows": [{"id", "app", "title", "focused",
            "workspace", "pid"}], "backend", "note"}``. ``backend`` says how the list was
            obtained (``hyprland``, ``x11``, ``system-events``, ...). Window ``id`` values are
            opaque handles that may change; re-list before relying on one.

        Preconditions:
            A desktop session the server can talk to. On Wayland this needs a compositor
            bridge — plain Wayland has no window-management protocol, and X11 tools such as
            ``wmctrl`` only see XWayland windows.

        Failure modes:
            ``unsupported`` with the detected compositor and what to enable; ``command_failed``
            when the compositor socket is stale (server running in a different session).
        """
        return await _invoke(driver.list_windows)

    @server.tool(
        name="focus_window",
        title="Focus a window",
        description=(
            "Raise and focus the window matching an app name or title substring. Steals input "
            "focus from whatever the user is doing."
        ),
        annotations=_annotations("Focus a window", read_only=False, destructive=True),
    )
    async def focus_window(app_or_title: str) -> dict[str, Any]:
        """Raise and focus a window by app name or title substring.

        Args:
            app_or_title: Exact title, exact app id, or a distinctive substring (e.g.
                ``"Mozilla Firefox"``, ``"brave"``). Matching prefers exact, then prefix,
                then substring; ties prefer the focused window.

        Returns:
            ``{"ok": true, "window": {...}, "backend", "already_focused", "mechanism"}``.
            ``already_focused: true`` means the call was a no-op.

        Preconditions:
            The window must be listed by :func:`list_windows`. On macOS the server process needs
            Automation/Accessibility consent.

        Failure modes:
            ``not_found`` with a candidate list of visible titles; ``command_failed`` when the
            compositor refuses (Windows foreground lock, closed window); ``unsupported`` on a
            session with no window API.
        """
        return await _invoke(lambda: driver.focus_window(app_or_title))


def build_server(
    driver: AutomationDriver | None = None,
    *,
    name: str = "iot-mcp",
) -> MCPServer:
    """Construct the MCP server with the OS-automation tools attached.

    Args:
        driver: Backend to delegate to. ``None`` selects one by platform, which is what the
            CLI relies on.
        name: Server name reported in the MCP initialize handshake.

    Returns:
        A configured :class:`MCPServer`; call ``streamable_http_app()`` on it (see
        :mod:`iot_mcp.cli`) or ``run()`` for stdio.
    """
    active = driver if driver is not None else get_driver()
    server = MCPServer(
        name,
        title="IoT OS Automation",
        description="Control applications, media, URLs and windows on this machine.",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
    )
    register_tools(server, active)
    return server
