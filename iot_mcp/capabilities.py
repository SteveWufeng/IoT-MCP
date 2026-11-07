"""Canonical capability names.

One place defines the capability vocabulary shared by drivers, the capability matrix
returned by ``list_actions()`` and the MCP tool layer. Drivers must never invent new
strings, otherwise ``list_actions()`` would report a matrix an agent cannot correlate
with the tools it can actually call.

Browser capabilities live beside the driver ones but are **not** in
:data:`ALL_CAPABILITIES`, and that separation is load-bearing: ``ALL_CAPABILITIES`` is the
contract every :class:`~iot_mcp.drivers.base.AutomationDriver` must probe, and no driver
drives a browser — the browser tier is an operator-approved add-on bridged to an upstream
MCP server (:mod:`iot_mcp.addons`). It has a lifecycle (approved/disabled/failed) rather
than a probe verdict, so it reports through ``browser_status``/``browser_install_status``
instead of pretending to be a driver capability in ``list_actions()``.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ALL_CAPABILITIES",
    "BROWSER_CAPABILITIES",
    "Capability",
    "TOOL_FOR_CAPABILITY",
]


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


class BrowserCapability(StrEnum):
    """The add-on tier: lifecycle and approval, not OS automation.

    These names are the ``capability`` an :class:`~iot_mcp.errors.DriverError` carries when
    a browser control is refused, so an agent can tell "the add-on is not approved" from
    "the browser is not running" without string-matching.
    """

    STATUS = "browser.status"
    START = "browser.start"
    STOP = "browser.stop"
    INSTALL_STATUS = "browser.install_status"
    REQUEST_INSTALL = "browser.request_install"
    ANNOTATE = "browser.annotate"
    ANNOTATE_CLEAR = "browser.annotate_clear"
    CALL = "browser.call"


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

#: Browser control capabilities. Absent from :data:`ALL_CAPABILITIES` on purpose — see the
#: module docstring. The tools that actually drive pages (``browser_navigate`` and friends)
#: are not listed anywhere: their names and schemas come from the approved add-on at
#: runtime, so any fixed list here would be a claim the server cannot verify.
BROWSER_CAPABILITIES: tuple[BrowserCapability, ...] = (
    BrowserCapability.STATUS,
    BrowserCapability.START,
    BrowserCapability.STOP,
    BrowserCapability.INSTALL_STATUS,
    BrowserCapability.REQUEST_INSTALL,
    BrowserCapability.ANNOTATE,
    BrowserCapability.ANNOTATE_CLEAR,
)

#: Which MCP tool exercises a capability. Emitted in the matrix so an agent reading
#: "media.next: unsupported" knows exactly which tool would have failed. Browser entries
#: are here too so one dict still answers "which tool does this capability belong to" —
#: but only the driver half of it reaches ``list_actions()``.
TOOL_FOR_CAPABILITY: dict[Capability | BrowserCapability, str] = {
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
    BrowserCapability.STATUS: "browser_status",
    BrowserCapability.START: "browser_start",
    BrowserCapability.STOP: "browser_stop",
    BrowserCapability.INSTALL_STATUS: "browser_install_status",
    BrowserCapability.REQUEST_INSTALL: "browser_request_install",
    BrowserCapability.ANNOTATE: "browser_annotate",
    BrowserCapability.ANNOTATE_CLEAR: "browser_annotate_clear",
}
