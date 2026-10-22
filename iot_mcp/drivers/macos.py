"""macOS automation backend: ``open``, AppleScript, and System Events.

**Not executed on the Linux host this package was developed on** — see "What is actually
verified" in ``docs/internals.md``. It is written to be read and audited: every AppleScript
fragment is built through :func:`escape_applescript`, and launching goes through ``open``
with an argument vector, so no user input ever reaches a shell.

Mechanisms:

* **Apps** — ``.app`` bundles under ``/Applications`` and friends, with ``Info.plist`` read
  directly by :mod:`plistlib` (no subprocess). Launched with ``open -b <bundle id>`` or
  ``open -a <name>``; quit with the app's own AppleScript ``quit`` verb, falling back to
  ``SIGTERM``.
* **URLs** — ``open <url>`` behind the shared scheme allowlist.
* **Media** — per-player AppleScript dictionaries (Music, Spotify, QuickTime, VLC). macOS
  has no system-wide "now playing" bus equivalent to MPRIS, and built-in keyboards expose no
  virtual key code for next/previous track, so app dictionaries are the reliable route.
* **Windows** — the Accessibility API through System Events. Requires the process running the
  server to hold Automation/Accessibility permission for the target apps; the first call after
  a fresh install pops a TCC consent dialog, which is reported as ``command_failed`` with a
  hint rather than silently doing nothing.
"""

from __future__ import annotations

import os
import plistlib
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..capabilities import Capability
from ..errors import (
    CommandFailedError,
    DependencyMissingError,
    InvalidInputError,
    NotFoundError,
    UnsupportedCapabilityError,
)
from ._proc import run, spawn_detached, which
from ._url import validate_url
from ._winutil import Window, select_window
from .base import AutomationDriver, Support, ok

__all__ = ["MacOSDriver", "escape_applescript"]

_US = "\x1f"

#: Bundle roots searched for ``.app`` bundles, in priority order.
_APP_DIRS: tuple[str, ...] = (
    "/Applications",
    "/Applications/Utilities",
    "~/Applications",
    "/System/Applications",
    "/System/Applications/Utilities",
    "/System/Library/CoreServices/Applications",
)

#: Players with a usable AppleScript dictionary: process name -> (display name, verbs).
#: Verbs are the property/method names in each app's own dictionary; nothing here is a
#: synthesised keystroke, which is what makes it reliable across macOS versions.
_KNOWN_PLAYERS: dict[str, tuple[str, dict[str, str]]] = {
    "Music": (
        "Music",
        {
            "play_pause": "playpause",
            "next": "next track",
            "previous": "previous track",
            "status": "player state",
            "title": "name of current track",
            "artist": "artist of current track",
            "album": "album of current track",
        },
    ),
    "Spotify": (
        "Spotify",
        {
            "play_pause": "playpause",
            "next": "next track",
            "previous": "previous track",
            "status": "player state",
            "title": "name of current track",
            "artist": "artist of current track",
            "album": "album of current track",
        },
    ),
    "VLC": (
        "VLC",
        {
            "play_pause": "playpause",
            "next": "next",
            "previous": "previous",
            "status": "playback state",
        },
    ),
    "QuickTime Player": (
        "QuickTime Player",
        {
            "play_pause": "playpause",
            "next": "",
            "previous": "",
            "status": "playing",
        },
    ),
}

_TIMEOUT = 12.0

#: Process names this backend will never quit.
#:
#: ``close_app`` is driven by an LLM, and a mistyped name is one keystroke away from
#: ending the user's session: ``loginwindow`` is the login session itself, ``WindowServer``
#: takes the whole screen down, and ``launchd``/``Dock``/``Finder`` break the desktop in
#: ways that look like a crash. They are refused rather than confirmed, because the MVP has
#: no confirmation channel.
PROTECTED_PROCESSES = frozenset(
    {
        "launchd",
        "loginwindow",
        "WindowServer",
        "Dock",
        "Finder",
        "SystemUIServer",
        "cfprefsd",
        "Spotlight",
        "notificationcenter",
    }
)
_TERMINATE_GRACE_SECONDS = 4.0


