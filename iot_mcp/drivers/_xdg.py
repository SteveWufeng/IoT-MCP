"""XDG desktop-entry discovery and parsing (Linux).

Pure, filesystem-parameterised logic with no subprocess calls, so app discovery can be
unit-tested from any host OS by pointing it at a fixture directory.

Spec reference: the freedesktop.org Desktop Entry Specification. We read only the
``[Desktop Entry]`` group, which is where ``Name``/``Exec``/``NoDisplay`` live.
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import InvalidInputError, NotFoundError

__all__ = [
    "DesktopApp",
    "discover_apps",
    "match_app",
    "parse_desktop_entry",
    "strip_field_codes",
    "xdg_app_dirs",
]

#: Keys that mark an entry as "do not show the user an app for this".
_HIDDEN_KEYS = ("NoDisplay", "Hidden")

_DEFAULT_DATA_DIRS = ("/usr/local/share", "/usr/share")


@dataclass(frozen=True)
class DesktopApp:
    """One launchable application discovered from a ``.desktop`` file.

    Attributes:
        id: Desktop-entry id (filename minus the ``.desktop`` suffix), e.g.
            ``org.gnome.Nautilus``. This is the stable handle callers pass to
            ``open_app``.
        name: Localised ``Name`` as written in the entry.
        exec_line: Raw ``Exec`` value, field codes included.
        comment: ``Comment`` value, may be empty.
        path: Absolute path of the ``.desktop`` file this came from.
        try_exec: ``TryExec`` value used to check the real binary exists, may be empty.
        desktop_file_id: ``StartupWMClass`` when present — the best hint for matching a
            window back to an app.
    """

    id: str
    name: str
    exec_line: str
    comment: str = ""
    path: str = ""
    try_exec: str = ""
    desktop_file_id: str = ""
    categories: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form returned by the ``list_apps`` tool."""
        return {
            "id": self.id,
            "name": self.name,
            "exec": self.exec_line,
            "comment": self.comment,
            "desktop_file": self.path,
            "startup_wm_class": self.desktop_file_id,
            "categories": list(self.categories),
        }


def parse_desktop_entry(
    text: str, *, app_id: str | None = None, path: str = ""
) -> DesktopApp | None:
    """Parse the ``[Desktop Entry]`` group of a ``.desktop`` file.

    Malformed files are common in the wild (vendored editors ship odd ones), so this
    returns ``None`` instead of raising whenever the entry is unusable.

    Args:
        text: Full file contents.
        app_id: Id to use; defaults to the first group name being irrelevant, so callers
            normally pass the filename stem.
        path: Origin file path, recorded for diagnostics.

    Returns:
        A :class:`DesktopApp`, or ``None`` if there is no ``[Desktop Entry]`` group or no
        ``Name``.
    """
    in_entry = False
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if in_entry:
                break  # second group: we already have what we need
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), value.strip())

    name = values.get("Name")
    if not name:
        return None
    if any(values.get(key, "").lower() == "true" for key in _HIDDEN_KEYS):
        return None

    categories = tuple(
        part.strip() for part in values.get("Categories", "").split(";") if part.strip()
    )
    return DesktopApp(
        id=app_id or name.lower().replace(" ", "-"),
        name=name,
        exec_line=values.get("Exec", ""),
        comment=values.get("Comment", ""),
        path=path,
        try_exec=values.get("TryExec", ""),
        desktop_file_id=values.get("StartupWMClass", ""),
        categories=categories,
    )


def strip_field_codes(exec_line: str) -> list[str]:
    """Tokenise an ``Exec`` value and drop desktop-entry field codes.

    ``Exec`` may legitimately contain ``%U``, ``%F``, ``%u`` etc. placeholders that only
    make sense when a file/URL is being handed to the app. Launching the app with no
    document means those tokens must disappear — never be passed literally, and **never**
    be shell-expanded.

    Args:
        exec_line: The raw ``Exec`` string.

    Returns:
        An argument vector suitable for ``execvp`` (may be empty if nothing is left).

    Raises:
        ValueError: The line cannot be tokenised (unbalanced quotes).
    """
    if not exec_line.strip():
        return []
    tokens = shlex.split(exec_line, posix=True)
    argv: list[str] = []
    for token in tokens:
        if len(token) == 2 and token[0] == "%" and token[1].isalpha():
            continue  # %u, %U, %f, %F, %d, %D, %n, %N, %i, %c, %k
        if token == "%%":
            argv.append("%")
            continue
        # A field code glued to a value, e.g. file:%U — drop the code part only.
        cleaned = token.replace("%U", "").replace("%u", "").replace("%F", "").replace("%f", "")
        argv.append(cleaned.strip() or token)
    return [token for token in argv if token]


