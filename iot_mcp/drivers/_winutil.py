"""Shared window value type and matching logic.

Window listing is backend-specific, but *choosing* the window a caller meant is not. This
module holds the neutral :class:`Window` shape plus a pure :func:`select_window` that every
backend reuses, so the fuzzy-matching behaviour an agent sees is identical on Linux, macOS
and Windows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import InvalidInputError, NotFoundError

__all__ = ["Window", "select_window"]


@dataclass(frozen=True)
class Window:
    """One on-screen window, normalised across windowing backends.

    Attributes:
        id: Backend-native handle (Hyprland address, X11 window id, macOS window index).
            Opaque to callers, required by backends to focus it.
        app: Application identity — WM_CLASS, bundle id, or process name. May be empty
            when the backend cannot tell.
        title: Window title, may be empty for apps that set none.
        focused: Whether this window currently has input focus.
        workspace: Desktop/workspace/tag label, when the backend exposes one.
        extra: Backend-specific fields kept for debuggability (pid, class, geometry).
    """

    id: str
    app: str = ""
    title: str = ""
    focused: bool = False
    workspace: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form returned by ``list_windows``."""
        payload: dict[str, Any] = {
            "id": self.id,
            "app": self.app,
            "title": self.title,
            "focused": self.focused,
        }
        if self.workspace:
            payload["workspace"] = self.workspace
        if self.extra:
            payload.update(self.extra)
        return payload

    def haystack(self) -> list[str]:
        """Lowercased strings a query may match against, best first."""
        return [value.lower() for value in (self.title, self.app, self.id) if value]


def select_window(query: str, windows: list[Window], *, target: str = "windows.focus") -> Window:
    """Choose the window ``query`` refers to.

    Precedence: exact title, exact app, title prefix, app prefix, title substring, app
    substring, query-contains-title. Case-insensitive. A query that matches only the
    already-focused window still returns it, so the call is idempotent.

    Args:
        query: App name or title substring supplied by the agent.
        windows: Candidate windows from a backend.
        target: Capability name used in error attribution.

    Returns:
        The best-matching window.

    Raises:
        InvalidInputError: ``query`` is blank.
        NotFoundError: Nothing matched; the error lists up to 15 visible window titles so
            the agent can retry with something real.
    """
    if not query or not query.strip():
        raise InvalidInputError(
            "app_or_title must not be empty",
            capability=target,
            hint="Pass an app name or a distinctive title substring from list_windows().",
        )
    needle = query.strip().lower()
    scored: list[tuple[int, Window]] = []
    for window in windows:
        title = window.title.lower()
        app = window.app.lower()
        if title == needle or app == needle:
            score = 0
        elif title.startswith(needle) or app.startswith(needle):
            score = 1
        elif needle in title:
            score = 2
        elif needle in app:
            score = 3
        elif title and title in needle:
            score = 4
        else:
            continue
        scored.append((score, window))

    if not scored:
        raise NotFoundError(
            f"no window matches {query!r}",
            capability=target,
            hint="Call list_windows() and match on an exact 'title' or 'app' value.",
            details={
                "query": query,
                "visible_windows": len(windows),
                "candidates": [
                    {"title": window.title, "app": window.app} for window in windows[:15]
                ],
            },
        )
    # Prefer an exact/strong match, then the currently focused window on ties.
    scored.sort(key=lambda item: (item[0], not item[1].focused, item[1].title.lower()))
    return scored[0][1]
