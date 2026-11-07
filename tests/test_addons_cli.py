"""Operator CLI: request is safe, install is the consent boundary, revoke is reversible."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from iot_mcp import addons_cli
from iot_mcp.addons import AddonManager


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "addons"


@pytest.fixture
def executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(addons_cli.shutil, "which", lambda program: f"/usr/bin/{program}")
    monkeypatch.setattr("iot_mcp.addons.shutil.which", lambda program: f"/usr/bin/{program}")


def _main(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = addons_cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_path_reports_state_directories(capsys: pytest.CaptureFixture[str], root: Path) -> None:
    code, out, _ = _main(capsys, "--root", str(root), "path")

    assert code == 0
    assert str(root) in out
    assert str(root / "entries") in out
    assert str(root / "requests") in out


def test_request_lists_pending_without_approving(
    capsys: pytest.CaptureFixture[str], root: Path
) -> None:
    code, out, _ = _main(
        capsys,
        "--root",
        str(root),
        "request",
        "browser",
        "--reason",
        "agent needs docs",
        "--requested-by",
        "claude",
    )

    assert code == 0
    assert "nothing was installed or approved" in out
    assert _pending_names(root) == ["browser"]
    assert AddonManager(root).approved("browser") is None


def test_pending_can_filter_by_name(capsys: pytest.CaptureFixture[str], root: Path) -> None:
    AddonManager(root).request_install("browser", reason="docs")

    code, out, _ = _main(capsys, "--root", str(root), "pending", "browser")
    assert code == 0
    assert "agent needs docs" in out or "docs" in out

    code, out, _ = _main(capsys, "--root", str(root), "pending", "other")
    assert code == 0
    assert "no install requests pending" in out


def test_install_requires_typed_confirmation_and_aborts_on_wrong_text(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes please")

    code, out, _ = _main(capsys, "--root", str(root), "install", "browser")

    assert code == 1
    assert "cancelled: nothing was approved" in out
    assert AddonManager(root).approved("browser") is None


def test_install_cancels_on_eof(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)

    code, out, _ = _main(capsys, "--root", str(root), "install", "browser")

    assert code == 1
    assert "no input" in out
    assert AddonManager(root).approved("browser") is None


def test_install_typed_confirmation_approves_and_clears_requests(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    executable: None,
) -> None:
    AddonManager(root).request_install("browser", reason="docs")
    monkeypatch.setattr("builtins.input", lambda prompt="": "approve browser")

    code, out, _ = _main(capsys, "--root", str(root), "install", "browser", "--notes", "why")

    assert code == 0
    assert "approved browser" in out
    assert AddonManager(root).approved("browser") is not None
    assert AddonManager(root).pending_requests("browser") == []
    assert "restart" in out.lower()


def test_install_yes_skips_prompt_but_warns(
    capsys: pytest.CaptureFixture[str], root: Path, executable: None
) -> None:
    code, out, _ = _main(capsys, "--root", str(root), "install", "browser", "--yes")

    assert code == 0
    assert "--yes" in out
    assert AddonManager(root).approved("browser") is not None


def test_install_custom_argv_is_approved_by_exact_fingerprint(
    capsys: pytest.CaptureFixture[str], root: Path, executable: None
) -> None:
    code, out, _ = _main(
        capsys,
        "--root",
        str(root),
        "install",
        "custom",
        "--argv",
        "my-mcp --stdio",
        "--yes",
    )

    assert code == 0
    entry = AddonManager(root).approved("custom")
    assert entry is not None
    assert entry.argv == ("my-mcp", "--stdio")


def test_install_rejects_argv_program_not_on_path(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    monkeypatch.setattr(addons_cli.shutil, "which", lambda program: None)

    code, _, err = _main(
        capsys,
        "--root",
        str(root),
        "install",
        "custom",
        "--argv",
        "not-real --stdio",
        "--yes",
    )

    assert code == 2
    assert "not on PATH" in err


def test_install_unknown_without_default_needs_argv(
    capsys: pytest.CaptureFixture[str], root: Path
) -> None:
    code, out, _ = _main(capsys, "--root", str(root), "install", "unknown", "--yes")

    assert code == 2
    assert "no default launch command" in out


def test_revoke_removes_existing_and_is_harmless_when_absent(
    capsys: pytest.CaptureFixture[str], root: Path, executable: None
) -> None:
    AddonManager(root).approve("browser", approver="op@host")

    code, out, _ = _main(capsys, "--root", str(root), "revoke", "browser")
    assert code == 0
    assert "revoked browser" in out
    assert AddonManager(root).approved("browser") is None

    code, out, _ = _main(capsys, "--root", str(root), "revoke", "browser")
    assert code == 0
    assert "no approval record" in out


def test_list_shows_approved_entries_and_invalid_records(
    capsys: pytest.CaptureFixture[str], root: Path, executable: None
) -> None:
    manager = AddonManager(root)
    manager.approve("browser", approver="op@host")
    manager.entries_dir.mkdir(parents=True, exist_ok=True)
    (manager.entries_dir / "broken.json").write_text("{not json}", encoding="utf-8")

    code, out, _ = _main(capsys, "--root", str(root), "list")

    assert code == 0
    assert "browser" in out
    assert "INVALID" in out


def test_list_reports_nothing_approved(capsys: pytest.CaptureFixture[str], root: Path) -> None:
    code, out, _ = _main(capsys, "--root", str(root), "list")

    assert code == 0
    assert "no add-ons approved" in out


def test_invalid_addon_name_is_usage_error(capsys: pytest.CaptureFixture[str], root: Path) -> None:
    code, _, err = _main(capsys, "--root", str(root), "install", "Bad Name", "--yes")

    assert code == 2
    assert "invalid" in err.lower()


def _pending_names(root: Path) -> list[str]:
    files = sorted((root / "requests").glob("*.json"))
    return [str(json.loads(path.read_text(encoding="utf-8"))["name"]) for path in files]
