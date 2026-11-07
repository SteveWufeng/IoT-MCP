"""Shared plumbing for the MCP tool layer.

The tool functions in :mod:`iot_mcp.app` and :mod:`iot_mcp.browser` all honour one contract:
they return a JSON-serialisable envelope with an ``ok`` key and never let an exception (or a
traceback) reach the model. Both layers need that normalisation and the same annotation
builder, and neither should import the other — the browser tier is optional and must stay
importable without dragging in the driver stack's assumptions.

Kept free of MCP *server* imports on purpose: only :class:`mcp.types.ToolAnnotations`, so
this stays import-cheap and testable exactly like :mod:`iot_mcp.errors`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.types import ToolAnnotations

from .errors import DriverError

__all__ = ["annotations_for", "invoke"]


async def invoke(action: Callable[[], Any]) -> dict[str, Any]:
    """Run one action and normalise the outcome to the tool result envelope.

    Args:
        action: Zero-argument callable returning either a result dict or an awaitable of
            one, so async and synchronous paths share a single error-handling path.

    Returns:
        The action's result dict, or :meth:`iot_mcp.errors.DriverError.to_dict` for any
        typed failure. Unexpected exceptions are converted too, because an unhandled
        traceback in a tool response is useless to an agent.
    """
    try:
        outcome = action()
        return await outcome if hasattr(outcome, "__await__") else outcome
    except DriverError as exc:
        return exc.to_dict()
    except Exception as exc:  # pragma: no cover - tool bug, reported not raised
        return {
            "ok": False,
            "error": {
                "type": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
                "hint": "The server raised an unexpected error; report it or try a different "
                "capability from list_actions().",
            },
        }


def annotations_for(
    title: str, *, read_only: bool, destructive: bool = False, open_world: bool = False
) -> ToolAnnotations:
    """Build tool annotations, so a client can gate dangerous calls without parsing text.

    Args:
        title: Human-facing label.
        read_only: Whether the tool only observes.
        destructive: Whether it can disrupt user work (closing apps, stealing focus).
        open_world: Whether the call reaches outside the machine. ``open_url`` is the only
            driver tool that does; every real browser tool is open-world by definition, so
            the bridge copies the upstream's own annotations rather than second-guessing
            them.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=read_only,
        openWorldHint=open_world,
    )
