#!/usr/bin/env python3
"""List Linear feature-parent issues that are ready to consolidate.

A parent issue is a "ready candidate" when it is itself Done (state.type ==
"completed") and every one of its sub-issues is also Done. Rolling up such a
parent frees one Linear slot per sub-issue archived, which is reported as
``slots_freed``. This automates the hand-maintained "which feature to roll up
next" tracking used by the /linear-consolidate workflow.

Usage:
    uv run python scripts/linear/linear_consolidation_candidates.py
    uv run python scripts/linear/linear_consolidation_candidates.py --team AlphaMind
    uv run python scripts/linear/linear_consolidation_candidates.py --json

Exit codes:
    0   ran successfully (report produced; exit 0 even when there are no
        ready candidates)
    2   configuration or API error (missing key, network/API failure)

Environment:
    LINEAR_API_KEY    required (read from the repo-root .env if present)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

from alphamind.scripts._linear import LinearError, iter_issues, load_api_key
from alphamind.scripts._stdio import configure_utf8_stdio


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List Linear feature-parent issues ready to consolidate.",
    )
    parser.add_argument(
        "--team",
        default=None,
        metavar="NAME",
        help="restrict to a single team by name (default: whole workspace)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a single-line JSON object instead of the human report",
    )
    return parser.parse_args()


def _team_filter(team: str | None) -> dict[str, Any] | None:
    return {"team": {"name": {"eq": team}}} if team else None


def _build_candidates(
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Return (ready_candidates, in_flight_count).

    ready_candidates is sorted by slots_freed descending.
    """
    children_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        parent = issue.get("parent")
        if parent is not None:
            children_by_parent[parent["id"]].append(issue)

    by_id = {issue["id"]: issue for issue in issues}

    ready: list[dict[str, Any]] = []
    in_flight = 0

    for parent_id, children in children_by_parent.items():
        parent = by_id.get(parent_id)
        if parent is None:
            # Parent is outside the filtered set — skip.
            continue

        parent_state = (parent.get("state") or {}).get("type", "")
        all_children_done = all(
            (child.get("state") or {}).get("type") == "completed" for child in children
        )

        if parent_state == "completed" and all_children_done:
            project = parent.get("project") or {}
            ready.append(
                {
                    "identifier": parent.get("identifier", ""),
                    "title": parent.get("title", ""),
                    "slots_freed": len(children),
                    "project": project.get("name") or "(no project)",
                }
            )
        else:
            in_flight += 1

    ready.sort(key=lambda c: c["slots_freed"], reverse=True)
    return ready, in_flight


def _print_report(
    candidates: list[dict[str, Any]],
    in_flight: int,
    team: str | None,
) -> None:
    scope = f"team '{team}'" if team else "workspace"
    print(f"Linear consolidation candidates ({scope})")

    if not candidates:
        print("  No ready candidates found.")
    else:
        id_width = max(len(c["identifier"]) for c in candidates)
        title_width = max(len(c["title"]) for c in candidates)
        proj_width = max(len(c["project"]) for c in candidates)

        header = (
            f"  {'PARENT':<{id_width}}  "
            f"{'TITLE':<{title_width}}  "
            f"{'SUB-ISSUES':>10}  "
            f"{'PROJECT':<{proj_width}}"
        )
        print(header)
        print("  " + "-" * (len(header) - 2))

        for c in candidates:
            print(
                f"  {c['identifier']:<{id_width}}  "
                f"{c['title']:<{title_width}}  "
                f"{c['slots_freed']:>10}  "
                f"{c['project']:<{proj_width}}"
            )

    total_slots = sum(c["slots_freed"] for c in candidates)
    n = len(candidates)
    print(
        f"\n  {n} ready candidate{'s' if n != 1 else ''} would free "
        f"{total_slots} slot{'s' if total_slots != 1 else ''} total; "
        f"{in_flight} parent{'s' if in_flight != 1 else ''} still in flight (skipped)."
    )


def main() -> int:
    configure_utf8_stdio()
    args = _parse_args()

    try:
        api_key = load_api_key()
        issues = list(iter_issues(api_key, issue_filter=_team_filter(args.team)))
    except LinearError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    candidates, in_flight = _build_candidates(issues)
    total_slots = sum(c["slots_freed"] for c in candidates)

    if args.json:
        print(
            json.dumps(
                {
                    "candidates": candidates,
                    "total_slots": total_slots,
                    "in_flight": in_flight,
                }
            )
        )
    else:
        _print_report(candidates, in_flight, args.team)

    return 0


if __name__ == "__main__":
    sys.exit(main())
