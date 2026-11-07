"""The add-on registry and the operator-approval gate.

Add-ons are *not* plugins an agent can switch on. The Playwright bridge drives a real
browser with real network access on the host, so installation is split across the same
trust boundary the whole project recognises:

* **Request** (``browser_request_install`` tool, or ``iot-mcp-addons request``): an agent or
  human records *why* an add-on should exist. This writes a note. Nothing else. It cannot
  install, approve, download, or enable anything, and says so in its result.
* **Approve** (``iot-mcp-addons install <name>``): a human on the host — in front of a
  terminal, reading the printed risk block — runs the command. Only this writes an approval
  record, and the record pins the *exact* launch argv by fingerprint: a later
  ``--browser-argv`` that differs is a different program and needs a fresh approval.

State layout (default ``$XDG_CONFIG_HOME/iot-mcp`` or ``~/.config/iot-mcp``)::

    requests/<name>-<timestamp>.json   pending install requests (spool, read-only for tools)
    entries/<name>.json                operator approvals; only `install` writes these

The server reads the entries at startup and never writes them; tools never write them.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AddonEntry",
    "AddonError",
    "AddonManager",
    "BRAVE_BROWSER_ARGS",
    "BRAVE_HEADED_BROWSER_ARGS",
    "DEFAULT_BROWSER_ARGS",
    "PLAYWRIGHT_PACKAGE_SPEC",
    "HEADED_RISK",
    "RISK_STATEMENT",
    "risk_statement",
    "default_manager",
    "default_root",
    "fingerprint_argv",
]

#: The upstream package the default browser add-on runs. Pinned spec = the thing the
#: approval fingerprint covers; ``@latest`` is the upstream's own documented npx usage and
#: the lock lives in the approval step (a human re-reads this string at install time).
PLAYWRIGHT_PACKAGE_SPEC = "@playwright/mcp@latest"

#: Default stdio launch for the ``browser`` add-on: npx, pinned-ish spec, headless so
#: nothing appears on the host user's display without them having asked.
DEFAULT_BROWSER_ARGS: tuple[str, ...] = ("npx", "-y", PLAYWRIGHT_PACKAGE_SPEC, "--headless")

#: Alternative engine launches for the same ``browser`` approval. They are not separate add-ons,
#: because the server loads one approved launch per add-on name: approving one replaces the record
#: for the add-on. Brave is a Chromium-based browser, so it uses the Chromium engine with the
#: official ``@playwright/mcp`` server pointed at the locally installed Brave executable. There is
#: no separate ``playwright-brave`` MCP dependency to install: npm currently has no package by
#: that name, and Playwright's documented route is ``--executable-path``.
#: ``--isolated`` is load-bearing here: without it, a second Brave process delegates to the
#: user's *running* Brave instance (same default profile), so the agent would see and could act
#: on the operator's real, logged-in tabs. An in-memory profile is also the whole point of the
#: approval — verified live: with ``--isolated`` the first page has exactly one tab.
BRAVE_BROWSER_ARGS: tuple[str, ...] = (
    "npx",
    "-y",
    PLAYWRIGHT_PACKAGE_SPEC,
    "--headless",
    "--browser",
    "chromium",
    "--executable-path",
    "/usr/bin/brave",
    "--isolated",
)
BRAVE_HEADED_BROWSER_ARGS: tuple[str, ...] = (
    "npx",
    "-y",
    PLAYWRIGHT_PACKAGE_SPEC,
    "--browser",
    "chromium",
    "--executable-path",
    "/usr/bin/brave",
    "--isolated",
)

#: Add-on name → default launch argv.
DEFAULT_LAUNCH: dict[str, tuple[str, ...]] = {"browser": DEFAULT_BROWSER_ARGS}

#: Exact tool names Microsoft's Playwright MCP is expected to expose as of writing. The
#: bridge does not require this list (it registers whatever ``tools/list`` returns); it
#: exists so the approval text and docs can be concrete about what is being let in.
EXPECTED_BROWSER_TOOLS: tuple[str, ...] = (
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_tabs",
    "browser_take_screenshot",
    "browser_evaluate",
    "browser_fill_form",
    "browser_navigate_back",
    "browser_wait_for",
    "browser_close",
)

#: The risk text shown at install time and returned by the status tool. This is the
#: "understands the risk" part of the manual-approval requirement, in one place.
RISK_STATEMENT = f"""\
You are approving an OPTIONAL add-on that lets a remote model drive a real web browser
on this machine through {PLAYWRIGHT_PACKAGE_SPEC} (downloaded and executed via npx).

