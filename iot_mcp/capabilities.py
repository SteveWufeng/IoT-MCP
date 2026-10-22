"""Canonical capability names.

One place defines the capability vocabulary shared by drivers, the capability matrix
returned by ``list_actions()`` and the MCP tool layer. Drivers must never invent new
strings, otherwise ``list_actions()`` would report a matrix an agent cannot correlate
with the tools it can actually call.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ALL_CAPABILITIES", "Capability", "TOOL_FOR_CAPABILITY"]


class Capability(StrEnum):
    """Every capability an :class:`~iot_mcp.drivers.base.AutomationDriver` may provide."""

    APPS_LIST = "apps.list"
    APPS_OPEN = "apps.open"
    APPS_CLOSE = "apps.close"
    URL_OPEN = "url.open"
    MEDIA_PLAY_PAUSE = "media.play_pause"
    MEDIA_NEXT = "media.next"
    MEDIA_PREVIOUS = "media.previous"
    MEDIA_STATUS = "media.status"
    WINDOWS_LIST = "windows.list"
    WINDOWS_FOCUS = "windows.focus"


#: Fixed order for the capability matrix, so `list_actions()` output is stable/diffable.
ALL_CAPABILITIES: tuple[Capability, ...] = (
    Capability.APPS_LIST,
    Capability.APPS_OPEN,
    Capability.APPS_CLOSE,
    Capability.URL_OPEN,
    Capability.MEDIA_PLAY_PAUSE,
    Capability.MEDIA_NEXT,
    Capability.MEDIA_PREVIOUS,
    Capability.MEDIA_STATUS,
    Capability.WINDOWS_LIST,
    Capability.WINDOWS_FOCUS,
)

#: Which MCP tool exercises a capability. Emitted in the matrix so an agent reading
#: "media.next: unsupported" knows exactly which tool would have failed.
TOOL_FOR_CAPABILITY: dict[Capability, str] = {
    Capability.APPS_LIST: "list_apps",
    Capability.APPS_OPEN: "open_app",
    Capability.APPS_CLOSE: "close_app",
    Capability.URL_OPEN: "open_url",
    Capability.MEDIA_PLAY_PAUSE: "media_play_pause",
    Capability.MEDIA_NEXT: "media_next",
    Capability.MEDIA_PREVIOUS: "media_previous",
    Capability.MEDIA_STATUS: "media_status",
    Capability.WINDOWS_LIST: "list_windows",
    Capability.WINDOWS_FOCUS: "focus_window",
}
