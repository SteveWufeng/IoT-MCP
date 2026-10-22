"""Windows automation backend: PowerShell + Win32 P/Invoke.

**Not executed on the Linux host this package was developed on** — see "What is actually
verified" in ``docs/internals.md``. Read it as an audit target.

Injection model (the important part): PowerShell has no argument escaping that is safe to
hand-roll, so user input is **never interpolated into a script**. Scripts are static text sent
on stdin (``powershell -Command -``) and every caller-supplied value travels in a child-only
environment variable (``IOT_MCP_ARG0`` …) that the script reads through ``$env:``. The result
is that a name containing ``"; Remove-Item C:\\*`` is data, not code, regardless of quoting
rules. :func:`quote_single` exists only for the two places where a literal is unavoidable and
is unit-tested.

Mechanisms:

* **Apps** — installed launchables are the Start Menu ``.lnk`` set; launched with
  ``Start-Process``; closed by ``CloseMainWindow()`` then ``Stop-Process -Force``.
* **URLs** — ``Start-Process`` behind the shared scheme allowlist.
* **Media** — ``SendInput`` with the dedicated consumer media virtual key codes
  (``VK_MEDIA_PLAY_PAUSE`` 0xB3, ``VK_MEDIA_NEXT_TRACK`` 0xB0, ``VK_MEDIA_PREV_TRACK`` 0xB1).
  These are injected at the system level, so they reach whatever owns the media transport.
* **Media status** — reported **unsupported** on purpose: "what's playing" lives in SMTC
  (WinRT ``GlobalSystemMediaTransportControlsSessionManager``), which cannot be reached
  reliably from a PowerShell child. Pretending otherwise would be a silent lie to the agent;
  ``list_actions()`` says so explicitly.
* **Windows** — ``user32`` ``EnumWindows``/``GetWindowText``/``SetForegroundWindow``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from ..capabilities import Capability
from ..errors import (
    CommandFailedError,
    DependencyMissingError,
    InvalidInputError,
    NotFoundError,
    UnsupportedCapabilityError,
)
from ._proc import run, which
from ._url import validate_url
from ._winutil import Window, select_window
from .base import AutomationDriver, Support, ok

__all__ = ["WindowsDriver", "quote_single"]

_TIMEOUT = 20.0

#: Environment variables used to smuggle caller input past the script text, by position.
_ARG_PREFIX = "IOT_MCP_ARG"

#: Appended to a pipeline so the driver parses structure instead of PowerShell's
#: human-formatted table text. Note the bare command form: writing ``$args | ConvertTo-Json``
#: inside a pipeline scriptblock would bind ``$args`` to the scriptblock parameters,
#: not the piped object.
_FORMAT = "ConvertTo-Json -Compress"


def quote_single(value: str) -> str:
    """Escape ``value`` for a PowerShell single-quoted literal.

    Single-quoted PowerShell strings perform no interpolation at all; the only escape is
    doubling the quote itself. Used solely where a value must be a script literal (none of
    the shipped scripts need it today — kept because it is the *correct* primitive if one
    ever does, and it is unit-tested).

    Args:
        value: Text to make safe.

    Returns:
        The escaped text without surrounding quotes.
    """
    if not isinstance(value, str):
        raise InvalidInputError(f"expected str, got {type(value).__name__}")
    return value.replace("\x00", "").replace("'", "''')\n('")


class _Script:
    """A static PowerShell script plus the positional arguments it may read from ``$env:``.

    Keeping script text and arguments separate is what makes the injection story auditable:
    grep this file for ``f"`` near a ``$env:`` and you will find nothing.
    """

    __slots__ = ("body", "args")

    def __init__(self, body: str, *args: str) -> None:
        self.body = body
        self.args = args

    def env_for(self, base: Mapping[str, str]) -> dict[str, str]:
        """Return ``base`` plus this script's arguments as ``IOT_MCP_ARG<n>``.

        Args:
            base: Environment the child should start from.

        Returns:
            A new mapping; ``base`` is never mutated.
        """
        env = dict(base)
        for index, value in enumerate(self.args):
            env[f"{_ARG_PREFIX}{index}"] = value
        return env


#: PowerShell-source literals for the two Start Menu roots. These are package constants, not
#: caller data, and they must be *double* quoted in PowerShell so ``$env:`` expands.
_START_MENU_DIRS = (
    "$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs",
    "$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs",
)

_SCRIPT_LIST_APPS = f"""
$ErrorActionPreference = 'Stop'
$roots = @({", ".join(f'"{path}"' for path in _START_MENU_DIRS)})
$found = New-Object System.Collections.Generic.List[object]
foreach ($root in $roots) {{
    if (-not (Test-Path -LiteralPath $root)) {{ continue }}
    Get-ChildItem -LiteralPath $root -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object {{
            $found.Add([pscustomobject]@{{
                name = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)
                path = $_.FullName
                source = 'start-menu'
            }})
        }}
}}
Write-Output ($found | Sort-Object name -Unique | {_FORMAT})
"""

_SCRIPT_OPEN_APP = """
$ErrorActionPreference = 'Stop'
$target = $env:IOT_MCP_ARG0
if ([System.IO.Path]::GetExtension($target) -eq '.lnk') {
    if (-not (Test-Path -LiteralPath $target)) { throw "not found: $target" }
    Start-Process -FilePath $target
} else {
    $cmd = Get-Command -Name $target -ErrorAction SilentlyContinue
    if ($cmd) { Start-Process -FilePath $cmd.Source }
    else { Start-Process -FilePath $target }
}
Write-Output '{"started": $true}'
"""

_SCRIPT_CLOSE_APP = """
$ErrorActionPreference = 'Stop'
$name = $env:IOT_MCP_ARG0
$like = if ($name -match '[*\\?]') { $name } else { "$name*" }
$targets = Get-Process | Where-Object {
    $_.ProcessName -like $like -and $_.Id -ne $PID
}
if (-not $targets) { throw "no running process matches $name" }
$graceful = @(); $forced = @()
foreach ($proc in $targets) {
    $closed = $false
    try {
        if ($proc.MainWindowHandle -ne 0) { $closed = $proc.CloseMainWindow() }
    } catch { $closed = $false }
    if ($closed) { $graceful += $proc.Id } else { $forced += $proc.Id }
}
$remaining = @()
foreach ($id in $graceful) {
    $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($proc -and -not $proc.WaitForExit(3000)) { $remaining += $id }
}
foreach ($id in ($forced + $remaining)) {
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
}
Write-Output (@{
    terminated = ($graceful + $forced)
    force_killed = ($forced + $remaining)
} | ConvertTo-Json -Compress)
"""

_SCRIPT_OPEN_URL = """
$ErrorActionPreference = 'Stop'
Start-Process -FilePath $env:IOT_MCP_ARG0
Write-Output '{"opened": $true}'
"""

#: C# shim compiled by Add-Type once per media call. SendInput is the only documented way to
#: synthesise the hardware media keys, which have no Win32 message equivalent.
_SENDINPUT_TYPE = r"""
using System;
using System.Runtime.InteropServices;
public static class MediaKeys {
    [StructLayout(LayoutKind.Sequential)]
    struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Explicit)]
    struct INPUTUNION { [FieldOffset(0)] public KEYBDINPUT ki; }
    [StructLayout(LayoutKind.Sequential)]
    struct INPUT { public uint type; public INPUTUNION U; }
    [DllImport("user32.dll", SetLastError = true)]
    static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
    const uint KEYEVENTF_EXTENDEDKEY = 0x1;
    const uint KEYEVENTF_KEYUP = 0x2;
    public static bool Send(ushort vk) {
        INPUT[] inputs = new INPUT[2];
        inputs[0].type = 1;
        inputs[0].U.ki.wVk = vk;
        inputs[0].U.ki.dwFlags = KEYEVENTF_EXTENDEDKEY;
        inputs[1].type = 1;
        inputs[1].U.ki.wVk = vk;
        inputs[1].U.ki.dwFlags = KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP;
        return SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT))) == inputs.Length;
    }
}
"""

_SCRIPT_MEDIA_KEY = (
    """
