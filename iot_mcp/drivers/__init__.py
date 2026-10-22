"""Driver selection.

``get_driver()`` is the only place that decides *which* backend runs:

1. an explicit ``name`` (the server's ``--driver`` flag) always wins;
2. otherwise ``sys.platform`` picks the backend;
3. otherwise a :class:`~iot_mcp.drivers.base.NullDriver` is returned, which answers every
   capability with a reason instead of raising at import time.

Backends are imported lazily inside the call, so a broken or inapplicable backend can never
take down ``import iot_mcp.drivers`` — the property that keeps the server bootable on a
machine where nothing is installed.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping

from ..errors import InvalidInputError
from .base import AutomationDriver, NullDriver

__all__ = ["DRIVER_ALIASES", "available_drivers", "get_driver", "platform_driver_name"]

#: Accepted ``--driver`` values mapped to the factory that builds them.
DRIVER_ALIASES: dict[str, str] = {
    "linux": "linux",
    "linux2": "linux",
    "freebsd": "linux",  # XDG + playerctl apply; /proc features self-report as unavailable
    "macos": "macos",
    "mac": "macos",
    "osx": "macos",
    "darwin": "macos",
    "windows": "windows",
    "win": "windows",
    "win32": "windows",
    "null": "null",
    "none": "null",
}


def platform_driver_name(platform: str) -> str:
    """Map a ``sys.platform`` value onto a backend key.

    Args:
        platform: A ``sys.platform`` string (or a test stand-in for one).

    Returns:
        ``"linux"``, ``"macos"``, ``"windows"`` or ``"null"``.
    """
    if (
        platform.startswith("linux")
        or platform.startswith("freebsd")
        or platform.startswith("openbsd")
    ):
        return "linux"
    if platform == "darwin":
        return "macos"
    if platform.startswith("win"):
        return "windows"
    return "null"


def get_driver(
    name: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> AutomationDriver:
    """Build the automation driver to serve.

    Never raises for an unmatched platform. A bad explicit ``name`` *does* raise, because
    silently ignoring ``--driver windows`` on a Linux box would be worse than refusing to
    start.

    Args:
        name: Explicit backend key (``linux``, ``macos``, ``windows``, ``null``, or an alias
            such as ``darwin``/``osx``/``win``). ``None`` selects by platform.
        environ: Environment handed to the backend; defaults to the process environment.
        platform: ``sys.platform`` override, used by tests.

    Returns:
        A ready :class:`AutomationDriver`; :class:`NullDriver` when nothing matches or the
        selected backend cannot be imported.

    Raises:
        InvalidInputError: ``name`` is not a recognised backend key.
    """
    key = (name or "").strip().lower()
    if key:
        if key not in DRIVER_ALIASES:
            raise InvalidInputError(
                f"unknown driver {name!r}",
                hint="Choose one of: " + ", ".join(sorted(set(DRIVER_ALIASES))),
                details={"known": sorted(set(DRIVER_ALIASES))},
            )
        target = DRIVER_ALIASES[key]
    else:
        target = platform_driver_name(sys.platform if platform is None else platform)

    if target == "null":
        if key:  # explicitly requested, e.g. `--driver null` for a read-only endpoint
            return NullDriver("the null driver was explicitly selected; no capability will run")
        return NullDriver(
            f"no automation backend for platform "
            f"{sys.platform if platform is None else platform!r}; the server is running headless "
            "or on an unsupported OS"
        )

    try:
        if target == "linux":
            from .linux import LinuxDriver

            return LinuxDriver(environ=environ)
        if target == "macos":
            from .macos import MacOSDriver

            return MacOSDriver(environ=environ)
        if target == "windows":
            from .windows import WindowsDriver

            return WindowsDriver(environ=environ)
    except Exception as exc:  # a backend that cannot load must not take the server down
        return NullDriver(
            f"{target} backend failed to initialise ({type(exc).__name__}: {exc}); "
            "falling back to a null driver"
        )
    return NullDriver(f"driver {target!r} is not implemented")


def available_drivers() -> tuple[str, ...]:
    """Backend keys accepted by :func:`get_driver`."""
    return tuple(sorted(set(DRIVER_ALIASES)))
