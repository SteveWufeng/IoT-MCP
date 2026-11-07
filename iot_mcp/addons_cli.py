"""``iot-mcp-addons`` — the human side of add-on approval.

This CLI is the **only** supported way an add-on ever becomes approved. The MCP tool surface can
file a request and report state; it cannot reach :meth:`iot_mcp.addons.AddonManager.approve`,
and this module is deliberately not importable by the server process's tool layer. That
separation is the whole control: a prompt-injected model can ask, and a human decides.

Because the decision is the security boundary, ``install`` does not take a bare flag for
consent — it prints the full risk statement and requires the operator to type
``approve <name>``. ``--yes`` exists for scripted setups and says so loudly, because a consent
prompt that a wrapper can skip without a trace is not a consent prompt.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from typing import Any

from .addons import (
    DEFAULT_LAUNCH,
    PLAYWRIGHT_PACKAGE_SPEC,
    RISK_STATEMENT,
    AddonError,
    AddonManager,
)

__all__ = ["build_parser", "main"]

_CONFIRMATION_PREFIX = "approve"


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the operator CLI."""
    parser = argparse.ArgumentParser(
        prog="iot-mcp-addons",
        description=(
            "Approve or revoke optional IoT-MCP add-ons. Humans only: an MCP client can file a "
            "request, but only this command can approve one."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--root",
        default=None,
        metavar="DIR",
        help="Add-on state directory (default: $IOT_MCP_ADDON_ROOT or ~/.config/iot-mcp). "
        "Must match the directory the server was started with, or the server will not see this "
        "approval.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    path = sub.add_parser("path", help="Show where approvals and requests live.")
    path.set_defaults(func=_cmd_path)

    listing = sub.add_parser("list", help="List approved add-ons.")
    listing.set_defaults(func=_cmd_list)

    pending = sub.add_parser(
        "pending", help="List install requests a human still owes an answer to."
    )
    pending.add_argument("name", nargs="?", default=None, help="Filter to one add-on.")
    pending.set_defaults(func=_cmd_pending)

    request = sub.add_parser(
        "request", help="File an install request (what the agent's tool does; here, on purpose)."
    )
    request.add_argument("name", help=f"Add-on name. Known: {', '.join(sorted(DEFAULT_LAUNCH))}.")
    request.add_argument("--reason", required=True, help="Why. The operator reads this verbatim.")
    request.add_argument(
        "--requested-by", default="operator", help="Who is asking, for the record."
    )
    request.set_defaults(func=_cmd_request)

    install = sub.add_parser(
        "install",
        help="Approve an add-on. Prints the risks and requires a typed confirmation.",
    )
    install.add_argument("name", help=f"Add-on name. Known: {', '.join(sorted(DEFAULT_LAUNCH))}.")
    install.add_argument(
        "--argv",
        default=None,
        metavar="'COMMAND...'",
        help="Approve this exact launch command instead of the built-in default. The server "
        "may then only ever run this command — an override that does not match is refused.",
    )
    install.add_argument(
        "--notes",
        default="",
        help="Reminder to yourself about why this exists. Stored in the record.",
    )
    install.add_argument(
        "--yes",
        action="store_true",
        help="Skip the typed confirmation. For scripted setups only; it is recorded nowhere, "
        "so a skipped consent is indistinguishable from an informed one.",
    )
    install.set_defaults(func=_cmd_install)

    revoke = sub.add_parser(
        "revoke", help="Withdraw an approval. Takes effect at next server start."
    )
    revoke.add_argument("name", help="Add-on name.")
    revoke.set_defaults(func=_cmd_revoke)

    return parser


def _manager(args: argparse.Namespace) -> AddonManager:
    """Manager honouring ``--root`` over the environment default."""
    return AddonManager(args.root) if getattr(args, "root", None) else AddonManager()


def _cmd_path(args: argparse.Namespace, manager: AddonManager, out: Any) -> int:
    print(f"state root:  {manager.root}", file=out)
    print(f"approvals:   {manager.entries_dir}", file=out)
    print(f"requests:    {manager.requests_dir}", file=out)
    if manager.entries_dir.is_dir():
        approved = manager.list_approved()
        print(f"approved:    {', '.join(entry.name for entry in approved) or '(none)'}", file=out)
    else:
        print("approved:    (none — directory does not exist yet)", file=out)
    return 0


def _cmd_list(args: argparse.Namespace, manager: AddonManager, out: Any) -> int:
    entries = manager.list_approved()
    if not entries:
        print(f"no add-ons approved in {manager.root}", file=out)
        return 0
    for entry in entries:
        print(f"{entry.name}", file=out)
        print(f"  argv:        {' '.join(entry.argv)}", file=out)
        print(f"  fingerprint: {entry.fingerprint}", file=out)
        print(f"  approved:    {entry.approved_at} by {entry.approved_by}", file=out)
        print(
            f"  headless:    {'yes' if entry.headless else 'NO — a window will open on this host'}",
            file=out,
        )
        if entry.package:
            print(f"  package:     {entry.package}", file=out)
        if entry.notes:
            print(f"  notes:       {entry.notes}", file=out)
        problem = manager.explain_invalid(entry.name)
        if problem:  # pragma: no cover - list_approved filters these out
            print(f"  problem:     {problem}", file=out)
    invalid = _invalid_records(manager)
    for name, reason in invalid:
        print(f"{name}: INVALID — {reason}", file=out)
    return 0


def _cmd_pending(args: argparse.Namespace, manager: AddonManager, out: Any) -> int:
    requests = manager.pending_requests(args.name)
    if not requests:
        print("no install requests pending", file=out)
        return 0
    print(f"{len(requests)} request(s) pending:", file=out)
    for item in requests:
        print(
            f"\n  {item['name']}  requested {item.get('requested_at')} by {item.get('requested_by')}",
            file=out,
        )
        print(f"    reason:  {item.get('reason')}", file=out)
        if item.get("requested_argv"):
            print(f"    argv:    {' '.join(item['requested_argv'])}", file=out)
        print(f"    to act:  iot-mcp-addons install {item['name']}", file=out)
    return 0


def _cmd_request(args: argparse.Namespace, manager: AddonManager, out: Any) -> int:
    record = manager.request_install(
        args.name,
        reason=args.reason,
        requested_by=args.requested_by,
    )
    print(f"filed: {record['request_file']}", file=out)
    print(
        "nothing was installed or approved. An operator must run:\n"
        f"  iot-mcp-addons install {record['name']}",
        file=out,
    )
    return 0


def _cmd_install(args: argparse.Namespace, manager: AddonManager, out: Any) -> int:
    argv = _parse_argv(args.argv) if args.argv else None
    _show_install_context(args.name, manager, out)

    print(file=out)
    print(RISK_STATEMENT, file=out)
    launch = argv if argv is not None else DEFAULT_LAUNCH.get(args.name)
    if launch is None:
        print(
            f"no default launch command for {args.name!r}; pass --argv with the exact command "
            "you want to approve",
            file=out,
        )
        return 2
    print(f"This will approve exactly:\n  {' '.join(launch)}", file=out)
    if not args.yes:
        expected = f"{_CONFIRMATION_PREFIX} {args.name}"
        print(f"\nType `{expected}` to approve, anything else to cancel.", file=out)
        try:
            answer = input("> ").strip()
        except EOFError:
            print("(no input — cancelled)", file=out)
            return 1
        if answer != expected:
            print("cancelled: nothing was approved.", file=out)
            return 1
    else:
        print(
            "\n--yes: the typed confirmation was skipped. If you did not mean to approve this, "
            f"run `iot-mcp-addons revoke {args.name}` now.",
            file=out,
        )

    entry = manager.approve(
        args.name,
        argv=list(launch),
        package=_package_of(launch),
        notes=args.notes,
    )
    cleared = manager.clear_requests(args.name)
    print(
        f"\napproved {entry.name}: {' '.join(entry.argv)}\n"
        f"  fingerprint: {entry.fingerprint}\n"
        f"  record:      {manager.entries_dir / (entry.name + '.json')}"
        + (f"\n  cleared {cleared} pending request(s)" if cleared else ""),
        file=out,
    )
    print(
        "\nThe running server did not pick this up — approvals are read at startup.\n"
        "Restart it with the browser tier enabled:\n"
        "  python -m iot_mcp --browser",
        file=out,
    )
    return 0


def _cmd_revoke(args: argparse.Namespace, manager: AddonManager, out: Any) -> int:
    removed = manager.revoke(args.name)
    print(
        f"revoked {args.name}." if removed else f"no approval record for {args.name} to revoke.",
        file=out,
    )
    if removed:
        print(
            "A server already running the add-on keeps running it until it restarts or the "
            "add-on is stopped — the record gates startup and launch, not a live process.",
            file=out,
        )
    return 0


def _show_install_context(name: str, manager: AddonManager, out: Any) -> None:
    """Anything a human should read before the risk statement: existing approval, requests."""
    existing = manager.approved(name)
    if existing:
        print(f"Already approved: {' '.join(existing.argv)}", file=out)
        print(f"  approved {existing.approved_at} by {existing.approved_by}", file=out)
        print("  approving again replaces that record.\n", file=out)
    invalid = manager.explain_invalid(name)
    if invalid:
        print(f"Existing record is unusable and will be replaced: {invalid}\n", file=out)
    pending = [item for item in manager.pending_requests(name)]
    if pending:
        latest = pending[-1]
        print(f"An agent asked for this ({latest.get('requested_by')}):", file=out)
        print(f"  {latest.get('reason')}", file=out)
        if len(pending) > 1:
            print(f"  ({len(pending)} requests total; the newest one is shown)", file=out)
        print("", file=out)


def _invalid_records(manager: AddonManager) -> list[tuple[str, str]]:
    """Records on disk that fail validation, with the reason."""
    out: list[tuple[str, str]] = []
    for file in sorted(manager.entries_dir.glob("*.json")) if manager.entries_dir.is_dir() else []:
        reason = manager.explain_invalid(file.stem)
        if reason:
            out.append((file.stem, reason))
    return out


def _package_of(argv: tuple[str, ...] | list[str]) -> str:
    """Best-effort upstream package spec, recorded for humans (never executed)."""
    known = str(PLAYWRIGHT_PACKAGE_SPEC)
    for item in argv:
        if str(item).startswith("@") and "/" in str(item):
            return str(item)
    return known if known in " ".join(argv) else ""


def _parse_argv(raw: str) -> tuple[str, ...]:
    """Split an ``--argv`` value without shell expansion."""
    import shlex

    try:
        parts = tuple(shlex.split(raw))
    except ValueError as exc:
        raise AddonError(f"--argv is not a valid command line: {exc}") from exc
    if not parts:
        raise AddonError("--argv is empty; pass a command or omit it to use the default")
    if shutil.which(parts[0]) is None:
        raise AddonError(
            f"{parts[0]!r} is not on PATH; approving it would create an add-on that never starts"
        )
    return parts


def main(argv: list[str] | None = None) -> int:
    """Console entry point.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        Process exit code. ``1`` means the operator declined or a state change did not happen;
        ``2`` means the request itself was unusable.
    """
    args = build_parser().parse_args(argv)
    manager = _manager(args)
    try:
        return int(args.func(args, manager, sys.stdout))
    except (AddonError, ValueError) as exc:
        print(f"iot-mcp-addons: {exc}", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive cancel
        print("\ncancelled: nothing was changed.", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