def xdg_app_dirs(environ: dict[str, str], home: str | None = None) -> list[Path]:
    """Directories that may contain ``.desktop`` files, in spec priority order.

    Args:
        environ: Environment mapping (``XDG_DATA_HOME`` / ``XDG_DATA_DIRS`` are read).
        home: Home directory fallback for the default ``XDG_DATA_HOME``.

    Returns:
        Existing-and-nonexistent paths alike, de-duplicated, highest priority first.
    """
    home_dir = home or environ.get("HOME") or str(Path.home())
    data_home = environ.get("XDG_DATA_HOME") or posixpath.join(home_dir, ".local", "share")
    raw_dirs = environ.get("XDG_DATA_DIRS") or ":".join(_DEFAULT_DATA_DIRS)
    candidates = [posixpath.join(data_home, "applications")]
    candidates += [posixpath.join(part, "applications") for part in raw_dirs.split(":") if part]
    out: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = posixpath.normpath(candidate)
        if text not in seen:
            seen.add(text)
            out.append(Path(text))
    return out


def discover_apps(
    dirs: list[Path],
    *,
    limit: int = 500,
    recurse: int = 2,
) -> list[DesktopApp]:
    """Collect launchable apps from ``.desktop`` files under ``dirs``.

    Sub-directories (``extras/``, ``org.kde.*``) are searched to a bounded depth. Entries
    from a higher-priority directory shadow the same id from a lower one, per spec.

    Args:
        dirs: Priority-ordered search directories, typically from :func:`xdg_app_dirs`.
        limit: Maximum number of apps returned; keeps a tool response inside sane bounds.
        recurse: How many directory levels below each ``applications`` dir to search.

    Returns:
        Apps sorted by name.
    """
    found: dict[str, DesktopApp] = {}
    for base in dirs:
        try:
            if not base.is_dir():
                continue
            paths = sorted(_walk_limited(base, recurse))
        except OSError:
            continue  # unreadable directory is a non-event for capability reporting
        for path in paths:
            try:
                # Capped read: a .desktop file is tiny, and a pathological FIFO or huge
                # symlink target must not be slurped into memory during a tool call.
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read(64_000)
            except OSError:
                continue
            app = parse_desktop_entry(text, app_id=path.stem, path=str(path))
            if app is None or app.id in found:
                continue
            found[app.id] = app
            if len(found) >= limit:
                return sorted(found.values(), key=lambda item: item.name.lower())
    return sorted(found.values(), key=lambda item: item.name.lower())


def _walk_limited(root: Path, depth: int) -> list[Path]:
    """Yield ``*.desktop`` files under ``root`` up to ``depth`` levels down."""
    results: list[Path] = []

    def visit(directory: Path, remaining: int) -> None:
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if remaining > 0 and not entry.name.startswith("."):
                    visit(entry, remaining - 1)
            elif entry.suffix == ".desktop":
                results.append(entry)

    visit(root, depth)
    return results


def match_app(query: str, apps: list[DesktopApp]) -> DesktopApp:
    """Pick the app a caller most likely meant.

    Precedence: exact desktop id, exact name, punctuation-insensitive id, name/id prefix
    or ``StartupWMClass`` match, substring, then the case where the whole name appears
    somewhere in a longer sentence. Matching is case-insensitive. Ties on a given score go
    to the *shortest* name, because "Firefox" is a better answer for ``firefox`` than
    "Firefox Developer Edition" — alphabetical order alone would have made the qualifier
    win whenever it happened to sort first.

    Args:
        query: What the agent asked for (``"firefox"``, ``"Firefox"`` or
            ``"org.mozilla.firefox"``).
        apps: Candidate apps.

    Raises:
        ~iot_mcp.errors.NotFoundError: Nothing matched. The error carries up to 15
            candidate names so the agent can retry with a real id instead of guessing.
    """
    if not query or not query.strip():
        raise InvalidInputError(
            "name must not be empty", capability="apps.open", hint="Pass an app name or desktop id."
        )
    needle = query.strip().lower()
    ranked: list[tuple[int, DesktopApp]] = []
    for app in apps:
        app_id = app.id.lower()
        name = app.name.lower()
        if app_id == needle or name == needle:
            score = 0
        elif app_id.replace("-", "").replace(".", "") == needle.replace("-", "").replace(".", ""):
            score = 1
        elif (
            name.startswith(needle)
            or app_id.startswith(needle)
            # "firefox-browser.desktop" typed in full is as good a prefix match as a name.
            or app.desktop_file_id.lower() == needle
        ):
            score = 2
        elif needle in name or needle in app_id:
            score = 3
        elif name in needle:  # caller passed "open firefox browser"
            score = 4
        else:
            continue
        ranked.append((score, app))

    if not ranked:
        raise NotFoundError(
            f"no installed application matches {query!r}",
            capability="apps.open",
            hint="Call list_apps() and use an exact 'id' or 'name' from its output.",
            details={
                "query": query,
                "scanned_apps": len(apps),
                "candidates": [app.name for app in apps[:15]],
            },
        )
    ranked.sort(key=lambda item: (item[0], len(item[1].name), item[1].name.lower()))
    return ranked[0][1]
