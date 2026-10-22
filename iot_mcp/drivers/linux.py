"""Linux automation backend.

Mechanisms, all detected lazily and reported through ``list_actions()``:

* **Apps** — freedesktop ``.desktop`` entries under ``XDG_DATA_DIRS``/``XDG_DATA_HOME``;
  launched with ``execvp`` on the tokenised ``Exec`` line (field codes stripped), never a
  shell. Closed by matching ``/proc/<pid>/comm`` and ``exe`` and sending ``SIGTERM``,
  escalating to ``SIGKILL`` after a grace period.
* **URLs** — ``xdg-open`` (validated scheme allowlist first).
* **Media** — MPRIS over D-Bus, driven through the ``playerctl`` CLI. ``playerctl`` is used
  instead of a D-Bus client library because it collapses a lot of bus plumbing into one
  well-tested binary; its absence is detected and reported, never fatal.
* **Windows** — see :mod:`iot_mcp.drivers._linux_wayland`. X11 tools are optional and Wayland
  is never assumed to be X11.

The constructor takes its environment, executable lookup and ``/proc`` root as parameters so
the whole driver is testable from another OS without mocking subprocesses.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Callable, Mapping
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
from . import _linux_wayland
from ._proc import (
    process_alive,
    run,
    spawn_monitored,
    which,
)
from ._url import validate_url
from ._winutil import Window, select_window
from ._xdg import DesktopApp, discover_apps, match_app, strip_field_codes, xdg_app_dirs
from .base import AutomationDriver, Support, ok

__all__ = ["LinuxDriver"]

#: How long an app is allowed to look alive before we call the launch successful. GUI apps
#: exec a long-lived process, so anything that dies inside this window really failed.
_LAUNCH_GRACE_SECONDS = 0.35

#: Grace period between SIGTERM and SIGKILL when closing an app.
_TERMINATE_GRACE_SECONDS = 3.0

#: Safety cap: refuse to signal more processes than this in one call.
_MAX_PIDS_TO_SIGNAL = 64

#: URL schemes ``open_url`` will hand to the desktop's default handler.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto", "ftp", "ftps"})

#: Separator for playerctl ``-f`` output: a character that cannot appear in metadata.
_US = "\x1f"

#: Metadata keys requested from MPRIS, in the order they are formatted.
_METADATA_KEYS = (
    "xesam:title",
    "xesam:artist",
    "xesam:album",
    "xesam:url",
    "mpris:length",
    "xesam:artUrl",
)

_APPS_CACHE_TTL = 30.0


class LinuxDriver(AutomationDriver):
    """Automation backend for Linux desktop sessions (X11 and Wayland)."""

    name = "linux"

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        which_fn: Callable[[str], str | None] | None = None,
        proc_root: str | Path = "/proc",
    ) -> None:
        """Build the driver.

        Args:
            environ: Session environment to probe. Defaults to ``os.environ`` — pass a
                mapping in tests to simulate any distro/session.
            which_fn: Executable lookup hook. Defaults to a ``PATH`` lookup against
                ``environ``.
            proc_root: ``/proc`` equivalent, injectable so process matching is testable.
        """
        self._environ: dict[str, str] = dict(os.environ if environ is None else environ)
        self._which: Callable[[str], str | None] = (
            which_fn if which_fn is not None else lambda name: which(name, environ=self._environ)
        )
        self._proc_root = Path(proc_root)
        self._windows = _linux_wayland.detect_backend(self._environ, self._which)
        self._apps: list[DesktopApp] | None = None
        self._apps_at = 0.0

    @property
    def window_backend(self) -> _linux_wayland.Backend:
        """The detected window mechanism (exposed for diagnostics and tests)."""
        return self._windows

    # ------------------------------------------------------------------ capability probe

    def _probe(self) -> dict[Capability, Support]:
        """Probe every capability from environment + ``PATH`` only (no subprocesses)."""
        app_dirs = [path for path in xdg_app_dirs(self._environ) if path.is_dir()]
        proc_ok = self._proc_root.is_dir()

        playerctl = self._which("playerctl")
        if playerctl:
            media = Support.yes(
                "MPRIS over the session D-Bus, driven by 'playerctl'.", tool=playerctl
            )
        else:
            media = Support.no(
                "'playerctl' is not installed, so MPRIS media keys cannot be sent.",
                dependency="playerctl",
            )

        xdg_open = self._which("xdg-open")
        url_support = (
            Support.yes("xdg-open opens validated http/https/mailto/ftp URLs.", tool=xdg_open)
            if xdg_open
            else Support.no(
                "'xdg-open' is not installed, so URLs cannot be handed to a default handler.",
                dependency="xdg-open",
            )
        )

        support: dict[Capability, Support] = {
            Capability.APPS_LIST: (
                Support.yes(
                    "freedesktop .desktop entries discovered under XDG data directories.",
                    directories=[str(path) for path in app_dirs],
                )
                if app_dirs
                else Support.no(
                    "no application directory exists (XDG_DATA_DIRS/XDG_DATA_HOME point "
                    "nowhere); this looks like a headless install."
                )
            ),
            Capability.APPS_OPEN: (
                Support.yes(
                    "Exec lines are tokenised and passed to execvp — never to a shell.",
                    mechanism="execvp",
                )
                if app_dirs
                else Support.no("no application directories were found")
            ),
            Capability.APPS_CLOSE: (
                Support.yes(
                    "Processes matched by /proc comm/exe are sent SIGTERM, then SIGKILL "
                    f"after {int(_TERMINATE_GRACE_SECONDS)}s."
                )
                if proc_ok
                else Support.no(
                    f"{self._proc_root} is not readable, so processes cannot be matched"
                )
            ),
            Capability.URL_OPEN: url_support,
            Capability.MEDIA_PLAY_PAUSE: media,
            Capability.MEDIA_NEXT: media,
            Capability.MEDIA_PREVIOUS: media,
            Capability.MEDIA_STATUS: media,
            Capability.WINDOWS_LIST: self._window_support(),
            Capability.WINDOWS_FOCUS: self._window_support(),
        }
        return support

    def _window_support(self) -> Support:
        """Translate the detected window backend into a capability verdict.

        ``details`` is copied rather than splatted so a backend that ever reports a
        ``dependency`` key cannot collide with the keyword argument.
        """
        backend = self._windows
        details = dict(backend.details)
        if backend.supported:
            return Support(True, backend.reason, details=details)
        if backend.dependency:
            details["dependency"] = backend.dependency
        return Support(
            False,
            backend.reason,
            code="dependency_missing" if backend.dependency else "unsupported",
            details=details,
        )

    async def _probe_windows_hint(self) -> dict[str, str]:
        """Enrich an unsupported windows verdict with a live D-Bus compositor check.

        Only called on the failure path, so the common (working Hyprland/X11) case never
        spawns ``busctl``.
        """
        found = await _linux_wayland.detect_dbus_compositor(self._environ)
        if found is None:
            return {}
        detected, remediation = found
        return {"detected": detected, "remediation": remediation}

    # --------------------------------------------------------------------------- apps

    def _cached_apps(self) -> list[DesktopApp]:
        """Discover installed apps, re-scanning at most every ``_APPS_CACHE_TTL`` seconds."""
        now = time.monotonic()
        if self._apps is None or (now - self._apps_at) > _APPS_CACHE_TTL:
            self._apps = discover_apps(xdg_app_dirs(self._environ))
            self._apps_at = now
        return self._apps

    async def list_apps(self) -> dict[str, Any]:
        self._require(Capability.APPS_LIST)
        apps = self._cached_apps()
        dirs = [str(path) for path in xdg_app_dirs(self._environ) if path.is_dir()]
        return ok(
            Capability.APPS_LIST,
            count=len(apps),
            apps=[app.to_dict() for app in apps],
            searched_directories=dirs,
            note="Use 'id' or 'name' with open_app/close_app. List is cached for "
            f"{int(_APPS_CACHE_TTL)}s, so freshly installed apps may lag.",
        )

    async def open_app(self, name: str) -> dict[str, Any]:
        self._require(Capability.APPS_OPEN)
        app = match_app(name, self._cached_apps())
        if not app.exec_line.strip():
            raise InvalidInputError(
                f"{app.id!r} has no Exec line, so it cannot be launched",
                capability=Capability.APPS_OPEN.value,
                hint="Pick another app from list_apps(), or launch its binary with a shell yourself.",
                details={"desktop_file": app.path},
            )
        try:
            argv = strip_field_codes(app.exec_line)
        except ValueError as exc:  # unbalanced quoting inside a .desktop Exec value
            raise InvalidInputError(
                f"could not tokenise Exec line of {app.id!r}: {exc}",
                capability=Capability.APPS_OPEN.value,
                details={"exec": app.exec_line[:200]},
            ) from exc
        if not argv:
            raise InvalidInputError(
                f"Exec line of {app.id!r} contains only field codes",
                capability=Capability.APPS_OPEN.value,
                details={"exec": app.exec_line[:200]},
            )

        resolved = self._which(argv[0]) or (argv[0] if Path(argv[0]).is_absolute() else None)
        if resolved is None:
            raise NotFoundError(
                f"{app.id!r} points at executable {argv[0]!r}, which is not on PATH",
                capability=Capability.APPS_OPEN.value,
                hint="The desktop entry is stale (the app was probably uninstalled).",
                details={"desktop_file": app.path, "exec": app.exec_line[:200]},
            )

        process, stderr_tail = await self._spawn_launched(
            [resolved, *argv[1:]], Capability.APPS_OPEN, label=app.id, strict=True
        )
        # A GUI app that survives the grace window is considered launched; we never wait
        # for it to exit, so the event loop stays free.
        return ok(
            Capability.APPS_OPEN,
            app=app.to_dict(),
            launched_argv=[resolved, *argv[1:]],
            pid=process.pid,
            still_running=self._still_running(process),
            stderr_tail=stderr_tail[-500:],
        )

    async def close_app(self, name: str) -> dict[str, Any]:
        self._require(Capability.APPS_CLOSE)
        candidates = self._process_names_for(name)
        pids = await self._match_pids(candidates)
        if not pids:
            raise NotFoundError(
                f"no running process matches {name!r}",
                capability=Capability.APPS_CLOSE.value,
                hint="Call list_windows() or check the exact process name; already-exited "
                "apps are not an error to retry.",
                details={"query": name, "looked_for": sorted(candidates)},
            )
        terminated, forced = await self._signal_then_kill(pids)
        return ok(
            Capability.APPS_CLOSE,
            query=name,
            looked_for=sorted(candidates),
            terminated_pids=terminated,
            force_killed_pids=forced,
            count=len(terminated),
        )

    def _process_names_for(self, name: str) -> set[str]:
        """Derive process names to look for from an app name or desktop id.

        Agents pass anything from ``"firefox"`` to ``"Mozilla Firefox"``; the desktop entry
        (when one matches) tells us the real binary and ``StartupWMClass``.

        Args:
            name: Caller-supplied app identifier.

        Returns:
            Lowercased names to compare against ``/proc/<pid>/comm`` and ``exe`` basenames.
        """
        needle = name.strip().lower()
        if not needle:
            raise InvalidInputError(
                "name must not be empty", capability=Capability.APPS_CLOSE.value
            )
        names = {needle, Path(needle).name}
        try:
            app = match_app(name, self._cached_apps())
        except NotFoundError:
            # No desktop entry is fine: fall back to raw process-name matching, which also
            # covers binaries that simply never shipped a .desktop file.
            app = None
        if app is not None:
            names.add(app.name.lower())
            names.add(app.id.lower())
            if app.desktop_file_id:
                names.add(app.desktop_file_id.lower())
            try:
                argv = strip_field_codes(app.exec_line)
            except ValueError:
                argv = []
            if argv:
                names.add(Path(argv[0]).name.lower())
        return {item for item in names if item}

    def _candidate_processes(self) -> list[tuple[int, str, str]]:
        """Snapshot ``(pid, comm, exe_basename)`` for every readable process.

        Our own process, our parent and PID 1 are excluded: a request to "close python" must
        never take the MCP server down with it.

        Returns:
            Tuples of process id, ``comm`` and executable basename (lowercased where known).
        """
        rows: list[tuple[int, str, str]] = []
        protected = {os.getpid(), os.getppid(), 1}
        try:
            entries = list(self._proc_root.iterdir())
        except OSError:
            return rows
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid in protected:
                continue
            try:
                comm = (
                    (entry / "comm").read_text(encoding="utf-8", errors="replace").strip().lower()
                )
            except OSError:
                continue  # process vanished mid-scan
            exe = ""
            try:
                exe = Path(os.readlink(entry / "exe")).name.lower()
            except OSError:
                pass  # EPERM for other users' processes is normal
            rows.append((pid, comm, exe))
        return rows

    async def _match_pids(self, candidates: set[str]) -> list[int]:
        """Pids whose ``comm`` or ``exe`` basename matches one of ``candidates``.

        Matching is **exact**, with one concession: ``/proc/<pid>/comm`` is truncated by the
        kernel to 15 characters, so a long binary name is compared against the truncated
        candidate. Prefix matching was deliberately rejected — a query like ``"a"`` would
        otherwise match every process starting with "a" and turn ``close_app`` into a
        machine-wide kill.

        ``/proc`` is walked in a worker thread so a machine with thousands of processes
        cannot stall the event loop.

        Args:
            candidates: Lowercased names to match.

        Returns:
            Sorted pids, capped at ``_MAX_PIDS_TO_SIGNAL``.
        """
        rows = await asyncio.to_thread(self._candidate_processes)
        pids: list[int] = []
        for pid, comm, exe in rows:
            for candidate in candidates:
                if comm == candidate or exe == candidate or comm == candidate[:15]:
                    pids.append(pid)
                    break
        pids.sort()
        return pids[:_MAX_PIDS_TO_SIGNAL]

    async def _signal_then_kill(self, pids: list[int]) -> tuple[list[int], list[int]]:
        """Send ``SIGTERM``, wait for exit, then ``SIGKILL`` survivors.

        Returns:
            ``(signalled_pids, force_killed_pids)``.
        """
        signalled: list[int] = []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                signalled.append(pid)
            except ProcessLookupError:
                continue  # already gone: exactly the outcome we wanted
            except PermissionError:
                continue  # another user's process: skip rather than raise mid-sweep

        deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
        forced: list[int] = []
        while time.monotonic() < deadline:
            alive = [pid for pid in signalled if self._is_alive(pid)]
            if not alive:
                break
            await asyncio.sleep(0.15)
        else:
            alive = [pid for pid in signalled if self._is_alive(pid)]

        for pid in alive:
            try:
                os.kill(pid, signal.SIGKILL)
                forced.append(pid)
            except (ProcessLookupError, PermissionError):
                continue
        return signalled, forced

    def _is_alive(self, pid: int) -> bool:
        """Whether ``pid`` still exists (ignoring zombies, which are already dead)."""
        return process_alive(pid, self._proc_root) == "alive"

    def _child_env(self) -> dict[str, str]:
        """Environment handed to launched apps (a copy of the server's)."""
        return dict(self._environ)

    async def _spawn_launched(
        self, argv: list[str], capability: Capability, *, label: str, strict: bool
    ) -> tuple[asyncio.subprocess.Process, str]:
        """Launch a detached helper and decide, within a grace window, whether it took.

        Shared by ``open_app`` and ``open_url``: both hand off to a binary that is expected
        to *survive* (the app outlives the launcher), so "still running when the grace
        window closes" is the success signal.

        Liveness comes from ``/proc`` rather than ``returncode``, because asyncio only fills
        ``returncode`` in once it has reaped the child and the reaper runs on its own
        schedule; a freshly dead app can therefore still report ``None`` here.

        Args:
            argv: Launcher argv, already resolved to an absolute path.
            capability: Capability to name on failure.
            label: What to call the thing in error messages (app id or ``xdg-open``).
            strict: Whether a non-zero exit inside the grace window is an error.
                ``open_url`` passes false: ``xdg-open`` is a wrapper around ~20
                desktop-specific scripts and its exit status is not a reliable signal.

        Returns:
            The child process, plus a snippet of its stderr (empty when it said nothing).

        Raises:
            NotFoundError: The executable vanished between the PATH probe and the exec.
            CommandFailedError: It started and failed immediately, and ``strict`` is set.
        """
        try:
            process, tail = await spawn_monitored(argv, env=self._child_env())
        except FileNotFoundError as exc:
            # `which` and `exec` are two moments in time; a package upgrade can remove the
            # binary between them, and that must surface as `not_found` rather than an
            # OSError escaping to the transport.
            raise NotFoundError(
                f"{argv[0]!r} could not be executed (it no longer exists)",
                capability=capability.value,
                hint="The app was probably uninstalled or upgraded mid-session.",
                details={"argv": list(argv)},
            ) from exc
        await asyncio.sleep(_LAUNCH_GRACE_SECONDS)
        if not self._still_running(process) and process.returncode is None:
            # /proc has already forgotten it but the background reaper has not reported a
            # status yet. Asking here is cheap and idempotent — the reaper awaits the same
            # transport — and without it a crash inside the grace window would be reported
            # as "exited with code None".
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=0.5)
        # A non-zero returncode is proof, not a hint: asyncio only fills it in after reaping.
        if strict and process.returncode not in (None, 0):
            raise CommandFailedError(
                f"{label} exited immediately with {process.returncode}",
                capability=capability.value,
                hint="The app may need a display environment the server lacks.",
                details={
                    "argv": list(argv),
                    "returncode": process.returncode,
                    # The tail, not the head: the line that explains the crash is last.
                    "stderr": tail.text[-500:],
                },
            )
        return process, tail.text

    def _proc_authoritative(self) -> bool:
        """Whether the configured proc root really is a proc filesystem.

        Without one — a unit test, or a platform without ``/proc`` — the only liveness
        signal left is ``returncode``.
        """
        try:
            return (self._proc_root / "1").is_dir()
        except OSError:
            return False

    def _still_running(self, process: asyncio.subprocess.Process) -> bool:
        """Whether a process launched moments ago is still up.

        Prefers ``/proc``, which is authoritative and zombie-aware; ``returncode`` is the
        fallback where there is no proc root.
        """
        if self._proc_authoritative():
            return self._is_alive(process.pid)
        return process.returncode is None

    # -------------------------------------------------------------------------- urls

    async def open_url(self, url: str) -> dict[str, Any]:
        self._require(Capability.URL_OPEN)
        target = validate_url(url, capability=Capability.URL_OPEN)
        launcher = self._which("xdg-open")
        if launcher is None:  # probe said yes, PATH changed underneath us
            raise NotFoundError(
                "'xdg-open' disappeared from PATH",
                capability=Capability.URL_OPEN.value,
                hint="Install xdg-utils.",
            )
        process, stderr_tail = await self._spawn_launched(
            [launcher, target], Capability.URL_OPEN, label="xdg-open", strict=False
        )
        return ok(
            Capability.URL_OPEN,
            url=target,
            launcher="xdg-open",
            pid=process.pid,
            accepted_immediately=process.returncode in (None, 0),
            # xdg-open is a shim over desktop-specific scripts, so its output is the only
            # way to see *why* a URL did not reach a browser.
            stderr_tail=stderr_tail[-500:],
        )

    # ------------------------------------------------------------------------- media

    async def _player_names(self) -> list[str]:
        """Player names currently exposing MPRIS on the bus.

        Raises:
            CommandFailedError: ``playerctl`` failed for a reason other than "no player".
        """
        playerctl = self._require_playerctl()
        result = await run([playerctl, "-l"], timeout=6.0)
        if not result.ok:
            raise CommandFailedError(
                f"playerctl -l failed: {result.stderr.strip()[:200]}",
                capability=Capability.MEDIA_STATUS.value,
                hint="Check that a session D-Bus is running (dbus-launch / a desktop session).",
            )
        return result.lines()

    def _require_playerctl(self) -> str:
        """Path to ``playerctl``, or a typed error explaining how to get it back."""
        path = self._which("playerctl")
        if path is None:
            raise DependencyMissingError(
                "'playerctl' is not installed",
                capability=Capability.MEDIA_STATUS.value,
                hint="Install playerctl (package 'playerctl' on Arch/Fedora, 'playerctl' on Debian).",
            )
        return path

    async def _active_player(self, capability: Capability) -> str:
        """The player a media *command* should be sent to.

        Prefers the one that is actually Playing, then Paused, then whatever is listed:
        broadcasting to every player would double-toggle a system with two media apps open.

        Raises:
            NotFoundError: No MPRIS player is running at all.
        """
        players = await self._player_names()
        if not players:
            raise NotFoundError(
                "no MPRIS media player is running",
                capability=capability.value,
                hint="Start a media player (e.g. Spotify, VLC, or a browser with MPRIS "
                "support such as Firefox/Chrome) and retry.",
                details={"players": []},
            )
        playerctl = self._require_playerctl()
        statuses: dict[str, str] = {}
        for player in players[:8]:
            result = await run([playerctl, "-p", player, "-f", "{{status}}", "status"], timeout=6.0)
            statuses[player] = result.stdout.strip() if result.ok else ""
        for wanted in ("Playing", "Paused", "Stopped", ""):
            for player in players:
                if statuses.get(player, "") == wanted:
                    return player
        return players[0]

    async def _media_command(self, action: str, capability: Capability) -> dict[str, Any]:
        """Send one MPRIS transport command to the active player."""
        player = await self._active_player(capability)
        playerctl = self._require_playerctl()
        result = await run([playerctl, "-p", player, action], timeout=8.0)
        if not result.ok:
            raise CommandFailedError(
                f"playerctl {action} failed: {result.stderr.strip()[:200]}",
                capability=capability.value,
                hint=f"The player {player!r} may not support this command.",
                details={"player": player, "action": action},
            )
        status = await run([playerctl, "-p", player, "-f", "{{status}}", "status"], timeout=6.0)
        return ok(
            capability,
            player=player,
            action=action,
            status=status.stdout.strip() if status.ok else "Unknown",
            output=result.stdout.strip(),
        )

    async def media_play_pause(self) -> dict[str, Any]:
        return await self._media_command("play-pause", Capability.MEDIA_PLAY_PAUSE)

    async def media_next(self) -> dict[str, Any]:
        return await self._media_command("next", Capability.MEDIA_NEXT)

    async def media_previous(self) -> dict[str, Any]:
        return await self._media_command("previous", Capability.MEDIA_PREVIOUS)

    async def media_status(self) -> dict[str, Any]:
        self._require(Capability.MEDIA_STATUS)
        players = await self._player_names()
        if not players:
            return ok(
                Capability.MEDIA_STATUS,
                playing=None,
                players=[],
                note="No MPRIS player is running on the session bus.",
            )
        playerctl = self._require_playerctl()
        # Metadata is requested with an explicit unit-separator-delimited format so parsing
        # never depends on playerctl's default "key - value" rendering (which is ambiguous
        # for titles containing " - ").
        format_string = _US.join(f"{{{{{key}}}}}" for key in _METADATA_KEYS)
        entries: list[dict[str, Any]] = []
        for player in players[:8]:
            status = await run([playerctl, "-p", player, "status"], timeout=6.0)
            # `playerctl status` exits non-zero for a player that is present but idle, which
            # is a real state to report rather than a failure, so metadata is read anyway.
            metadata = await run(
                [playerctl, "-p", player, "-f", format_string, "metadata"], timeout=6.0
            )
            entry: dict[str, Any] = {
                "player": player,
                "status": status.stdout.strip() if status.ok else "Unknown",
            }
            if metadata.ok and metadata.stdout:
                fields = metadata.stdout.splitlines()[-1].split(_US)
                entry.update(
                    {
                        key.split(":", 1)[-1]: (value.strip() or None)
                        for key, value in zip(_METADATA_KEYS, fields, strict=False)
                    }
                )
            entries.append(entry)
        current = next((item for item in entries if item.get("status") == "Playing"), None)
        return ok(Capability.MEDIA_STATUS, playing=current, players=entries)

    # ----------------------------------------------------------------------- windows

    async def _require_windows(self, capability: Capability) -> None:
        """Gate the window capabilities, enriching a refusal with a live D-Bus diagnosis.

        The static probe is pure on purpose — ``list_actions()`` runs it on every request
        and must not spawn processes — so call time is the only moment a KWin or GNOME
        session can actually be identified. Without this, an agent on KDE is told "no window
        API" with no idea that KWin is right there and what to enable.

        Raises:
            UnsupportedCapabilityError: This session has no window API.
            DependencyMissingError: Installing a tool would fix it.
        """
        try:
            self._require(capability)
        except (UnsupportedCapabilityError, DependencyMissingError) as exc:
            hint = await self._probe_windows_hint()
            if hint:
                exc.details.update(hint)
                exc.hint = hint["remediation"]
            raise

    async def _enumerate_windows(self) -> list[Window]:
        """List windows, enriching a "not supported" verdict with a live D-Bus check.

        The extra ``busctl`` probe runs only on the failure path, so a working Hyprland or
        X11 session never pays for it. Its purpose is to upgrade "no window API" into "KWin
        is running and here is what you must enable".
        """
        try:
            return await _linux_wayland.list_windows(self._windows, self._environ)
        except UnsupportedCapabilityError as exc:
            hint = await self._probe_windows_hint()
            if hint:
                exc.details.update(hint)
                exc.hint = hint["remediation"]
            raise

    async def list_windows(self) -> dict[str, Any]:
        await self._require_windows(Capability.WINDOWS_LIST)
        windows = await self._enumerate_windows()
        return ok(
            Capability.WINDOWS_LIST,
            count=len(windows),
            windows=[window.to_dict() for window in windows],
            backend=self._windows.kind,
            note=self._windows.reason,
        )

    async def focus_window(self, app_or_title: str) -> dict[str, Any]:
        await self._require_windows(Capability.WINDOWS_FOCUS)
        windows = await self._enumerate_windows()
        target: Window = select_window(app_or_title, windows, target=Capability.WINDOWS_FOCUS.value)
        detail = await _linux_wayland.focus_window(self._windows, target, self._environ)
        return ok(
            Capability.WINDOWS_FOCUS,
            window=target.to_dict(),
            backend=self._windows.kind,
            already_focused=target.focused,
            **detail,
        )
