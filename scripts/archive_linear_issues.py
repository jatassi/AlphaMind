#!/usr/bin/env python3
"""Archive Done Linear sub-issues to free space under the free-tier cap.

Linear's free tier caps a workspace at roughly 250 active (non-archived)
issues. The `/linear-consolidate` workflow rolls a shipped feature's Done
sub-issues up into the parent issue's body, then the sub-issues are
removed. This script does the removal — by *archiving*, not trashing:
archived issues stop counting against the cap yet stay recoverable
indefinitely via Linear's archive view, whereas trashed issues are purged
after 30 days.

It is dry-run by default — it prints exactly what it would archive and
changes nothing. Re-run with --apply to perform the archive. The intended
flow is: dry-run -> show the operator the plan -> get explicit approval ->
re-run with --apply.

Usage:
    # preview a parent's Done sub-issues (changes nothing)
    uv run python scripts/archive_linear_issues.py --parent ALP-117
    # archive them, after operator approval
    uv run python scripts/archive_linear_issues.py --parent ALP-117 --apply
    # archive an explicit set
    uv run python scripts/archive_linear_issues.py --ids ALP-321,ALP-322 --apply

Exit codes:
    0   dry-run completed, or --apply archived everything planned
    1   --apply ran but one or more archives failed
    2   configuration, API, or usage error

Environment:
    LINEAR_API_KEY    required (read from the repo-root .env if present)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from alphamind.scripts._linear import (
    LinearError,
    archive_issue,
    iter_children,
    load_api_key,
    resolve_issue,
)
from alphamind.scripts._stdio import configure_utf8_stdio

_ARCHIVE = "ARCHIVE"
_SKIP_NOT_DONE = "SKIP-NOT-DONE"
_ALREADY_ARCHIVED = "ALREADY-ARCHIVED"


@dataclass
class Target:
    """One issue considered for archiving, with its planned disposition."""

    identifier: str
    uuid: str
    title: str
    state: str
    planned: str  # _ARCHIVE | _SKIP_NOT_DONE | _ALREADY_ARCHIVED
    result: str = ""  # filled by the --apply pass: ARCHIVED | FAILED (...)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive Done Linear sub-issues to free free-tier cap slots.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--parent",
        metavar="ALP-N",
        help="archive the Done sub-issues of this parent issue",
    )
    source.add_argument(
        "--ids",
        metavar="ALP-N,ALP-M,...",
        help="archive this explicit comma-separated list of issues",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually archive (default: dry-run — print the plan, change nothing)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a single-line JSON object instead of human-readable text",
    )
    return parser.parse_args()


def _targets_from_parent(api_key: str, identifier: str) -> list[Target]:
    """Resolve a parent to its Done sub-issues, refusing an in-flight parent."""
    parent = resolve_issue(api_key, identifier)
    state = parent.get("state") or {}
    if state.get("type") != "completed":
        raise LinearError(
            f"parent {parent['identifier']} is '{state.get('name')}', not Done — "
            f"its sub-issues may still be needed; use --ids to override deliberately"
        )
    targets: list[Target] = []
    for child in iter_children(api_key, parent["id"]):
        child_state = child.get("state") or {}
        is_done = child_state.get("type") == "completed"
        targets.append(
            Target(
                identifier=child["identifier"],
                uuid=child["id"],
                title=child["title"],
                state=child_state.get("name") or "(unknown)",
                planned=_ARCHIVE if is_done else _SKIP_NOT_DONE,
            )
        )
    return targets


def _targets_from_ids(api_key: str, raw: str) -> list[Target]:
    """Resolve an explicit comma-separated identifier list to targets."""
    identifiers = [part.strip() for part in raw.split(",") if part.strip()]
    if not identifiers:
        raise LinearError("--ids was empty")
    targets: list[Target] = []
    for identifier in identifiers:
        node = resolve_issue(api_key, identifier)
        state = node.get("state") or {}
        already_archived = node.get("archivedAt") is not None
        targets.append(
            Target(
                identifier=node["identifier"],
                uuid=node["id"],
                title=node["title"],
                state=state.get("name") or "(unknown)",
                planned=_ALREADY_ARCHIVED if already_archived else _ARCHIVE,
            )
        )
    return targets


def _apply(api_key: str, targets: list[Target]) -> None:
    """Archive every target planned for _ARCHIVE, recording the per-issue result."""
    for target in targets:
        if target.planned != _ARCHIVE:
            continue
        try:
            ok = archive_issue(api_key, target.uuid)
        except LinearError as exc:
            target.result = f"FAILED ({exc})"
        else:
            target.result = "ARCHIVED" if ok else "FAILED (mutation returned success=false)"


def _action_label(target: Target, *, applied: bool) -> str:
    if target.planned != _ARCHIVE:
        return target.planned
    if applied:
        return target.result or "FAILED (not attempted)"
    return "WOULD-ARCHIVE"


def _emit_human(args: argparse.Namespace, targets: list[Target]) -> None:
    scope = f"parent {args.parent}" if args.parent else f"{len(targets)} issue(s)"
    print(f"Archive plan — {scope} ({'applied' if args.apply else 'dry-run'})\n")
    id_width = max((len(t.identifier) for t in targets), default=0)
    for target in targets:
        action = _action_label(target, applied=args.apply)
        print(
            f"  {target.identifier:<{id_width}}  {action:<22}  {target.state:<12}  {target.title}"
        )

    planned = sum(1 for t in targets if t.planned == _ARCHIVE)
    skipped = len(targets) - planned
    print()
    if args.apply:
        archived = sum(1 for t in targets if t.result == "ARCHIVED")
        failed = planned - archived
        print(f"Archived {archived} issue(s); {failed} failed; {skipped} skipped.")
        if archived:
            print("Re-run scripts/check_linear_cap.py to see the freed buffer.")
    else:
        print(f"{planned} issue(s) would be archived; {skipped} skipped.")
        if planned:
            print("Re-run with --apply to archive them (after operator approval).")


def _emit_json(args: argparse.Namespace, targets: list[Target]) -> None:
    planned = sum(1 for t in targets if t.planned == _ARCHIVE)
    archived = sum(1 for t in targets if t.result == "ARCHIVED")
    payload: dict[str, Any] = {
        "mode": "parent" if args.parent else "ids",
        "source": args.parent or args.ids,
        "applied": args.apply,
        "to_archive": planned,
        "archived": archived,
        "failed": planned - archived if args.apply else 0,
        "issues": [
            {
                "identifier": t.identifier,
                "title": t.title,
                "state": t.state,
                "planned": t.planned,
                "result": t.result,
            }
            for t in targets
        ],
    }
    print(json.dumps(payload))


def main() -> int:
    configure_utf8_stdio()
    args = _parse_args()

    try:
        api_key = load_api_key()
        if args.parent:
            targets = _targets_from_parent(api_key, args.parent)
        else:
            targets = _targets_from_ids(api_key, args.ids)
    except LinearError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not targets:
        print(f"no sub-issues found for {args.parent} — nothing to archive")
        return 0

    if args.apply:
        _apply(api_key, targets)

    if args.json:
        _emit_json(args, targets)
    else:
        _emit_human(args, targets)

    failed = any(t.result.startswith("FAILED") for t in targets)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
