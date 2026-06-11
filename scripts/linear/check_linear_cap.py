#!/usr/bin/env python3
"""Check AlphaMind's Linear workspace against the free-tier issue cap.

Linear's free tier caps a workspace at roughly 250 active (non-archived)
issues. Hitting the cap blocks new issue creation, which strands a
``/draft-user-stories`` run half-saved and forces a consolidation rollup
before work can resume. This script reports the exact active count — and,
with ``--breakdown``, where those issues sit by workflow state and project,
which points at the fattest consolidation targets — so the cap can be
checked *before* any writes.

It queries the Linear GraphQL API directly and paginates the workspace,
returning an exact count, unlike the MCP ``list_issues`` probe, which can
only report ">= 250" once a single page fills.

Usage:
    uv run python scripts/linear/check_linear_cap.py
    uv run python scripts/linear/check_linear_cap.py --needed 12
    uv run python scripts/linear/check_linear_cap.py --needed 12 --json
    uv run python scripts/linear/check_linear_cap.py --breakdown
    uv run python scripts/linear/check_linear_cap.py --team AlphaMind

Exit codes:
    0   under the cap, and (if --needed given) the buffer covers it
    1   at/over the cap, or the buffer does not cover --needed + margin
    2   configuration or API error (missing key, network/API failure)

Environment:
    LINEAR_API_KEY    required (read from the repo-root .env if present)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any

from alphamind.scripts._linear import (
    FREE_TIER_CAP,
    LinearError,
    iter_issues,
    load_api_key,
)
from alphamind.scripts._stdio import configure_utf8_stdio

_DEFAULT_MARGIN = 2


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check AlphaMind's Linear workspace against the free-tier issue cap.",
    )
    parser.add_argument(
        "--needed",
        type=int,
        default=0,
        metavar="N",
        help="number of new issues you plan to create; the script reports whether "
        "the buffer covers them (a safety margin is added — see --margin)",
    )
    parser.add_argument(
        "--margin",
        type=int,
        default=_DEFAULT_MARGIN,
        metavar="N",
        help=f"safety margin added to --needed for retries / concurrent activity "
        f"(default: {_DEFAULT_MARGIN})",
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=FREE_TIER_CAP,
        metavar="N",
        help=f"free-tier issue cap to measure against (default: {FREE_TIER_CAP})",
    )
    parser.add_argument(
        "--team",
        default=None,
        metavar="NAME",
        help="restrict the count to a single team by name (default: whole workspace)",
    )
    parser.add_argument(
        "--breakdown",
        action="store_true",
        help="also show the active issue count split by workflow state and by project",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a single-line JSON object instead of human-readable text",
    )
    return parser.parse_args()


def _team_filter(team: str | None) -> dict[str, Any] | None:
    return {"team": {"name": {"eq": team}}} if team else None


def _breakdown(issues: list[dict[str, Any]]) -> tuple[Counter[str], Counter[str]]:
    """Tally the issues by workflow-state name and by project name."""
    by_state: Counter[str] = Counter()
    by_project: Counter[str] = Counter()
    for issue in issues:
        state = issue.get("state") or {}
        project = issue.get("project") or {}
        by_state[state.get("name") or "(no state)"] += 1
        by_project[project.get("name") or "(no project)"] += 1
    return by_state, by_project


def _print_tally(heading: str, tally: Counter[str]) -> None:
    print(f"\n{heading}")
    width = max((len(name) for name in tally), default=0)
    for name, count in tally.most_common():
        print(f"  {name:<{width}} : {count:>4}")


def _print_report(
    args: argparse.Namespace,
    issues: list[dict[str, Any]],
    buffer: int,
    required: int,
    ok: bool,
) -> None:
    active = len(issues)
    scope = f"team '{args.team}'" if args.team else "workspace"
    print(f"Linear free-tier cap check ({scope})")
    print(f"  active (non-archived) issues : {active}")
    print(f"  free-tier cap                : {args.cap}")
    print(f"  buffer remaining             : {buffer}")
    if args.needed:
        print(
            f"  planned new issues           : {args.needed}  "
            f"(+{args.margin} margin = {required} required)"
        )
    if ok and args.needed:
        print(f"  status                       : OK — buffer covers {required} required slots")
    elif ok:
        print(f"  status                       : OK — {buffer} slots free")
    elif active >= args.cap:
        print(
            f"  status                       : CAP REACHED — at or over the "
            f"{args.cap}-issue cap; run /linear-consolidate before creating issues"
        )
    else:
        print(
            f"  status                       : CAP RISK — buffer {buffer} < {required} "
            f"required; run /linear-consolidate before drafting"
        )
    if args.breakdown:
        by_state, by_project = _breakdown(issues)
        _print_tally("By workflow state:", by_state)
        _print_tally("By project:", by_project)


def main() -> int:
    configure_utf8_stdio()
    args = _parse_args()

    try:
        api_key = load_api_key()
        issues = list(iter_issues(api_key, issue_filter=_team_filter(args.team)))
    except LinearError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    active = len(issues)
    buffer = args.cap - active
    required = args.needed + args.margin if args.needed else 0
    ok = active < args.cap and buffer >= required

    if args.json:
        result: dict[str, Any] = {
            "active": active,
            "cap": args.cap,
            "buffer": buffer,
            "needed": args.needed,
            "margin": args.margin,
            "required": required,
            "ok": ok,
        }
        if args.breakdown:
            by_state, by_project = _breakdown(issues)
            result["by_state"] = dict(by_state.most_common())
            result["by_project"] = dict(by_project.most_common())
        print(json.dumps(result))
    else:
        _print_report(args, issues, buffer, required, ok)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