def _bundle_dir(raw: str, environ: Mapping[str, str]) -> Path:
    """Resolve one :data:`_APP_DIRS` entry into a concrete path.

    ``~`` is expanded against the **driver's** environment rather than the real ``HOME``, so
    tests can point the driver at a fixture bundle tree on any OS.
    """
    if raw.startswith("~/"):
        return Path(environ.get("HOME", ""), raw[2:])
    return Path(raw)


def escape_applescript(value: str) -> str:
    """Make ``value`` safe to embed inside a double-quoted AppleScript string literal.

    AppleScript has no parameter binding, so string injection is a genuine code-execution
    vector: an unescaped quote in ``open_app('x" & (do shell script "rm -rf /") & "')`` would
    otherwise run arbitrary shell commands as the user. Backslash is escaped first, then the
    quote; newlines and carriage returns are removed because they terminate a literal and
    would let a caller append whole statements.

    Args:
        value: Untrusted text (app name, window title, ...).

    Returns:
        The escaped text, **without** surrounding quotes.
    """
    if not isinstance(value, str):
        raise InvalidInputError(f"expected str, got {type(value).__name__}")
    text = value.replace("\x00", "")
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    for char, replacement in (("\r", " "), ("\n", " "), ("\t", " "), (_US, " ")):
        text = text.replace(char, replacement)
    return text


def applescript_quote(value: str) -> str:
    """Return ``value`` as a complete quoted AppleScript string literal."""
    return f'"{escape_applescript(value)}"'


@dataclass(frozen=True)
class MacApp:
    """One discovered ``.app`` bundle.

    Attributes:
        name: Display name (bundle directory stem or ``CFBundleName``).
        bundle_id: ``CFBundleIdentifier`` when present — the stable launch handle.
        executable: ``CFBundleExecutable``, used for process matching.
        path: Absolute ``.app`` path.
    """

    name: str
    bundle_id: str
    executable: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form returned by ``list_apps``."""
        return {
            "id": self.bundle_id or self.name,
            "name": self.name,
            "bundle_id": self.bundle_id,
            "executable": self.executable,
            "path": self.path,
        }


class UnsupportedVerbError(UnsupportedCapabilityError):
    """A known player's dictionary lacks the requested verb (e.g. QuickTime next track)."""

    def __init__(self, player: str, verb_key: str, capability: Capability) -> None:
        super().__init__(
            f"{player!r} does not support {verb_key!r} through its AppleScript dictionary",
            capability=capability.value,
            hint="Use list_actions() to see which players are supported, or control the "
            "player from its own UI.",
            details={"player": player, "verb": verb_key},
        )


