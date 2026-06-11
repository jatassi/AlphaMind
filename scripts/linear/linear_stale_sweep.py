#!/usr/bin/env python3
"""Sweep the Linear workspace for stale issues that waste the free-tier cap.

Two classes of dead-weight are reported:

1. **Archivable dead-end issues** — non-archived issues in any *canceled*
   workflow state (``state.type == "canceled"``).  That includes both the
   ``Canceled`` and the ``Duplicate`` workflow states.  All of them count
   against the 250-issue free-tier cap yet carry no live work; archiving
   them is safe and immediately frees the slot.

2. **Duplicates missing a ``duplicateOf`` link** — issues whose workflow
   state *name* is ``"Duplicate"`` but for which neither ``relations`` nor
   ``inverseRelations`` contains a node of type ``"duplicate"``.  Per
   AlphaMind convention, marking a duplicate requires *both* the state and
   the relation; issues missing the relation are a data-integrity gap.

Usage:
    uv run python scripts/linear/linear_stale_sweep.py
    uv run python scripts/linear/linear_stale_sweep.py --team AlphaMind
    uv run python scripts/linear/linear_stale_sweep.py --json

Exit codes:
    0   ran successfully (report produced; 0 even when both sections empty)
    2   configuration or API error (missing key, network or GraphQL failure)

Environment:
    LINEAR_API_KEY    required (read from the repo-root .env if present)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from alphamind.scripts._linear import (
    ISSUE_FIELDS,
    LinearError,
    iter_issues,
    load_api_key,
)
from alphamind.scripts._stdio import configure_utf8_stdio

# Extend the standard field set with the relation sub-selections needed to
# detect "Duplicate" issues that are missing their ``duplicateOf`` link.
_SWEEP_FIELDS = (
    ISSUE_FIELDS + " relations { nodes { type } }" + " inverseRelations { nodes { type } }"
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Report Linear issues that count against the free-tier cap but carry no live value."
        ),
    )
    parser.add_argument(
        "--team",
        default=None,
        metavar="NAME",
        help="restrict the sweep to a single team by name (default: whole workspace)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a single-line JSON object instead of human-readable text",
    )
    return parser.parse_args()


def _team_filter(team: str | None) -> dict[str, Any] | None:
    return {"team": {"name": {"eq": team}}} if team else None


def _has_duplicate_relation(issue: dict[str, Any]) -> bool:
    """Return True if the issue carries at least one 'duplicate' relation."""
    for key in ("relations", "inverseRelations"):
        container = issue.get(key) or {}
        nodes = container.get("nodes") or []
        for node in nodes:
            if node.get("type") == "duplicate":
                return True
    return False


def _classify(
    issues: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Split issues into (archivable_dead_ends, duplicates_missing_link)."""
    archivable: list[dict[str, Any]] = []
    missing_link: list[dict[str, Any]] = []
    for issue in issues:
        state = issue.get("state") or {}
        state_type: str = state.get("type") or ""
        state_name: str = state.get("name") or ""
        if state_type == "canceled":
            archivable.append(issue)
        if state_name == "Duplicate" and not _has_duplicate_relation(issue):
            missing_link.append(issue)
    return archivable, missing_link


def _print_table(
    rows: list[dict[str, Any]],
    include_state: bool = False,
) -> None:
    if not rows:
        print("  (none)")
        return
    id_w = max(len(r.get("identifier") or "") for r in rows)
    state_w = (
        max(len((r.get("state") or {}).get("name") or "") for r in rows) if include_state else 0
    )
    for row in rows:
        identifier = (row.get("identifier") or "").ljust(id_w)
        title = row.get("title") or ""
        if include_state:
            state_name = ((row.get("state") or {}).get("name") or "").ljust(state_w)
            print(f"  {identifier}  {state_name}  {title}")
        else:
            print(f"  {identifier}  {title}")


def _print_report(
    archivable: list[dict[str, Any]],
    missing_link: list[dict[str, Any]],
) -> None:
    n = len(archivable)
    print("Archivable dead-end issues (count against the 250 cap):")
    _print_table(archivable, include_state=True)
    plural = "s" if n != 1 else ""
    print(f"→ {n} issue{plural}; archiving them frees {n} slot{plural}.")
    print()
    print("Duplicates missing a duplicateOf link:")
    _print_table(missing_link, include_state=False)
    m = len(missing_link)
    print(f"→ {m} issue{'s' if m != 1 else ''} missing a duplicate relation.")


def _build_json(
    archivable: list[dict[str, Any]],
    missing_link: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "archivable": [
            {
                "identifier": row.get("identifier"),
                "state": (row.get("state") or {}).get("name"),
                "title": row.get("title"),
            }
            for row in archivable
        ],
        "archivable_count": len(archivable),
        "duplicates_missing_link": [
            {
                "identifier": row.get("identifier"),
                "title": row.get("title"),
            }
            for row in missing_link
        ],
    }


def main() -> int:
    configure_utf8_stdio()
    args = _parse_args()

    try:
        api_key = load_api_key()
        issues = list(
            iter_issues(
                api_key,
                issue_filter=_team_filter(args.team),
                fields=_SWEEP_FIELDS,
            )
        )
    except LinearError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    archivable, missing_link = _classify(issues)

    if args.json:
        print(json.dumps(_build_json(archivable, missing_link)))
    else:
        _print_report(archivable, missing_link)

    return 0


if __name__ == "__main__":
    sys.exit(main())