Understand exactly what this enables:
  * Arbitrary navigation and arbitrary JavaScript ({PLAYWRIGHT_PACKAGE_SPEC} ships
    browser_evaluate) executed in the browser's origin context, on this machine's network.
  * Form filling, clicking and file download/upload inside that browser session.
  * With --browser-argv overrides that attach to an existing browser (cdp-endpoint) or use
    a real user profile, the model can act inside your LOGGED-IN sessions — mail, banking,
    anything the profile can reach. The default add-on is a fresh headless profile.
  * Whatever the MCP client's model is told to do, the browser will try to do. Page content
    is itself an injection surface: a hostile page can try to steer the agent's next calls.
  * The browser process runs as this user, with this user's filesystem access for
    downloads/uploads, subject to the browser sandbox only.

There is no TLS here. If this server is --expose'd, the add-on will refuse to run unless a
bearer token is configured (--token / --token-file); prefer a token plus an SSH tunnel.
"""

#: Added to the risk text when the approved launch is not headless. The base text describes a
#: headless instance; a headed one is visible to the room, not just to the model.
HEADED_RISK = """\
This launch is HEADED (no ``--headless`` in the argv), so add to the above:
  * A real window opens on this machine's desktop, can take focus, and is visible to anyone
    looking at the screen, to screen shares, and to screenshots.
  * To make that window appear, the server hands the browser process this session's compositor
    handle — the Wayland socket, or ``DISPLAY`` on an X11 session (never both).