class MacOSDriver(AutomationDriver):
    """Automation backend for macOS 12+ desktop sessions."""

    name = "macos"

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        which_fn: Callable[[str], str | None] | None = None,
    ) -> None:
        """Build the driver.

        Args:
            environ: Session environment (``HOME`` is read for ``~/Applications``).
            which_fn: Executable lookup hook, injectable for tests.
        """
        self._environ: dict[str, str] = dict(os.environ if environ is None else environ)
        self._which: Callable[[str], str | None] = (
            which_fn if which_fn is not None else lambda name: which(name, environ=self._environ)
        )

    # ------------------------------------------------------------------ capability probe

    def _probe(self) -> dict[Capability, Support]:
        osascript = self._which("osascript")
        opener = self._which("open")
        if osascript is None:  # pragma: no cover - never true on a real macOS
            base = Support.no(
                "'osascript' is not available, so AppleScript cannot run.",
                dependency="osascript",
            )
        else:
            base = Support.yes("AppleScript via osascript; string arguments are escaped.")

        app_support = (
            Support.yes(
                ".app bundles discovered under /Applications and ~/Applications; Info.plist "
                "read directly with plistlib.",
                directories=list(_APP_DIRS),
            )
            if any(_bundle_dir(path, self._environ).is_dir() for path in _APP_DIRS)
            else Support.no("no application bundle directory exists on this volume")
        )
        opener_support = (
            Support.yes(f"{opener} launches apps and opens validated URLs.", tool=opener)
            if opener
            else Support.no("'open' is not available", dependency="open")
        )

        accessibility_reason = (
            "System Events Accessibility API; the first call needs TCC Automation consent "
            "for the calling process."
            if osascript
            else "AppleScript (osascript) is unavailable, so the System Events API cannot be reached."
        )
        return {
            Capability.APPS_LIST: app_support,
            Capability.APPS_OPEN: app_support if opener else opener_support,
            Capability.APPS_CLOSE: base,
            Capability.URL_OPEN: opener_support,
            Capability.MEDIA_PLAY_PAUSE: self._media_support(base),
            Capability.MEDIA_NEXT: self._media_support(base),
            Capability.MEDIA_PREVIOUS: self._media_support(base),
            Capability.MEDIA_STATUS: self._media_support(base),
            Capability.WINDOWS_LIST: Support(
                osascript is not None,
                accessibility_reason,
                code="dependency_missing" if osascript is None else "unsupported",
                details={"dependency": "osascript"} if osascript is None else {},
            ),
            Capability.WINDOWS_FOCUS: Support(
                osascript is not None,
                accessibility_reason
                + (" Window control also needs 'set frontmost' + AXRaise." if osascript else ""),
                code="dependency_missing" if osascript is None else "unsupported",
                details={"dependency": "osascript"} if osascript is None else {},
            ),
        }

    @staticmethod
    def _media_support(base: Support) -> Support:
        """Media capability shares the AppleScript verdict, with the mechanism spelled out."""
        if not base.supported:
            return base
        return Support.yes(
            "Per-player AppleScript dictionaries ("
            + ", ".join(sorted(_KNOWN_PLAYERS))
            + "). macOS has no system-wide now-playing bus, so a player must be running.",
            players=sorted(_KNOWN_PLAYERS),
        )

    # --------------------------------------------------------------------------- apps

    def _discover_apps(self) -> list[MacApp]:
        """Find ``.app`` bundles and read their ``Info.plist``.

        Returns:
            Apps sorted by display name. Unreadable plists are skipped rather than raising:
            a single broken bundle must not remove the whole app list.
        """
        apps: dict[str, MacApp] = {}
        for raw in _APP_DIRS:
            base = _bundle_dir(raw, self._environ)
            try:
                if not base.is_dir():
                    continue
                # One level of nesting covers /Applications/Utilities and vendor folders.
                candidates = sorted(base.glob("*.app"))
                for child in sorted(item for item in base.iterdir() if item.is_dir()):
                    candidates.extend(sorted(child.glob("*.app")))
            except OSError:
                continue
            for bundle in candidates:
                app = self._read_bundle(bundle)
                if app is not None and app.name.lower() not in apps:
                    apps[app.name.lower()] = app
        return sorted(apps.values(), key=lambda item: item.name.lower())

    def _read_bundle(self, bundle: Path) -> MacApp | None:
        """Read one bundle's identity from ``Contents/Info.plist``."""
        name = bundle.name[: -len(".app")]
        plist_path = bundle / "Contents" / "Info.plist"
        bundle_id, executable = "", ""
        try:
            with plist_path.open("rb") as handle:
                info = plistlib.load(handle)
            if isinstance(info, Mapping):
                bundle_id = str(info.get("CFBundleIdentifier", "") or "")
                executable = str(info.get("CFBundleExecutable", "") or "")
                display = str(info.get("CFBundleName", "") or "")
                if display:
                    name = display
                if str(info.get("LSUIElement", "")).lower() in {"1", "true", "yes"}:
                    return None  # agent without a UI is not a launchable "app"
        except (OSError, ValueError):
            # Unreadable or malformed Info.plist: the bundle directory name is still a usable
            # identity and `open -a` will find it, so skip the plist rather than the app.
            pass
        return MacApp(name=name, bundle_id=bundle_id, executable=executable, path=str(bundle))

    def _match(self, query: str, apps: list[MacApp]) -> MacApp:
        """Pick the app a caller meant by name, bundle id, or executable name."""
        needle = (query or "").strip().lower()
        if not needle:
            raise InvalidInputError(
                "name must not be empty",
                capability=Capability.APPS_OPEN.value,
                hint="Pass an app name like 'Safari' or a bundle id like 'com.apple.Safari'.",
            )
        for app in apps:
            if needle in {app.name.lower(), app.bundle_id.lower(), app.executable.lower()}:
                return app
        for app in apps:
            if app.name.lower().startswith(needle) or app.bundle_id.lower().startswith(needle):
                return app
        for app in apps:
            if needle in app.name.lower() or needle in app.bundle_id.lower():
                return app
        raise NotFoundError(
            f"no installed application matches {query!r}",
            capability=Capability.APPS_OPEN.value,
            hint="Call list_apps() and use an exact 'name' or 'bundle_id'.",
            details={
                "query": query,
                "scanned_apps": len(apps),
                "candidates": [a.name for a in apps[:15]],
            },
        )

    async def list_apps(self) -> dict[str, Any]:
        self._require(Capability.APPS_LIST)
        apps = self._discover_apps()
        return ok(
            Capability.APPS_LIST,
            count=len(apps),
            apps=[app.to_dict() for app in apps],
            searched_directories=list(_APP_DIRS),
        )

    async def open_app(self, name: str) -> dict[str, Any]:
        self._require(Capability.APPS_OPEN)
        apps = self._discover_apps()
        app = self._match(name, apps)
        opener = self._which("open") or "/usr/bin/open"
        argv = [opener, "-b", app.bundle_id] if app.bundle_id else [opener, "-a", app.name]
        result = await run(argv, timeout=_TIMEOUT)
        if not result.ok:
            raise NotFoundError(
                f"open could not launch {app.name!r}: {result.stderr.strip()[:200]}",
                capability=Capability.APPS_OPEN.value,
                hint="Check the bundle still exists (list_apps()) and that Gatekeeper allows it.",
                details={"argv": argv, "stderr": result.stderr.strip()[:300]},
            )
        return ok(Capability.APPS_OPEN, app=app.to_dict(), launched_with=argv)

    async def close_app(self, name: str) -> dict[str, Any]:
        self._require(Capability.APPS_CLOSE)
        apps = self._discover_apps()
        try:
            app = self._match(name, apps)
        except NotFoundError:
            app = MacApp(name=name.strip(), bundle_id="", executable=name.strip(), path="")
        self._refuse_protected(app)
        script = f"tell application {applescript_quote(app.name)} to quit"
        result = await self._osascript(script, Capability.APPS_CLOSE, allow_failure=True)
        if result == "ok":
            return ok(Capability.APPS_CLOSE, app=app.to_dict(), method="AppleScript quit")
        # Not every app is AppleScript-aware (or consent was denied): fall back to SIGTERM
        # on the bundle executable, which is what a user's Force Quit ultimately does.
        executable = app.executable or app.name
        pids = await self._pgrep(executable)
        if not pids:
            raise NotFoundError(
                f"{app.name!r} is not running and could not be quit by AppleScript",
                capability=Capability.APPS_CLOSE.value,
                hint="Confirm the exact name with list_apps() or list_windows().",
                details={"apple_script_result": result},
            )
        killed: list[int] = []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except OSError:
                continue
        return ok(
            Capability.APPS_CLOSE,
            app=app.to_dict(),
            method="SIGTERM",
            terminated_pids=killed,
            note="AppleScript quit was refused, so the process was signalled instead.",
        )

    def _refuse_protected(self, app: MacApp) -> None:
        """Refuse to quit a process whose death would take the session down with it.

        Raises:
            InvalidInputError: The name resolves to a protected system process.
        """
        for candidate in (app.executable, app.name):
            if candidate and candidate in PROTECTED_PROCESSES:
                raise InvalidInputError(
                    f"{candidate!r} is a protected system process and will not be quit",
                    capability=Capability.APPS_CLOSE.value,
                    hint="Only user applications can be closed through this server.",
                    details={"app": app.name, "protected": True},
                )

    async def _pgrep(
        self, process_name: str, *, cap: Capability = Capability.APPS_CLOSE
    ) -> list[int]:
        """Pids of running processes named exactly ``process_name``.

        Raises:
            DependencyMissingError: ``pgrep`` is missing (never true on macOS).
        """
        pgrep = self._which("pgrep")
        if pgrep is None:
            raise DependencyMissingError(
                "'pgrep' is not available",
                capability=str(cap),
                hint="This backend assumes a standard macOS userland.",
            )
        result = await run([pgrep, "-x", process_name], timeout=6.0)
        if not result.ok:
            return []  # pgrep exits 1 when nothing matches: an ordinary answer
        pids: list[int] = []
        for line in result.lines():
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid not in {os.getpid(), os.getppid(), 1}:
                pids.append(pid)
        return pids

    # -------------------------------------------------------------------------- urls

    async def open_url(self, url: str) -> dict[str, Any]:
        self._require(Capability.URL_OPEN)
        target = validate_url(url, capability=Capability.URL_OPEN)
        opener = self._which("open") or "/usr/bin/open"
        await spawn_detached([opener, target])
        return ok(Capability.URL_OPEN, url=target, launcher="open")

    # ------------------------------------------------------------------------- media

    async def _osascript(
        self, script: str, capability: Capability, *, allow_failure: bool = False
    ) -> Any:
        """Run one AppleScript fragment through ``osascript -e``.

        Args:
            script: The script text. Every interpolated value must already have gone through
                :func:`escape_applescript`.
            capability: Capability recorded on raised errors.
            allow_failure: Return the string ``"ok"`` or ``"failed"`` instead of raising.

        Returns:
            Stripped stdout when ``allow_failure`` is false and the script succeeded; the
            strings ``"ok"``/``"failed"`` when ``allow_failure`` is true.

        Raises:
            DependencyMissingError: ``osascript`` is unavailable.
            CommandFailedError: The script raised — commonly error ``-1743`` (Automation
                consent denied) or ``-1719`` (target app not running).
        """
        exe = self._which("osascript")
        if exe is None:
            raise DependencyMissingError(
                "'osascript' is not available",
                capability=str(capability),
                hint="This backend requires macOS with /usr/bin/osascript.",
            )
        result = await run([exe, "-e", script], timeout=_TIMEOUT)
        if allow_failure:
            return "ok" if result.ok else "failed"
        if not result.ok:
            detail = result.stderr.strip()[:300]
            hint = "AppleScript failed."
            if "-1743" in detail:
                hint = (
                    "Automation consent is required: System Settings > Privacy & Security > "
                    "Automation, allow the terminal/server process to control the target app."
                )
            elif "-1728" in detail or "-600" in detail:
                hint = "The target application is not running; start it first."
            raise CommandFailedError(
                f"AppleScript failed: {detail}", capability=str(capability), hint=hint
            )
        return result.stdout.strip()

    async def _running_players(self) -> list[str]:
        """Names of running apps that have a usable media AppleScript dictionary."""
        running: list[str] = []
        for process_name in _KNOWN_PLAYERS:
            if await self._pgrep(process_name, cap=Capability.MEDIA_STATUS):
                running.append(process_name)
        return running

    async def _media_command(self, verb_key: str, capability: Capability) -> dict[str, Any]:
        """Send one transport verb to the first running known player."""
        players = await self._running_players()
        if not players:
            raise NotFoundError(
                "no supported media player is running",
                capability=capability.value,
                hint=(
                    "Supported players: "
                    + ", ".join(sorted(_KNOWN_PLAYERS))
                    + ". macOS has no system-wide media keys bus, so one of these must be open."
                ),
                details={"players": []},
            )
        player = players[0]
        _display, verbs = _KNOWN_PLAYERS[player]
        verb = verbs.get(verb_key, "")
        if not verb:
            raise UnsupportedVerbError(player, verb_key, capability)
        script = f"tell application {applescript_quote(player)} to {verb}"
        await self._osascript(script, capability)
        return ok(capability, player=player, verb=verb)

    async def media_play_pause(self) -> dict[str, Any]:
        return await self._media_command("play_pause", Capability.MEDIA_PLAY_PAUSE)

    async def media_next(self) -> dict[str, Any]:
        return await self._media_command("next", Capability.MEDIA_NEXT)

    async def media_previous(self) -> dict[str, Any]:
        return await self._media_command("previous", Capability.MEDIA_PREVIOUS)

    async def media_status(self) -> dict[str, Any]:
        self._require(Capability.MEDIA_STATUS)
        players = await self._running_players()
        if not players:
            return ok(
                Capability.MEDIA_STATUS,
                playing=None,
                players=[],
                note="No supported player is running (Music, Spotify, VLC, QuickTime Player).",
            )
        entries: list[dict[str, Any]] = []
        for player in players:
            _display, verbs = _KNOWN_PLAYERS[player]
            entry: dict[str, Any] = {"player": player, "status": None}
            for field_key in ("status", "title", "artist", "album"):
                verb = verbs.get(field_key, "")
                if not verb:
                    continue
                script = f"tell application {applescript_quote(player)} to get {verb}"
                try:
                    value = await self._osascript(script, Capability.MEDIA_STATUS)
                except (CommandFailedError, DependencyMissingError):
                    value = ""  # e.g. nothing loaded in the player: report the gap, keep going
                if value:
                    entry[field_key] = value
            entries.append(entry)
        return ok(
            Capability.MEDIA_STATUS,
            playing=next((item for item in entries if item.get("status")), None),
            players=entries,
        )

    # ----------------------------------------------------------------------- windows

    async def _windows(self) -> list[Window]:
        """Enumerate windows through System Events' Accessibility API.

        One script returns every ``(process, window)`` pair, delimited by ``\\x1f`` and
        newlines, because per-window round trips would be dozens of process spawns.

        Raises:
            CommandFailedError: Accessibility/Automation permission is missing, or the
                WindowServer refused the query.
        """
        script = (
            'tell application "System Events"\n'
            '  set output to ""\n'
            "  repeat with proc in (every process whose background only is false)\n"
            "    repeat with win in (windows of proc)\n"
            "      try\n"
            '        set output to output & (name of proc) & "' + _US + '" & (name of win) & "\n"\n'
            "      end try\n"
            "    end repeat\n"
            "  end repeat\n"
            "  return output\n"
            "end tell"
        )
        raw = await self._osascript(script, Capability.WINDOWS_LIST)
        windows: list[Window] = []
        focused = await self._frontmost_process()
        for line in str(raw).splitlines():
            if _US not in line:
                continue
            process_name, _, title = line.partition(_US)
            windows.append(
                Window(
                    id=f"{process_name}{_US}{title}",
                    app=process_name.strip(),
                    title=title.strip(),
                    focused=process_name.strip().lower() == focused,
                )
            )
        return windows

    async def _frontmost_process(self) -> str:
        """Name of the frontmost process, lowercased; ``""`` if unobtainable."""
        script = (
            'tell application "System Events" to get name of first process whose frontmost is true'
        )
        try:
            return (await self._osascript(script, Capability.WINDOWS_LIST)).lower()
        except (CommandFailedError, DependencyMissingError):
            return ""

    async def list_windows(self) -> dict[str, Any]:
        self._require(Capability.WINDOWS_LIST)
        windows = await self._windows()
        return ok(
            Capability.WINDOWS_LIST,
            count=len(windows),
            windows=[window.to_dict() for window in windows],
            backend="system-events",
            note="Requires Accessibility/Automation consent for the server process.",
        )

    async def focus_window(self, app_or_title: str) -> dict[str, Any]:
        self._require(Capability.WINDOWS_FOCUS)
        windows = await self._windows()
        target = select_window(app_or_title, windows, target=Capability.WINDOWS_FOCUS.value)
        process_name, _, title = target.id.partition(_US)
        script = (
            f'tell application "System Events" to tell process {applescript_quote(process_name)}\n'
            "  set frontmost to true\n"
            f'  perform action "AXRaise" of window {applescript_quote(title)}\n'
            "end tell"
        )
        await self._osascript(script, Capability.WINDOWS_FOCUS)
        return ok(
            Capability.WINDOWS_FOCUS,
            window=target.to_dict(),
            backend="system-events",
            already_focused=target.focused,
        )
