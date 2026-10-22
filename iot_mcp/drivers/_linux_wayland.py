"""Linux window backends: Hyprland IPC, X11 helper CLIs, and Wayland-compositor detection.

There is no single "list the windows" API on Linux. What works depends entirely on the
session:

======================  ==============================================  ==============
Session                 Mechanism                                       This package
======================  ==============================================  ==============
Hyprland                ``hyprctl -j clients`` / ``dispatch``           implemented
X11 (or XWayland)       ``wmctrl`` (+ ``xdotool`` for focus)            implemented
KWin (KDE)              ``org.kde.KWin`` on the user D-Bus              detected + degraded
GNOME Shell             ``org.gnome.Shell`` (extension-gated)           detected + degraded
plain Wayland           no window-management protocol in core spec      detected + degraded
======================  ==============================================  ==============

Wayland deliberately has no window-management protocol: clients cannot enumerate or raise
each other's windows without a compositor-specific bridge. Reporting that honestly beats
importing X11 tooling and crashing on a Wayland-only machine, which is what most "Linux
desktop automation" snippets do.

Detection never happens at import time and never raises: a probe failure downgrades the
capability and the reason surfaces in ``list_actions()``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..errors import CommandFailedError, UnsupportedCapabilityError
from ._proc import run, which
from ._winutil import Window

__all__ = ["Backend", "detect_backend", "detect_dbus_compositor", "focus_window", "list_windows"]

_TIMEOUT = 8.0

#: Compositor D-Bus interfaces we recognise but cannot drive without an extra bridge:
#: name -> (how we describe what we found, what the operator should enable).
_LIMITED_COMPOSITOR_INTERFACES: dict[str, tuple[str, str]] = {
    "org.kde.KWin": (
        "KWin (KDE) is running",
        "KWin exposes window control only through its scripting D-Bus API, which is off by "
        "default. Enable a KWin scripting bridge, or install 'wmctrl'/'xdotool' to manage "
        "XWayland windows.",
    ),
    "org.gnome.Shell": (
        "GNOME Shell is running",
        "GNOME Shell publishes no public window API; Eval-based extensions are disabled on "
        "GNOME 42+. Install a Window-List style Shell extension, or run apps under XWayland "
        "with 'wmctrl' installed.",
    ),
    "org.deepin.dde.KWinWrapper1": (
        "deepin KWin wrapper is running",
        "Same limitation as KWin: window control needs the KWin scripting bridge.",
    ),
}


@dataclass(frozen=True)
class Backend:
    """Which window mechanism is live, and whether it can actually be used.

    Attributes:
        kind: One of ``hyprland``, ``x11``, ``x11-tools-only``, ``wayland-kwin``,
            ``wayland-gnome``, ``wayland``, ``none``.
        supported: Whether list/focus are implemented for this session.
        reason: Explanation surfaced in the capability matrix and in raised errors.
        dependency: Tool whose installation would make this work, if any.
        details: Detected facts (compositor, tools found) for agents and bug reports.
    """

    kind: str
    supported: bool
    reason: str
    dependency: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def detect_backend(
    environ: Mapping[str, str], which_fn: Callable[[str], str | None] = which
) -> Backend:
    """Decide which window backend to use from environment variables and installed CLIs.

    Pure and synchronous on purpose: it runs inside ``list_actions()``, so it must not
    spawn processes or touch D-Bus. The D-Bus check that needs ``busctl`` happens later, in
    :func:`detect_dbus_compositor`, and only to enrich an already-negative answer.

    Args:
        environ: Session environment. Reads ``HYPRLAND_INSTANCE_SIGNATURE``,
            ``WAYLAND_DISPLAY``, ``DISPLAY``, ``XDG_CURRENT_DESKTOP``, ``DESKTOP_SESSION``.
        which_fn: Executable lookup hook, injectable for tests.

    Returns:
        A :class:`Backend`. Never raises.
    """
    hyprland_instance = environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    wayland_display = environ.get("WAYLAND_DISPLAY", "")
    x11_display = environ.get("DISPLAY", "")
    session_flavour = (
        f"{environ.get('XDG_CURRENT_DESKTOP', '')} {environ.get('DESKTOP_SESSION', '')}".lower()
    )

    if hyprland_instance:
        hyprctl = which_fn("hyprctl")
        if hyprctl:
            return Backend(
                kind="hyprland",
                supported=True,
                reason="Hyprland session with 'hyprctl' available (dynamic IPC socket).",
                details={"compositor": "hyprland", "tool": hyprctl},
            )
        return Backend(
            kind="hyprland",
            supported=False,
            reason="Hyprland session detected but 'hyprctl' is not installed.",
            dependency="hyprctl",
            details={"compositor": "hyprland"},
        )

    wmctrl = which_fn("wmctrl")
    xdotool = which_fn("xdotool")
    tools = [name for name, found in (("wmctrl", wmctrl), ("xdotool", xdotool)) if found]

    if tools and x11_display:
        return Backend(
            kind="x11",
            supported=True,
            reason=f"X11 DISPLAY is set; using {'/'.join(tools)}.",
            details={
                "tools": tools,
                "display": x11_display,
                # On a hybrid session, X11 tools are blind to native Wayland windows.
                "wayland_caveat": (
                    "Wayland session too: only XWayland windows are visible to these tools"
                    if wayland_display
                    else ""
                ),
            },
        )

    if tools and wayland_display:
        return Backend(
            kind="x11-tools-only",
            supported=False,
            reason=(
                f"{'/'.join(tools)} are installed but DISPLAY is unset, so this is a "
                "Wayland-only session: those tools see only XWayland windows (usually none)."
            ),
            details={"wayland_display": wayland_display},
        )

    if wayland_display:
        if "kde" in session_flavour or "plasma" in session_flavour:
            found, _fix = _LIMITED_COMPOSITOR_INTERFACES["org.kde.KWin"]
            return Backend(
                kind="wayland-kwin",
                supported=False,
                reason=f"{found}; window control needs its scripting bridge.",
                details={"compositor": "kwin", "wayland_display": wayland_display},
            )
        if "gnome" in session_flavour:
            found, _fix = _LIMITED_COMPOSITOR_INTERFACES["org.gnome.Shell"]
            return Backend(
                kind="wayland-gnome",
                supported=False,
                reason=f"{found}; window control needs a Shell extension.",
                details={"compositor": "gnome-shell", "wayland_display": wayland_display},
            )
        return Backend(
            kind="wayland",
            supported=False,
            reason=(
                f"Wayland session ({wayland_display}) with no supported compositor bridge. "
                "The Wayland core protocol has no window-management interface; install "
                "hyprctl on Hyprland, or wmctrl/xdotool for an X11/XWayland session."
            ),
            details={"wayland_display": wayland_display, "current_desktop": session_flavour},
        )

    return Backend(
        kind="none",
        supported=False,
        reason="No DISPLAY or WAYLAND_DISPLAY in the server's environment: nothing to control.",
        details={"hint": "Run the server inside the desktop session you want to automate."},
    )


async def _hyprctl_json(args: list[str], environ: Mapping[str, str]) -> Any:
    """Run ``hyprctl -j <args>`` and decode its JSON reply.

    Raises:
        CommandFailedError: hyprctl failed (stale socket, mismatched instance signature)
            or answered something that is not JSON.
    """
    exe = which("hyprctl", environ=environ) or "hyprctl"
    result = await run([exe, "-j", *args], timeout=_TIMEOUT)
    if not result.ok:
        raise CommandFailedError(
            f"hyprctl {' '.join(args)} failed: {result.stderr.strip()[:300]}",
            capability="windows.list",
            hint="Is the server running in the same Hyprland instance? "
            "HYPRLAND_INSTANCE_SIGNATURE must match the compositor.",
            details={"stderr": result.stderr.strip()[:400]},
        )
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise CommandFailedError(
            f"could not parse hyprctl output as JSON: {exc}",
            capability="windows.list",
            details={"stdout": result.stdout[:400]},
        ) from exc


def window_from_hyprland(client: Mapping[str, Any]) -> Window:
    """Map one ``hyprctl -j clients`` entry onto the neutral :class:`Window` shape."""
    workspace = client.get("workspace")
    workspace_name = (
        workspace.get("name", "") if isinstance(workspace, Mapping) else str(workspace or "")
    )
    return Window(
        id=str(client.get("address", "")),
        app=str(client.get("initialClass") or client.get("class") or ""),
        title=str(client.get("title", "")),
        focused=bool(client.get("focused")),
        workspace=workspace_name,
        extra={
            "pid": client.get("pid"),
            "floating": client.get("floating"),
            "fullscreen": client.get("fullscreen"),
        },
    )


def parse_wmctrl_line(line: str) -> Window | None:
    """Parse one ``wmctrl -l -p -x`` output line.

    The format is ``<id> <desktop> <pid> <client_machine> <title> <wm_class>``. Titles may
    contain spaces and ``wmctrl`` offers no quoting, so the last whitespace-separated token
    is taken as ``WM_CLASS`` and everything before it as the title. That is the same
    heuristic every other wmctrl wrapper uses, and it is why ``-x`` output is lossy for
    titles ending in a single word — noted in ``docs/internals.md`` as an X11-path limitation.

    Args:
        line: One output line.

    Returns:
        A :class:`Window`, or ``None`` when the line is not a window row.
    """
    parts = line.split(None, 4)
    if len(parts) < 4 or not parts[0].startswith("0x"):
        return None
    window_id, desktop, pid_str = parts[0], parts[1], parts[2]
    tail = parts[4] if len(parts) > 4 else ""
    tokens = tail.split()
    if len(tokens) >= 2:
        app, title = tokens[-1], " ".join(tokens[:-1])
    elif len(tokens) == 1:
        app, title = tokens[0], ""
    else:
        app, title = "", ""
    try:
        pid: int | None = int(pid_str)
    except ValueError:
        pid = None
    return Window(id=window_id, app=app, title=title.strip(), workspace=desktop, extra={"pid": pid})


async def _xdotool_focused_id(environ: Mapping[str, str]) -> str:
    """Return the focused X11 window id, or ``""`` when it cannot be determined."""
    xdotool = which("xdotool", environ=environ)
    if not xdotool:
        return ""
    result = await run([xdotool, "getwindowfocus"], timeout=_TIMEOUT)
    return result.stdout.strip() if result.ok else ""


async def _list_windows_x11(environ: Mapping[str, str]) -> list[Window]:
    """Enumerate X11 windows with ``wmctrl``, marking the focused one via ``xdotool``.

    Raises:
        UnsupportedCapabilityError: ``wmctrl`` is absent (``xdotool`` alone cannot
            enumerate reliably).
        CommandFailedError: ``wmctrl`` failed — usually a wrong/unreachable DISPLAY.
    """
    wmctrl = which("wmctrl", environ=environ)
    if wmctrl is None:
        raise UnsupportedCapabilityError(
            "window listing needs 'wmctrl'; 'xdotool' alone cannot enumerate reliably",
            capability="windows.list",
            hint="Install wmctrl.",
            details={"dependency": "wmctrl"},
        )
    result = await run([wmctrl, "-l", "-p", "-x"], timeout=_TIMEOUT)
    if not result.ok:
        raise CommandFailedError(
            f"wmctrl failed: {result.stderr.strip()[:200]}",
            capability="windows.list",
            hint="Check that DISPLAY points at a running X server.",
        )
    focused_id = await _xdotool_focused_id(environ)
    windows: list[Window] = []
    for line in result.stdout.splitlines():
        parsed = parse_wmctrl_line(line)
        if parsed is None:
            continue
        windows.append(
            Window(
                id=parsed.id,
                app=parsed.app,
                title=parsed.title,
                workspace=parsed.workspace,
                focused=bool(focused_id) and parsed.id == focused_id,
                extra=parsed.extra,
            )
        )
    return windows


async def list_windows(backend: Backend, environ: Mapping[str, str]) -> list[Window]:
    """Enumerate on-screen windows using ``backend``.

    Args:
        backend: Result of :func:`detect_backend`.
        environ: Session environment passed to helpers.

    Returns:
        Windows in the backend's native order (back-to-front for Hyprland's client list).

    Raises:
        UnsupportedCapabilityError: This session has no usable window API. The message
            names the detected compositor and the concrete way forward.
        CommandFailedError: A helper CLI failed in an actionable way.
    """
    if backend.kind == "hyprland":
        payload = await _hyprctl_json(["clients"], environ)
        clients = payload if isinstance(payload, list) else []
        return [
            window_from_hyprland(client)
            for client in clients
            if isinstance(client, Mapping) and client.get("address")
        ]
    if backend.kind == "x11":
        return await _list_windows_x11(environ)
    raise UnsupportedCapabilityError(
        f"window control is unavailable on this session: {backend.reason}",
        capability="windows.list",
        hint=str(backend.details.get("hint") or "See the windows.list entry in list_actions()."),
        details=backend.details,
    )


async def focus_window(
    backend: Backend, window: Window, environ: Mapping[str, str]
) -> dict[str, Any]:
    """Raise/focus ``window`` through ``backend``.

    Args:
        backend: Result of :func:`detect_backend`.
        window: A window previously returned by :func:`list_windows` (its ``id`` must be a
            live handle for this backend).
        environ: Session environment passed to helpers.

    Returns:
        Confirmation fields for the tool result (which mechanism ran).

    Raises:
        UnsupportedCapabilityError: No usable window API for this session.
        CommandFailedError: The compositor refused the request, or the tool is missing.
    """
    if backend.kind == "hyprland":
        exe = which("hyprctl", environ=environ) or "hyprctl"
        result = await run(
            [exe, "dispatch", "focuswindow", f"address:{window.id}"], timeout=_TIMEOUT
        )
        if not result.ok:
            raise CommandFailedError(
                f"hyprctl could not focus {window.id}: {result.stderr.strip()[:200]}",
                capability="windows.focus",
                hint="The window may have closed; re-run list_windows() and retry.",
            )
        return {"mechanism": "hyprctl dispatch focuswindow"}

    if backend.kind == "x11":
        wmctrl = which("wmctrl", environ=environ)
        if wmctrl:
            result = await run([wmctrl, "-i", "-a", window.id], timeout=_TIMEOUT)
            mechanism = "wmctrl -i -a"
        else:
            xdotool = which("xdotool", environ=environ) or "xdotool"
            result = await run([xdotool, "windowactivate", "--sync", window.id], timeout=_TIMEOUT)
            mechanism = "xdotool windowactivate"
        if not result.ok:
            raise CommandFailedError(
                f"could not activate window {window.id}: {result.stderr.strip()[:200]}",
                capability="windows.focus",
                hint=f"Re-run list_windows() (window ids are not stable across calls) and retry ({mechanism}).",
            )
        return {"mechanism": mechanism}

    raise UnsupportedCapabilityError(
        f"window focus is unavailable on this session: {backend.reason}",
        capability="windows.focus",
        hint="See the windows.focus entry in list_actions().",
        details=backend.details,
    )


async def detect_dbus_compositor(environ: Mapping[str, str]) -> tuple[str, str] | None:
    """Ask the user D-Bus which recognised-but-unusable compositor interfaces are published.

    This turns a generic "unsupported" into something actionable ("KWin is running; here is
    what to enable"), which is the whole point of ``list_actions()`` being the first call an
    agent makes.

    Args:
        environ: Session environment; ``DBUS_SESSION_BUS_ADDRESS`` must be set.

    Returns:
        ``(detected, remediation)`` for the first recognised interface on the bus, or
        ``None`` when nothing matched. Never raises — an unreachable or absent session bus
        is a normal state on a headless machine and must not fail the caller.
    """
    if not environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return None
    busctl = which("busctl", environ=environ)
    if busctl is None:
        return None
    try:
        result = await run(
            [busctl, "--user", "--no-pager", "--no-legend", "list"], timeout=_TIMEOUT
        )
    except Exception:  # a wedged bus must not wedge us: degrade silently
        return None
    if not result.ok:
        return None
    published = {line.split()[0] for line in result.stdout.splitlines() if line.split()}
    for name, (detected, remediation) in _LIMITED_COMPOSITOR_INTERFACES.items():
        if name in published:
            return detected, remediation
    return None