"""


def risk_statement(headless: bool = True) -> str:
    """Risk text for one specific launch, headed or not.

    Args:
        headless: Whether the launch argv carries ``--headless``.

    Returns:
        :data:`RISK_STATEMENT`, plus :data:`HEADED_RISK` when the launch opens a window.
    """
    return RISK_STATEMENT if headless else RISK_STATEMENT + HEADED_RISK


_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class AddonError(RuntimeError):
    """An add-on operation was refused, with a reason an operator can act on."""


def fingerprint_argv(argv: list[str] | tuple[str, ...]) -> str:
    """sha256 over the exact argv (length-prefixed so separators can't be forged)."""
    digest = hashlib.sha256()
    for item in argv:
        encoded = str(item).encode("utf-8")
        digest.update(str(len(encoded)).encode("ascii"))
        digest.update(b":")
        digest.update(encoded)
    return "sha256:" + digest.hexdigest()


def default_root() -> Path:
    """State directory: ``$XDG_CONFIG_HOME/iot-mcp`` or ``~/.config/iot-mcp``."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "iot-mcp"


@dataclass(frozen=True)
class AddonEntry:
    """A human-approved add-on.

    Attributes:
        name: Add-on key (``browser``).
        argv: Exact launch command approved (stdio MCP server).
        fingerprint: :func:`fingerprint_argv` over ``argv``; the server refuses to launch
            an argv whose fingerprint differs from the approved record.
        approved_at: ISO-ish unix time of approval.
        approved_by: Host user that ran the approval (evidence, not identity).
        package: Upstream package spec that was installed, when known.
        headless: Whether the approved argv contains ``--headless`` (informational).
        notes: Operator's own reminder of why this exists.
    """

    name: str
    argv: tuple[str, ...]
    fingerprint: str
    approved_at: str
    approved_by: str
    package: str = ""
    headless: bool = False
    notes: str = ""

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> AddonEntry:
        """Rebuild from the JSON record, tolerating unknown future keys."""
        return cls(
            name=str(record["name"]),
            argv=tuple(str(item) for item in record.get("argv", [])),
            fingerprint=str(record.get("fingerprint", "")),
            approved_at=str(record.get("approved_at", "")),
            approved_by=str(record.get("approved_by", "")),
            package=str(record.get("package", "")),
            headless=bool(record.get("headless", False)),
            notes=str(record.get("notes", "")),
        )

    def to_record(self) -> dict[str, Any]:
        """JSON-serializable form written to ``entries/<name>.json``."""
        record = asdict(self)
        record["argv"] = list(self.argv)
        return record

    def matches(self, argv: list[str] | tuple[str, ...]) -> bool:
        """Whether ``argv`` is exactly what the operator approved."""
        return fingerprint_argv(argv) == self.fingerprint


class AddonManager:
    """Reads approval records; the *only* writer is :meth:`approve` (the CLI install)."""

    def __init__(
        self, root: str | Path | None = None, *, environ: dict[str, str] | None = None
    ) -> None:
        """Point the manager at a state root.

        Args:
            root: State directory. ``None`` honours ``IOT_MCP_ADDON_ROOT`` then the XDG
                default — the environment hook is how tests and staging setups relocate it.
            environ: Environment for the override; defaults to ``os.environ``.
        """
        env = os.environ if environ is None else environ
        if root is not None:
            base: Path | None = Path(root)
        elif env.get("IOT_MCP_ADDON_ROOT"):
            base = Path(env["IOT_MCP_ADDON_ROOT"])
        else:
            base = None
        self._root = base if base is not None else default_root()

    @property
    def root(self) -> Path:
        """State directory (may not exist yet; nothing here creates it eagerly)."""
        return self._root

    @property
    def entries_dir(self) -> Path:
        return self._root / "entries"

    @property
    def requests_dir(self) -> Path:
        return self._root / "requests"

    # ------------------------------------------------------------------ approvals (read)

    def approved(self, name: str) -> AddonEntry | None:
        """The approval record for ``name``, or ``None``.

        A corrupt or unparseable record reads as *not approved*: the server must fail
        closed on the trust gate, and the problem is surfaced by :meth:`explain_invalid`
        rather than silently launching something unapproved.
        """
        file = self.entries_dir / f"{_checked_name(name)}.json"
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
            entry = AddonEntry.from_record(record)
        except (OSError, ValueError, KeyError):
            return None
        if entry.fingerprint != fingerprint_argv(entry.argv) or not entry.argv:
            return None
        return entry

    def explain_invalid(self, name: str) -> str | None:
        """Why an on-disk record was rejected, or ``None`` when it is valid/absent."""
        file = self.entries_dir / f"{_checked_name(name)}.json"
        if not file.exists():
            return None
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
            entry = AddonEntry.from_record(record)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return f"{file} is unreadable ({type(exc).__name__}: {exc})"
        if not entry.argv:
            return f"{file} has an empty argv"
        if entry.fingerprint != fingerprint_argv(entry.argv):
            return f"{file}'s fingerprint does not match its argv (record tampered or edited)"
        return None

    def list_approved(self) -> list[AddonEntry]:
        """Every valid approval record, sorted by name."""
        try:
            files = sorted(self.entries_dir.glob("*.json"))
        except OSError:  # pragma: no cover - unreadable config dir
            return []
        entries = [self.approved(file.stem) for file in files]
        return [entry for entry in entries if entry is not None]

    # ------------------------------------------------------------------ approvals (write)

    def approve(
        self,
        name: str,
        *,
        argv: list[str] | None = None,
        package: str = "",
        notes: str = "",
        approver: str | None = None,
    ) -> AddonEntry:
        """Record a human approval. **Callers must have obtained explicit consent first**
        — this method is reached only from ``iot-mcp-addons install`` after the operator
        typed the confirmation word; nothing in the MCP tool surface calls it.

        Args:
            name: Add-on key.
            argv: Exact launch argv; defaults to :data:`DEFAULT_LAUNCH` for the name.
            package: What was installed (recorded, not executed, by this method).
            notes: Operator's reminder text.
            approver: Override for the recorded approver (tests); defaults to the OS user
                and host.
        """
        key = _checked_name(name)
        launch = tuple(argv) if argv is not None else DEFAULT_LAUNCH.get(key)
        if launch is None:
            raise AddonError(
                f"no default launch for add-on {key!r}; pass the exact argv you approved"
            )
        entry = AddonEntry(
            name=key,
            argv=tuple(str(item) for item in launch),
            fingerprint=fingerprint_argv(launch),
            approved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            approved_by=approver or _whoami(),
            package=package,
            headless="--headless" in launch,
            notes=notes,
        )
        self._validate_executable(entry)
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        file = self.entries_dir / f"{key}.json"
        _atomic_write(file, json.dumps(entry.to_record(), indent=2) + "\n")
        return entry

    def revoke(self, name: str) -> bool:
        """Delete an approval. Returns whether one existed."""
        file = self.entries_dir / f"{_checked_name(name)}.json"
        try:
            file.unlink()
            return True
        except FileNotFoundError:
            return False

    def _validate_executable(self, entry: AddonEntry) -> None:
        """Fail early (at approval, with a terminal in front of you) if argv[0] can't run."""
        program = str(entry.argv[0])
        if shutil.which(program) is None:
            raise AddonError(
                f"{program!r} is not on PATH, so approving it would give you an add-on that "
                "never starts. Install the runtime (node/npm provide npx) and approve again."
            )

    # ---------------------------------------------------------------- requests (spool)

    def request_install(
        self,
        name: str,
        *,
        reason: str,
        requested_by: str = "agent",
        requested_argv: list[str] | None = None,
    ) -> dict[str, Any]:
        """Queue an install *request* — a note for the operator, not an installation.

        This is the method behind the ``browser_request_install`` tool. It creates one file
        under ``requests/`` and returns the exact command a human must run. It never
        touches ``entries/``, never runs anything, never enables anything.
        """
        key = _checked_name(name)
        if key not in DEFAULT_LAUNCH and requested_argv is None:
            raise AddonError(
                f"unknown add-on {key!r}; known add-ons: {', '.join(sorted(DEFAULT_LAUNCH))}"
            )
        clean_reason = " ".join(str(reason or "").split())[:2000]
        if not clean_reason:
            raise AddonError("an install request must carry a reason the operator can read")
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        payload = {
            "name": key,
            "reason": clean_reason,
            "requested_by": str(requested_by)[:120],
            "requested_argv": [str(item) for item in requested_argv] if requested_argv else None,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "install_command": f"iot-mcp-addons install {key}",
        }
        file = self.requests_dir / f"{key}-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        _atomic_write(file, json.dumps(payload, indent=2) + "\n")
        return {"request_file": str(file), **payload}

    def pending_requests(self, name: str | None = None) -> list[dict[str, Any]]:
        """Queued requests (newest last), optionally filtered by add-on name."""
        try:
            files = sorted(self.requests_dir.glob("*.json"))
        except OSError:  # pragma: no cover
            return []
        out: list[dict[str, Any]] = []
        for file in files:
            try:
                record = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if name is not None and record.get("name") != name:
                continue
            out.append({"request_file": str(file), **record})
        return out

    def clear_requests(self, name: str | None = None) -> int:
        """Drop queued requests after the operator has acted on them. Returns count."""
        count = 0
        for item in self.pending_requests(name):
            try:
                Path(item["request_file"]).unlink()
                count += 1
            except OSError:  # pragma: no cover - racy deletion is fine
                pass
        return count

    def startup_notes(self) -> list[str]:
        """Operator-facing lines the server prints at boot (pending requests, bad records)."""
        notes: list[str] = []
        for file in self.entries_dir.glob("*.json") if self.entries_dir.is_dir() else []:
            problem = self.explain_invalid(file.stem)
            if problem:
                notes.append(
                    f"add-on record invalid: {problem}; re-run `iot-mcp-addons install {file.stem}`"
                )
        pending = self.pending_requests()
        if pending:
            names = ", ".join(sorted({str(item["name"]) for item in pending}))
            notes.append(
                f"{len(pending)} add-on install request(s) awaiting a human ({names}). "
                f"Review: iot-mcp-addons pending · Approve: iot-mcp-addons install <name>"
            )
        return notes


def default_manager() -> AddonManager:
    """Manager at the environment-selected root (used by server and CLI alike)."""
    return AddonManager()


def _checked_name(name: str) -> str:
    key = str(name or "").strip().lower()
    if not _NAME_RE.match(key):
        raise AddonError(
            f"add-on name {name!r} is invalid; use a slug like 'browser' "
            "(lowercase letters, digits, hyphens, ≤32 chars)"
        )
    return key


def _whoami() -> str:
    try:
        return f"{getpass.getuser()}@{os.uname().nodename}"
    except Exception:  # pragma: no cover - exotic platforms / no passwd entry
        return "unknown"


def _atomic_write(file: Path, text: str) -> None:
    """Write via a temp file + rename: a crash must not leave a half-written approval."""
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(file.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, file)


def manager_stream_default() -> Any:
    """stderr, indirected for testability of install prompts."""
    return sys.stderr
