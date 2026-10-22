"""Typed error surface for automation drivers.

This module is deliberately free of MCP and transport imports so that drivers can be
imported and unit-tested in isolation from the server stack.

Every driver failure surfaces as a :class:`DriverError` subclass carrying a stable
machine-readable ``code`` plus an operator/LLM-facing ``hint``. The MCP tool layer turns
these into a JSON envelope via :meth:`DriverError.to_dict` rather than letting a traceback
reach the model.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CommandFailedError",
    "CommandTimeoutError",
    "DependencyMissingError",
    "DriverError",
    "InvalidInputError",
    "NotFoundError",
    "UnsupportedCapabilityError",
]


class DriverError(Exception):
    """Base class for every capability failure raised by a driver.

    Attributes:
        code: Stable machine-readable identifier (e.g. ``"unsupported"``). Agents can
            branch on it without string-matching the message.
        capability: Capability name the failure belongs to (e.g. ``"media.play_pause"``),
            or ``None`` when the failure is not capability specific.
        hint: Actionable next step for the human or agent reading the error.
        details: Extra JSON-serializable context (exit codes, stderr tails, candidates).
    """

    code: str = "driver_error"

    def __init__(
        self,
        message: str,
        *,
        capability: str | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.capability = capability
        self.hint = hint
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable error envelope used by every MCP tool.

        The envelope shape is stable: ``{"ok": false, "error": {...}}``. Success results
        use the same ``ok`` key set to ``true``, so a caller only ever has to inspect one
        field to decide whether a call worked.
        """
        error: dict[str, Any] = {
            "type": self.code,
            "message": str(self),
        }
        if self.capability is not None:
            error["capability"] = self.capability
        if self.hint:
            error["hint"] = self.hint
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


class UnsupportedCapabilityError(DriverError):
    """The running driver cannot provide this capability at all on this machine."""

    code = "unsupported"


class DependencyMissingError(DriverError):
    """A required external CLI tool or service is not installed / not running.

    Distinct from :class:`UnsupportedCapabilityError`: installing the named dependency
    (or starting the named service) makes the capability work without a code change.
    """

    code = "dependency_missing"


class NotFoundError(DriverError):
    """A named app, window or player could not be resolved."""

    code = "not_found"


class CommandFailedError(DriverError):
    """A subprocess exited non-zero."""

    code = "command_failed"


class CommandTimeoutError(DriverError):
    """A subprocess did not finish inside its deadline and was killed.

    Surfacing this as its own type matters: a hung helper (a media player that stopped
    answering D-Bus, for instance) must never be able to wedge the MCP server.
    """

    code = "timeout"


class InvalidInputError(DriverError):
    """Caller-supplied input was rejected before anything was executed."""

    code = "invalid_input"
