"""Path-traversal, symlink and hostile-filesystem cases for .desktop discovery.

Threat model (shared with test_injection_boundaries): an attacker who can drop files
into an XDG data directory — a rogue app install, a leftover in a shared directory —
is in range. Discovery must then stay *inside* the tree, read only regular files,
stay bounded, and never interpret the query as a path.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
from pathlib import Path
from typing import Any

import pytest

from iot_mcp.drivers._xdg import discover_apps, match_app
from iot_mcp.errors import InvalidInputError, NotFoundError


def apps_dir_of(root: Path) -> Path:
    return root / "applications"


# ---------------------------------------------------------------- symlink escapes


def test_a_symlinked_desktop_file_leaving_the_tree_is_ignored(
    tmp_path: Path, desktop_entries: Any
) -> None:
    """``_walk_limited`` skips every symlink: /etc/passwd cannot be recruited as an
    app by linking it (or a file like it) into the applications dir."""
    root = desktop_entries({"good": {"Name": "Good", "Exec": "good"}})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Evil\nExec=/bin/evil\n"
    )
    (apps_dir_of(root) / "sneaky.desktop").symlink_to(outside / "evil.desktop")

    found = discover_apps([apps_dir_of(root)])

    assert [app.id for app in found] == ["good"], "the symlink never enters the index"


def test_a_symlinked_directory_leaving_the_tree_is_ignored(
    tmp_path: Path, desktop_entries: Any
) -> None:
    root = desktop_entries({"good": {"Name": "Good", "Exec": "good"}})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Hidden\nExec=/bin/hidden\n"
    )
    (apps_dir_of(root) / "escape").symlink_to(outside, target_is_directory=True)

    found = discover_apps([apps_dir_of(root)])

    assert "hidden" not in {app.id for app in found}


def test_a_self_referencing_symlink_does_not_loop_or_crash(
    tmp_path: Path, desktop_entries: Any
) -> None:
    root = desktop_entries({"good": {"Name": "Good", "Exec": "good"}})
    (apps_dir_of(root) / "loop").symlink_to(apps_dir_of(root), target_is_directory=True)
    found = discover_apps([apps_dir_of(root)])
    assert [app.id for app in found] == ["good"]


# ------------------------------------------------------------ path containment


def test_every_discovered_path_stays_under_the_scanned_directory(
    tmp_path: Path, desktop_entries: Any
) -> None:
    """Reported app paths are containment-asserted by the test suite itself: nothing
    in the walk can surface an absolute path pointing elsewhere."""
    entries = {f"app{i}": {"Name": f"App {i}", "Exec": f"app{i}"} for i in range(5)}
    root = desktop_entries(entries)
    applications = apps_dir_of(root)
    (applications / "extras").mkdir()
    (applications / "extras" / "nested.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Nested\nExec=nested\n"
    )

    for app in discover_apps([applications]):
        assert Path(app.path).resolve().is_relative_to(applications.resolve())


def test_only_the_desktop_suffix_is_read(tmp_path: Path, desktop_entries: Any) -> None:
    root = desktop_entries({"good": {"Name": "Good", "Exec": "good"}})
    applications = apps_dir_of(root)
    (applications / "notes.txt").write_text("Name=NotAnApp\n")
    (applications / "payload.desktop.exe").write_text("Exec=/bin/evil\n")
    found = discover_apps([applications])
    assert [app.id for app in found] == ["good"]


def test_a_directory_named_like_an_entry_is_skipped_not_read(
    tmp_path: Path, desktop_entries: Any
) -> None:
    """``weird.desktop/`` as a directory must not crash the scan (it is not a file)."""
    root = desktop_entries({"good": {"Name": "Good", "Exec": "good"}})
    (apps_dir_of(root) / "weird.desktop").mkdir()
    found = discover_apps([apps_dir_of(root)])
    assert [app.id for app in found] == ["good"]


def test_discovery_is_capped_at_the_documented_limit(tmp_path: Path, desktop_entries: Any) -> None:
    """The cap bounds one tool response; thousands of planted files cannot balloon it."""
    entries = {f"app{i:04d}": {"Name": f"App {i:04d}", "Exec": "x"} for i in range(510)}
    root = desktop_entries(entries)
    found = discover_apps([apps_dir_of(root)])
    assert len(found) == 500


# ------------------------------------------------------------------ unreadable


def test_a_file_the_server_cannot_read_is_a_non_event(tmp_path: Path, desktop_entries: Any) -> None:
    root = desktop_entries({"good": {"Name": "Good", "Exec": "good"}})
    bad = apps_dir_of(root) / "unreadable.desktop"
    bad.write_text("[Desktop Entry]\nType=Application\nName=Bad\nExec=bad\n")
    bad.chmod(0)
    try:
        found = discover_apps([apps_dir_of(root)])
    finally:
        bad.chmod(0o644)
    assert [app.id for app in found] == ["good"]


# ------------------------------------------------------- FIFO freeze (finding)

FIFO_TIMEOUT = 1.0


async def _attempt_discovery_over_fifo(tmp_path: Path, desktop_entries: Any) -> Any:
    """Run discovery over a tree containing a ``*.desktop`` FIFO on a *private*
    executor, and guarantee the blocked reader is released before returning."""
    root = desktop_entries({"good": {"Name": "Good", "Exec": "good"}})
    applications = apps_dir_of(root)
    fifo = applications / "trap.desktop"
    os.mkfifo(fifo)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(discover_apps, [applications])
        return await asyncio.wait_for(asyncio.wrap_future(future), FIFO_TIMEOUT)
    finally:
        # O_RDWR on a FIFO never blocks, so this cleanup open is sync by design — it exists
        # precisely to release the reader blocked in open(), and must run even if the test failed.
        with open(fifo, "r+b", buffering=0):  # noqa: ASYNC230 - non-blocking by construction
            pass
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "finding: discover_apps open()s every *.desktop unconditionally, so a FIFO "
        "named like an entry blocks forever — and LinuxDriver calls it synchronously "
        "on cache refresh, freezing the event loop of the whole server"
    ),
)
async def test_discovery_must_not_block_on_a_fifo_named_like_an_entry(
    tmp_path: Path, desktop_entries: Any
) -> None:
    apps = await _attempt_discovery_over_fifo(tmp_path, desktop_entries)
    assert [app.id for app in apps] == ["good"], "a FIFO must be skipped, not waited on"


async def test_currently_a_fifo_poisons_discovery_until_released(
    tmp_path: Path, desktop_entries: Any
) -> None:
    """Pin the fact behind the xfail above: the scan is still in flight when the
    deadline passes — only releasing a writer unblocks it."""
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await _attempt_discovery_over_fifo(tmp_path, desktop_entries)


# ------------------------------------------------------------ query-as-path


def test_a_traversal_query_matches_nothing(tmp_path: Path, desktop_entries: Any) -> None:
    """The query is a name, never a path: traversal shapes just miss."""
    root = desktop_entries({"firefox": {"Name": "Firefox", "Exec": "firefox"}})
    apps = discover_apps([apps_dir_of(root)])
    for hostile in (
        "../../../../etc/passwd",
        "/usr/share/applications/../../evil",
        "..%2f..%2fetc/passwd",
    ):
        with pytest.raises(NotFoundError):
            match_app(hostile, apps)


def test_an_empty_query_is_refused_before_matching(tmp_path: Path, desktop_entries: Any) -> None:
    root = desktop_entries({"firefox": {"Name": "Firefox", "Exec": "firefox"}})
    apps = discover_apps([apps_dir_of(root)])
    with pytest.raises(InvalidInputError):
        match_app("   ", apps)


def test_a_path_shaped_query_cannot_select_anything_outside_the_index(
    tmp_path: Path, desktop_entries: Any
) -> None:
    """A query that looks like a full .desktop path can only ever fuzzy-match an
    entry that is already inside the tree; its OWN Exec launches, never the path."""
    root = desktop_entries({"firefox": {"Name": "Firefox", "Exec": "firefox"}})
    apps = discover_apps([apps_dir_of(root)])
    fake_path = str(tmp_path / "attacker" / "firefox.desktop")
    winner = match_app(fake_path, apps)  # fuzzy: "firefox" name appears in the sentence
    assert winner.id == "firefox"
    assert Path(winner.path).resolve().is_relative_to(apps_dir_of(root).resolve())
