"""Add-on approval store: requests can be filed by agents, entries only by operator code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from iot_mcp.addons import (
    BRAVE_BROWSER_ARGS,
    BRAVE_HEADED_BROWSER_ARGS,
    DEFAULT_BROWSER_ARGS,
    HEADED_RISK,
    PLAYWRIGHT_PACKAGE_SPEC,
    RISK_STATEMENT,
    AddonEntry,
    AddonError,
    AddonManager,
    fingerprint_argv,
    risk_statement,
)


@pytest.fixture
def manager(tmp_path: Path) -> AddonManager:
    return AddonManager(tmp_path)


@pytest.fixture
def executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("iot_mcp.addons.shutil.which", lambda program: f"/usr/bin/{program}")


def _write_entry(manager: AddonManager, name: str, record: dict[str, Any]) -> Path:
    manager.entries_dir.mkdir(parents=True, exist_ok=True)
    file = manager.entries_dir / f"{name}.json"
    file.write_text(json.dumps(record), encoding="utf-8")
    return file


def test_fingerprint_is_over_exact_argv_not_just_a_join() -> None:
    assert fingerprint_argv(("a", "bc")) != fingerprint_argv(("ab", "c"))
    assert fingerprint_argv(["npx", "-y", "pkg"]) == fingerprint_argv(("npx", "-y", "pkg"))


def test_brave_launches_still_use_official_playwright_mcp() -> None:
    assert PLAYWRIGHT_PACKAGE_SPEC in BRAVE_BROWSER_ARGS
    assert "--browser" in BRAVE_BROWSER_ARGS
    assert "--executable-path" in BRAVE_BROWSER_ARGS
    assert "/usr/bin/brave" in BRAVE_BROWSER_ARGS
    assert "--headless" in BRAVE_BROWSER_ARGS


def test_brave_launches_are_isolated_from_the_users_real_profile() -> None:
    # Without --isolated, a second Brave process delegates to the running instance and the
    # agent could drive the operator's logged-in tabs. Verified live against @playwright/mcp.
    assert "--isolated" in BRAVE_BROWSER_ARGS
    assert "--isolated" in BRAVE_HEADED_BROWSER_ARGS


def test_brave_headed_launch_differs_from_headless_only_by_window_mode() -> None:
    assert "--headless" not in BRAVE_HEADED_BROWSER_ARGS
    assert [arg for arg in BRAVE_HEADED_BROWSER_ARGS if arg != "--headless"] == [
        arg for arg in BRAVE_BROWSER_ARGS if arg != "--headless"
    ]


def test_manager_uses_explicit_root_over_environment(tmp_path: Path) -> None:
    manager = AddonManager(tmp_path / "explicit", environ={"IOT_MCP_ADDON_ROOT": "/ignored"})
    assert manager.root == tmp_path / "explicit"
    assert manager.entries_dir == tmp_path / "explicit" / "entries"


def test_manager_uses_environment_root_when_no_explicit_root(tmp_path: Path) -> None:
    manager = AddonManager(environ={"IOT_MCP_ADDON_ROOT": str(tmp_path)})
    assert manager.root == tmp_path


def test_request_install_writes_a_spool_file_but_no_approval(manager: AddonManager) -> None:
    record = manager.request_install("browser", reason="need docs", requested_by="client")

    assert record["name"] == "browser"
    assert record["reason"] == "need docs"
    assert record["install_command"] == "iot-mcp-addons install browser"
    assert manager.approved("browser") is None
    assert len(manager.pending_requests("browser")) == 1


def test_request_install_clears_only_when_asked(manager: AddonManager) -> None:
    manager.request_install("browser", reason="need docs")
    manager.request_install("browser", reason="again")

    assert manager.clear_requests("browser") == 2
    assert manager.pending_requests("browser") == []


def test_pending_requests_can_be_filtered_by_name(manager: AddonManager) -> None:
    manager.request_install("browser", reason="need docs")
    manager.requests_dir.joinpath("other.json").write_text(
        json.dumps({"name": "other", "reason": "noise"}),
        encoding="utf-8",
    )

    assert [item["name"] for item in manager.pending_requests("browser")] == ["browser"]


def test_request_install_requires_a_readable_reason(manager: AddonManager) -> None:
    with pytest.raises(AddonError, match="must carry a reason"):
        manager.request_install("browser", reason="   ")


def test_request_install_rejects_unknown_addons_without_argv(manager: AddonManager) -> None:
    with pytest.raises(AddonError, match="unknown add-on"):
        manager.request_install("nope", reason="please")


def test_request_install_accepts_unknown_addon_with_requested_argv(
    manager: AddonManager,
) -> None:
    record = manager.request_install("nope", reason="custom bridge", requested_argv=["/bin/true"])
    assert record["requested_argv"] == ["/bin/true"]


def test_request_install_sanitizes_and_truncates_reason(manager: AddonManager) -> None:
    record = manager.request_install("browser", reason="  lots\nof\twhitespace  ")
    assert record["reason"] == "lots of whitespace"


def test_approve_writes_default_browser_entry_and_records_approver(
    manager: AddonManager, executable: None
) -> None:
    entry = manager.approve("browser", package="@playwright/mcp@latest", approver="op@host")

    assert entry.argv == DEFAULT_BROWSER_ARGS
    assert entry.fingerprint == fingerprint_argv(DEFAULT_BROWSER_ARGS)
    assert entry.approved_by == "op@host"
    assert entry.headless is True
    assert manager.approved("browser") == entry


def test_approve_custom_argv_is_pinned_exactly(manager: AddonManager, executable: None) -> None:
    entry = manager.approve(
        "custom",
        argv=["my-mcp", "--stdio"],
        notes="operator test",
        approver="op@host",
    )

    assert entry.argv == ("my-mcp", "--stdio")
    assert entry.matches(["my-mcp", "--stdio"]) is True
    assert entry.matches(["my-mcp", "--stdio", "--extra"]) is False


def test_approve_refuses_executable_missing_from_PATH(
    manager: AddonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("iot_mcp.addons.shutil.which", lambda program: None)
    with pytest.raises(AddonError, match="not on PATH"):
        manager.approve("browser")


def test_corrupt_entry_reads_as_not_approved_and_explains(manager: AddonManager) -> None:
    _write_entry(manager, "browser", {"not": "an entry"})

    assert manager.approved("browser") is None
    assert "unreadable" in (manager.explain_invalid("browser") or "")


def test_tampered_fingerprint_fails_closed(manager: AddonManager, executable: None) -> None:
    entry = manager.approve("browser", approver="op@host")
    record = entry.to_record()
    record["argv"] = ["npx", "-y", "@playwright/mcp@latest"]
    _write_entry(manager, "browser", record)

    assert manager.approved("browser") is None
    assert "fingerprint does not match" in (manager.explain_invalid("browser") or "")


def test_empty_argv_fails_closed(manager: AddonManager) -> None:
    _write_entry(
        manager,
        "browser",
        {
            "name": "browser",
            "argv": [],
            "fingerprint": fingerprint_argv([]),
            "approved_at": "now",
            "approved_by": "op@host",
        },
    )

    assert manager.approved("browser") is None
    assert "empty argv" in (manager.explain_invalid("browser") or "")


def test_list_approved_skips_invalid_records_and_sorts(
    manager: AddonManager, executable: None
) -> None:
    manager.approve("zeta", argv=["zeta"], approver="op@host")
    manager.approve("alpha", argv=["alpha"], approver="op@host")
    _write_entry(manager, "broken", {"not": "valid"})

    assert [entry.name for entry in manager.list_approved()] == ["alpha", "zeta"]


def test_revoke_removes_entry_and_reports_whether_it_existed(
    manager: AddonManager, executable: None
) -> None:
    manager.approve("browser", approver="op@host")

    assert manager.revoke("browser") is True
    assert manager.revoke("browser") is False


def test_invalid_name_is_rejected_before_touching_disk(manager: AddonManager) -> None:
    with pytest.raises(AddonError, match="invalid"):
        manager.request_install("../browser", reason="escape")


def test_startup_notes_report_invalid_records_and_pending_requests(
    manager: AddonManager, executable: None
) -> None:
    entry = manager.approve("browser", approver="op@host")
    record = entry.to_record()
    record["fingerprint"] = "sha256:bad"
    _write_entry(manager, "browser", record)
    manager.request_install("browser", reason="need docs")

    notes = "\n".join(manager.startup_notes())
    assert "add-on record invalid" in notes
    assert "awaiting a human" in notes


def test_entry_record_roundtrip_preserves_argv_tuple() -> None:
    entry = AddonEntry(
        name="browser",
        argv=("npx", "pkg"),
        fingerprint=fingerprint_argv(("npx", "pkg")),
        approved_at="now",
        approved_by="op@host",
        package="pkg",
        headless=True,
        notes="note",
    )

    assert AddonEntry.from_record(entry.to_record()) == entry


def test_default_root_honours_xdg_config_home(monkeypatch: pytest.MonkeyPatch) -> None:
    from iot_mcp.addons import default_root

    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-test")
    assert default_root() == Path("/tmp/xdg-test/iot-mcp")


def test_default_root_falls_back_to_home_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from iot_mcp.addons import default_root

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/tmp/home-test"))
    assert default_root() == Path("/tmp/home-test/.config/iot-mcp")


def test_risk_statement_is_mode_specific() -> None:
    """A headed approval must be told about the window; a headless one must not be."""
    assert risk_statement(headless=True) == RISK_STATEMENT
    assert HEADED_RISK not in risk_statement(headless=True)
    assert risk_statement(headless=False) == RISK_STATEMENT + HEADED_RISK
