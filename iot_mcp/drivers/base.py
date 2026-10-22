"""The capability surface every automation backend must implement.

Nothing in this module imports MCP, FastMCP, or any transport. Drivers are plain async
Python objects that return JSON-serializable dicts or raise typed
:class:`~iot_mcp.errors.DriverError` subclasses, which is what makes them unit-testable
without the server stack (see ``tests/``).

Contract for every capability method:

* It is ``async`` — implementations may spawn subprocesses and must not block the loop.
* On success it returns a ``dict`` that survives ``json.dumps`` unchanged (no paths,
  enums, or process objects). ``{"ok": true, ...}`` via :func:`ok`.
* On failure it raises a :class:`~iot_mcp.errors.DriverError` subclass. It never returns
  a traceback, and never raises a bare ``Exception`` the tool layer cannot classify.
* It calls :meth:`AutomationDriver._require` first, so an unsupported capability fails
  with the *reason* the probe found, not with a ``FileNotFoundError``.
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..capabilities import ALL_CAPABILITIES, TOOL_FOR_CAPABILITY, Capability
from ..errors import DependencyMissingError, UnsupportedCapabilityError

__all__ = ["AutomationDriver", "NullDriver", "Support", "ok"]


def ok(capability: Capability | str | None = None, **payload: Any) -> dict[str, Any]:
    """Build the success half of the tool result envelope.

    Args:
        capability: Capability this result belongs to; echoed back for agent clarity.
        **payload: JSON-serializable result fields.

    Returns:
        ``{"ok": true, ...}`` — the single shape every tool result uses, so an agent
        branches on one key for both success and failure.
    """
    result: dict[str, Any] = {"ok": True}
    if capability is not None:
        result["capability"] = str(capability)
    result.update(payload)
    return result


@dataclass(frozen=True)
class Support:
    """Probe verdict for one capability.

    Attributes:
        supported: Whether the capability can run right now on this machine.
        reason: Short human/LLM-readable explanation. On success this names the mechanism
            in use (e.g. ``"Hyprland IPC via hyprctl"``); on failure it names what to fix.
        code: Error code used if the capability is invoked anyway. ``"dependency_missing"``
            means installing a tool fixes it; ``"unsupported"`` means it never will here.
        details: Extra structured context, e.g. detected compositor or missing packages.
    """

    supported: bool
    reason: str
    code: str = "unsupported"
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def yes(cls, reason: str, **details: Any) -> Support:
        """Supported, with the mechanism that will be used."""
        return cls(True, reason, details=details)

    @classmethod
    def no(cls, reason: str, *, dependency: str | None = None, **details: Any) -> Support:
        """Unsupported.

        Args:
            reason: What is missing and why.
            dependency: Set when installing that tool would flip this to supported.
            **details: Extra structured context.
        """
        return cls(
            False,
            reason,
            code="dependency_missing" if dependency else "unsupported",
            details=({"dependency": dependency} | details) if dependency else details,
        )


class AutomationDriver(ABC):
    """Abstract automation backend for one operating system.

    Subclasses own two concerns and nothing else: probing what the host can actually do,
    and performing the work. Tool schemas, transport and discovery live elsewhere.
    """

    #: Short stable identifier reported by ``list_actions()`` ("linux", "macos", ...).
    name: ClassVar[str] = "abstract"

    # ------------------------------------------------------------------ capabilities

    @abstractmethod
    def _probe(self) -> dict[Capability, Support]:
        """Probe the host and return a verdict for **every** capability.

        Implementations must be cheap and must not raise: they run inside ``list_actions()``
        and while handling any other tool call. A missing CLI must produce
        :meth:`Support.no`, never an exception.

        Returns:
            A map covering all of ``ALL_CAPABILITIES``.
        """

    def support(self) -> dict[Capability, Support]:
        """Cached :meth:`_probe` result for this driver instance."""
        cached = getattr(self, "_support_cache", None)
        if cached is None:
            probed = self._probe()
            # Fill any capability a backend forgot, so the matrix is always complete.
            for capability in ALL_CAPABILITIES:
                probed.setdefault(
                    capability,
                    Support.no("backend did not report this capability"),
                )
            self._support_cache = probed
        return self._support_cache  # type: ignore[no-any-return]

    def supports(self, capability: Capability) -> bool:
        """Whether ``capability`` is available right now."""
        entry = self.support().get(capability)
        return bool(entry and entry.supported)

    def _require(self, capability: Capability) -> None:
        """Raise unless ``capability`` is currently available.

        Raises:
            UnsupportedCapabilityError: No amount of setup on this host enables it.
            DependencyMissingError: Installing/starting the named dependency enables it.
        """
        entry = self.support().get(capability)
        if entry is None:
            raise UnsupportedCapabilityError(
                f"{self.name} driver does not implement {capability.value}",
                capability=capability.value,
            )
        if entry.supported:
            return
        kwargs: dict[str, Any] = {
            "capability": capability.value,
            "hint": entry.reason,
            "details": {"driver": self.name, **entry.details},
        }
        if entry.code == DependencyMissingError.code:
            raise DependencyMissingError(
                f"{capability.value} unavailable: {entry.reason}", **kwargs
            )
        raise UnsupportedCapabilityError(
            f"{capability.value} unavailable: {entry.reason}", **kwargs
        )

    def capability_matrix(self) -> dict[str, Any]:
        """Full capability matrix, as returned by the ``list_actions()`` tool.

        Returns:
            ``{"ok": true, "driver": ..., "platform": ..., "capabilities": {name:
            {"supported", "reason", "tool", ...}}, "summary": {...}}``.
        """
        matrix: dict[str, Any] = {}
        supported_count = 0
        for capability in ALL_CAPABILITIES:
            entry = self.support()[capability]
            record: dict[str, Any] = {
                "supported": entry.supported,
                "reason": entry.reason,
                "tool": TOOL_FOR_CAPABILITY[capability],
            }
            if entry.details:
                record["details"] = entry.details
            matrix[capability.value] = record
            supported_count += int(entry.supported)

        return ok(
            driver=self.name,
            platform=sys.platform,
            capabilities=matrix,
            summary={
                "supported": supported_count,
                "total": len(ALL_CAPABILITIES),
                "unsupported": [
                    capability.value
                    for capability in ALL_CAPABILITIES
                    if not self.support()[capability].supported
                ],
            },
        )

    # ----------------------------------------------------------------------- actions

    @abstractmethod
    async def list_apps(self) -> dict[str, Any]:
        """Return launchable applications (see the tool docstring in ``app.py``)."""

    @abstractmethod
    async def open_app(self, name: str) -> dict[str, Any]:
        """Launch an application by name or identifier."""

    @abstractmethod
    async def close_app(self, name: str) -> dict[str, Any]:
        """Ask a running application to terminate."""

    @abstractmethod
    async def open_url(self, url: str) -> dict[str, Any]:
        """Open a URL in the user's default handler."""

    @abstractmethod
    async def media_play_pause(self) -> dict[str, Any]:
        """Toggle playback in the active media player."""

    @abstractmethod
    async def media_next(self) -> dict[str, Any]:
        """Skip to the next track."""

    @abstractmethod
    async def media_previous(self) -> dict[str, Any]:
        """Go back to the previous track."""

    @abstractmethod
    async def media_status(self) -> dict[str, Any]:
        """Report what is playing right now."""

    @abstractmethod
    async def list_windows(self) -> dict[str, Any]:
        """List on-screen windows."""

    @abstractmethod
    async def focus_window(self, app_or_title: str) -> dict[str, Any]:
        """Raise/focus a window by app name or title substring."""

    # -------------------------------------------------------------------- reporting

    def describe(self) -> str:
        """One-line, agent-facing summary used in startup logs."""
        supported = [
            capability.value for capability in ALL_CAPABILITIES if self.supports(capability)
        ]
        return f"{self.name} driver: {len(supported)}/{len(ALL_CAPABILITIES)} capabilities — " + (
            ", ".join(supported) or "none"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.describe()}>"