$ErrorActionPreference = 'Stop'
if (-not ('MediaKeys' -as [type])) { Add-Type -TypeDefinition @'
"""
    + _SENDINPUT_TYPE
    + """
'@ }
$vk = [Convert]::ToUInt16($env:IOT_MCP_ARG0)
if (-not [MediaKeys]::Send($vk)) { throw "SendInput failed for VK 0x$('{0:X}' -f $vk)" }
Write-Output ('{"sent_vk": ' + $vk + '}')
"""
)

_SCRIPT_LIST_WINDOWS = r"""
$ErrorActionPreference = 'Stop'
if (-not ('WinEnum.WinInfo' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
namespace WinEnum {
  public class WinInfo { public string Id; public string Title; public string Process; public bool Focused; }
  public static class Api {
    public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int maxCount);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    public static List<WinInfo> List() {
      var list = new List<WinInfo>();
      IntPtr fg = GetForegroundWindow();
      EnumWindows((h, l) => {
        if (!IsWindowVisible(h)) return true;
        var sb = new StringBuilder(512);
        GetWindowTextW(h, sb, sb.Capacity);
        if (sb.Length == 0) return true;
        uint pid; GetWindowThreadProcessId(h, out pid);
        string procName = "";
        try { procName = Process.GetProcessById((int)pid).ProcessName; } catch { }
        list.Add(new WinInfo {
          Id = "0x" + ((long)h).ToString("x"),
          Title = sb.ToString(), Process = procName, Focused = (h == fg)
        });
        return true;
      }, IntPtr.Zero);
      return list;
    }
  }
}
'@ }
Write-Output ([WinEnum.Api]::List() | ConvertTo-Json -Compress)
"""

_SCRIPT_FOCUS_WINDOW = """
$ErrorActionPreference = 'Stop'
if (-not ('WinFocus' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class WinFocus {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  public static bool Raise(long handle) {
    IntPtr h = new IntPtr(handle);
    ShowWindowAsync(h, 9);            // SW_RESTORE: und minimise before the raise
    return SetForegroundWindow(h);
  }
}
'@ }
$handle = [Convert]::ToInt64($env:IOT_MCP_ARG0, 16)
if (-not [WinFocus]::Raise($handle)) {
  throw "SetForegroundWindow refused the request (Windows foreground lock); the window was restored"
}
Write-Output ('{"focused": "' + $env:IOT_MCP_ARG0 + '"}')
"""

#: Consumer-control virtual key codes (see Winuser.h); these are hardware media keys, which
#: is why they reach the player that actually owns media transport rather than the foreground app.
_MEDIA_VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1}


def _parse_rows(text: str) -> list[dict[str, Any]]:
    """Decode a PowerShell ``ConvertTo-Json -Compress`` payload into rows.

    ``ConvertTo-Json`` emits an array for collections, a bare object for a single item, and
    *nothing* for an empty result — three shapes every caller has to survive.

    Args:
        text: Captured stdout.

    Returns:
        A list of dicts (possibly empty). Non-object JSON scalars are dropped.
    """
    payload = (text or "").strip()
    if not payload:
        return []
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return [] if payload.startswith(("{", "[")) else [{"raw": payload}]
    if isinstance(decoded, list):
        return [item for item in decoded if isinstance(item, dict)]
    if isinstance(decoded, dict):
        return [decoded]
    return []


class WindowsDriver(AutomationDriver):
    """Automation backend for Windows 10/11 desktop sessions."""

    name = "windows"

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        which_fn: Callable[[str], str | None] | None = None,
        path_exists: Callable[[str], bool] | None = None,
    ) -> None:
        """Build the driver.

        Args:
            environ: Session environment (``APPDATA``/``ProgramData`` locate the Start Menu).
            which_fn: Executable lookup hook, injectable for tests.
            path_exists: Directory-existence hook, injectable so the Start Menu probe can be
                exercised from a non-Windows test host.
        """
        self._environ: dict[str, str] = dict(os.environ if environ is None else environ)
        self._which: Callable[[str], str | None] = (
            which_fn if which_fn is not None else lambda name: which(name, environ=self._environ)
        )
        self._exists: Callable[[str], bool] = (
            path_exists if path_exists is not None else os.path.isdir
        )

    def _start_menu_dirs(self) -> list[str]:
        """Concrete Start Menu ``Programs`` paths for this account/machine."""
        roots = [
            os.path.join(self._environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu"),
            os.path.join(
                self._environ.get("ProgramData", r"C:\ProgramData"),
                "Microsoft",
                "Windows",
                "Start Menu",
            ),
        ]
        return [os.path.join(root, "Programs") for root in roots if root]

    def _probe(self) -> dict[Capability, Support]:
        shell = self._which("powershell.exe") or self._which("powershell") or self._which("pwsh")
        if shell is None:
            shell_support = Support.no(
                "no PowerShell on PATH, so no Win32 bridge is available.", dependency="powershell"
            )
        else:
            shell_support = Support.yes(
                "PowerShell child process; caller input is passed via environment variables, "
                "never interpolated into script text.",
                shell=shell,
            )
        start_menu = [path for path in self._start_menu_dirs() if self._exists(path)]
        apps_support = (
            Support.yes(
                "Start Menu .lnk shortcuts enumerate installed launchables.",
                directories=start_menu,
            )
            if start_menu
            else Support.no(
                "no Start Menu Programs directory was found for this account; installed apps "
                "cannot be enumerated (open_app can still launch by executable name)"
            )
        )
        return {
            Capability.APPS_LIST: apps_support if shell else shell_support,
            Capability.APPS_OPEN: shell_support,
            Capability.APPS_CLOSE: shell_support,
            Capability.URL_OPEN: shell_support,
            Capability.MEDIA_PLAY_PAUSE: shell_support,
            Capability.MEDIA_NEXT: shell_support,
            Capability.MEDIA_PREVIOUS: shell_support,
            Capability.MEDIA_STATUS: Support.no(
                "Windows publishes now-playing state through SMTC (WinRT "
                "GlobalSystemMediaTransportControlsSessionManager), which is not reachable "
                "from a PowerShell child process. Play/pause, next and previous still work "
                "via injected media keys; reading track metadata needs a WinRT client "
                "(planned, deliberately not faked here).",
            ),
            Capability.WINDOWS_LIST: shell_support,
            Capability.WINDOWS_FOCUS: shell_support,
        }

    async def _ps(self, script: _Script, capability: Capability) -> str:
        """Run ``script`` in a PowerShell child and return its stdout.

        The script body goes in on stdin via ``-Command -`` and caller data arrives as
        ``IOT_MCP_ARG<n>`` environment variables, so nothing needs quoting.

        Args:
            script: Static body plus positional arguments.
            capability: Capability recorded on raised errors.

        Returns:
            Raw stdout text.

        Raises:
            DependencyMissingError: PowerShell is not installed.
            CommandFailedError: The script threw (mapped from stderr where possible).
        """
        shell = self._which("powershell.exe") or self._which("powershell") or self._which("pwsh")
        if shell is None:
            raise DependencyMissingError(
                "PowerShell is not available",
                capability=str(capability),
                hint="This backend requires Windows PowerShell 5.1 or PowerShell 7+.",
            )
        result = await run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", "-"],
            stdin=script.body,
            env=script.env_for(self._environ),
            timeout=_TIMEOUT,
        )
        if not result.ok:
            detail = result.stderr.strip()[:400]
            raise CommandFailedError(
                f"PowerShell reported a failure: {detail}",
                capability=str(capability),
                hint="See the mechanism description in list_actions() for what this backend can do.",
                details={"stderr": detail},
            )
        return result.stdout

    async def list_apps(self) -> dict[str, Any]:
        self._require(Capability.APPS_LIST)
        rows = _parse_rows(await self._ps(_Script(_SCRIPT_LIST_APPS), Capability.APPS_LIST))
        apps = [
            {
                "id": row.get("name", ""),
                "name": row.get("name", ""),
                "path": row.get("path", ""),
                "source": row.get("source", "start-menu"),
            }
            for row in rows
            if row.get("name")
        ]
        return ok(Capability.APPS_LIST, count=len(apps), apps=apps)

    async def open_app(self, name: str) -> dict[str, Any]:
        self._require(Capability.APPS_OPEN)
        query = (name or "").strip()
        if not query:
            raise InvalidInputError(
                "name must not be empty",
                capability=Capability.APPS_OPEN.value,
                hint="Pass a Start Menu shortcut name from list_apps(), or an executable.",
            )
        target = query
        if not query.lower().endswith(".lnk"):
            # Prefer the Start Menu shortcut: it exists even when the app is not on PATH,
            # which is the common case for GUI apps installed per-user.
            for row in await self._installed():
                if str(row.get("name", "")).lower() == query.lower():
                    target = str(row.get("path", "")) or query
                    break
        await self._ps(_Script(_SCRIPT_OPEN_APP, target), Capability.APPS_OPEN)
        return ok(Capability.APPS_OPEN, requested=query, launched=target)

    async def _installed(self) -> list[dict[str, Any]]:
        """Installed Start Menu rows, or an empty list if that probe fails."""
        try:
            return _parse_rows(await self._ps(_Script(_SCRIPT_LIST_APPS), Capability.APPS_LIST))
        except (CommandFailedError, DependencyMissingError):
            return []

    async def close_app(self, name: str) -> dict[str, Any]:
        self._require(Capability.APPS_CLOSE)
        query = (name or "").strip()
        if not query:
            raise InvalidInputError(
                "name must not be empty", capability=Capability.APPS_CLOSE.value
            )
        try:
            stdout = await self._ps(_Script(_SCRIPT_CLOSE_APP, query), Capability.APPS_CLOSE)
        except CommandFailedError as exc:
            if "no running process matches" in str(exc):
                raise NotFoundError(
                    f"no running process matches {query!r}",
                    capability=Capability.APPS_CLOSE.value,
                    hint="Process-name matching is prefix based; list_windows() shows running apps.",
                    details={"query": query},
                ) from exc
            raise
        rows = _parse_rows(stdout)
        row = rows[0] if rows else {}
        return ok(
            Capability.APPS_CLOSE,
            query=query,
            terminated_pids=row.get("terminated", []),
            force_killed_pids=row.get("force_killed", []),
            note="CloseMainWindow was tried first; Stop-Process -Force covered the rest.",
        )

    async def open_url(self, url: str) -> dict[str, Any]:
        self._require(Capability.URL_OPEN)
        target = validate_url(url, capability=Capability.URL_OPEN)
        await self._ps(_Script(_SCRIPT_OPEN_URL, target), Capability.URL_OPEN)
        return ok(Capability.URL_OPEN, url=target, launcher="Start-Process")

    async def _media_key(self, verb: str, capability: Capability) -> dict[str, Any]:
        code = _MEDIA_VK[verb]
        await self._ps(_Script(_SCRIPT_MEDIA_KEY, str(code)), capability)
        return ok(capability, action=verb, virtual_key=f"0x{code:02X}", mechanism="SendInput")

    async def media_play_pause(self) -> dict[str, Any]:
        return await self._media_key("play_pause", Capability.MEDIA_PLAY_PAUSE)

    async def media_next(self) -> dict[str, Any]:
        return await self._media_key("next", Capability.MEDIA_NEXT)

    async def media_previous(self) -> dict[str, Any]:
        return await self._media_key("previous", Capability.MEDIA_PREVIOUS)

    async def media_status(self) -> dict[str, Any]:
        self._require(Capability.MEDIA_STATUS)
        raise UnsupportedCapabilityError(
            "reading now-playing state is not implemented on Windows",
            capability=Capability.MEDIA_STATUS.value,
            hint=self.support()[Capability.MEDIA_STATUS].reason,
        )

    async def _enumerate(self) -> list[Window]:
        """Visible top-level windows via ``EnumWindows``."""
        rows = _parse_rows(await self._ps(_Script(_SCRIPT_LIST_WINDOWS), Capability.WINDOWS_LIST))
        windows: list[Window] = []
        for row in rows:
            if not row.get("Id"):
                continue
            windows.append(
                Window(
                    id=str(row["Id"]),
                    app=str(row.get("Process", "") or ""),
                    title=str(row.get("Title", "") or ""),
                    focused=bool(row.get("Focused")),
                )
            )
        return windows

    async def list_windows(self) -> dict[str, Any]:
        self._require(Capability.WINDOWS_LIST)
        windows = await self._enumerate()
        return ok(
            Capability.WINDOWS_LIST, count=len(windows), windows=[w.to_dict() for w in windows]
        )

    async def focus_window(self, app_or_title: str) -> dict[str, Any]:
        self._require(Capability.WINDOWS_FOCUS)
        windows = await self._enumerate()
        target = select_window(app_or_title, windows, target=Capability.WINDOWS_FOCUS.value)
        await self._ps(
            _Script(_SCRIPT_FOCUS_WINDOW, target.id.replace("0x", "")), Capability.WINDOWS_FOCUS
        )
        return ok(
            Capability.WINDOWS_FOCUS,
            window=target.to_dict(),
            already_focused=target.focused,
            mechanism="SetForegroundWindow",
        )