class NullDriver(AutomationDriver):
    """Fallback driver used when no backend matches the host.

    Exists so the server always boots and always answers ``list_actions()`` with a
    machine-readable explanation, instead of crashing at import or handing the agent a
    traceback. Every capability reports unsupported.
    """

    name = "null"

    def __init__(self, reason: str = "no automation backend matched this host") -> None:
        self._reason = reason

    def _probe(self) -> dict[Capability, Support]:
        return {capability: Support.no(self._reason) for capability in ALL_CAPABILITIES}

    async def list_apps(self) -> dict[str, Any]:
        self._require(Capability.APPS_LIST)
        raise AssertionError("unreachable")  # pragma: no cover

    async def open_app(self, name: str) -> dict[str, Any]:
        self._require(Capability.APPS_OPEN)
        raise AssertionError("unreachable")  # pragma: no cover

    async def close_app(self, name: str) -> dict[str, Any]:
        self._require(Capability.APPS_CLOSE)
        raise AssertionError("unreachable")  # pragma: no cover

    async def open_url(self, url: str) -> dict[str, Any]:
        self._require(Capability.URL_OPEN)
        raise AssertionError("unreachable")  # pragma: no cover

    async def media_play_pause(self) -> dict[str, Any]:
        self._require(Capability.MEDIA_PLAY_PAUSE)
        raise AssertionError("unreachable")  # pragma: no cover

    async def media_next(self) -> dict[str, Any]:
        self._require(Capability.MEDIA_NEXT)
        raise AssertionError("unreachable")  # pragma: no cover

    async def media_previous(self) -> dict[str, Any]:
        self._require(Capability.MEDIA_PREVIOUS)
        raise AssertionError("unreachable")  # pragma: no cover

    async def media_status(self) -> dict[str, Any]:
        self._require(Capability.MEDIA_STATUS)
        raise AssertionError("unreachable")  # pragma: no cover

    async def list_windows(self) -> dict[str, Any]:
        self._require(Capability.WINDOWS_LIST)
        raise AssertionError("unreachable")  # pragma: no cover

    async def focus_window(self, app_or_title: str) -> dict[str, Any]:
        self._require(Capability.WINDOWS_FOCUS)
        raise AssertionError("unreachable")  # pragma: no cover


def _assert_json_safe(payload: dict[str, Any]) -> None:  # pragma: no cover - test helper
    """Raise ``TypeError`` if ``payload`` is not JSON-serializable.

    Used by the test suite to enforce the "plain JSON-serializable dict" contract.
    """
    json.dumps(payload)
