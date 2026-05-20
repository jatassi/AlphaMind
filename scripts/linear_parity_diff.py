#!/usr/bin/env python3
"""Diff a Linear parent's sub-issue bodies against their local archive files.

Before `/linear-consolidate` archives a feature's Done sub-issues, the
local archive at `docs/_archive/implementation/<layer>/<feature>/` must
faithfully mirror the Linear sub-issue bodies — the local copy is what
survives archiving. This script does that parity check deterministically:
it fetches every sub-issue body via the Linear GraphQL API (no MCP ~5KB
truncation), matches each to a local `.md` file by user-story index, and
reports MATCH / DIFFERS with a unified diff for every mismatch.

It normalises away the differences the check is meant to ignore — leading
YAML frontmatter, heading levels, and Linear's `<issue id=...>` cross-ref
markup — so a DIFFERS verdict reflects real content drift.

Usage:
    uv run python scripts/linear_parity_diff.py ALP-114 \\
        --archive-dir docs/_archive/implementation/03-analysis-layer/synthesizer
    uv run python scripts/linear_parity_diff.py ALP-114 --archive-dir DIR --json

Exit codes:
    0   every sub-issue matches its local archive file
    1   drift found — DIFFERS, or a sub-issue / file with no counterpart
    2   configuration, API, or usage error

Environment:
    LINEAR_API_KEY    required (read from the repo-root .env if present)
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphamind.scripts._linear import (
    ISSUE_FIELDS_WITH_BODY,
    LinearError,
    iter_children,
    load_api_key,
    resolve_issue,
)
from alphamind.scripts._stdio import configure_utf8_stdio

_MATCH = "MATCH"
_DIFFERS = "DIFFERS"
_LINEAR_ONLY = "LINEAR-ONLY"
_LOCAL_ONLY = "LOCAL-ONLY"

_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_ISSUE_REF = re.compile(r'<issue id="[^"]*">([^<]*)</issue>')
_HEADING = re.compile(r"^#+[ \t]*", re.MULTILINE)
_INDEX = re.compile(r"\s*(\d+[a-z]?)")


@dataclass
class Row:
    """One sub-issue / archive-file pairing and its parity verdict."""

    index: str
    identifier: str
    filename: str
    status: str
    diff: str = ""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diff a Linear parent's sub-issue bodies against local archive files.",
    )
    parser.add_argument("parent", metavar="ALP-N", help="the parent issue identifier")
    parser.add_argument(
        "--archive-dir",
        required=True,
        metavar="DIR",
        help="directory of local .md archive files to compare against",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="also compare already-archived sub-issues (default: non-archived only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a single-line JSON object instead of human-readable text",
    )
    return parser.parse_args()


def _normalize(text: str) -> str:
    """Strip the differences the parity check is meant to ignore.

    Removes leading YAML frontmatter, collapses heading levels, rewrites
    Linear's ``<issue id=...>ALP-N</issue>`` cross-refs to plain ``ALP-N``,
    trims per-line trailing whitespace, and collapses blank-line runs.
    """
    text = _FRONTMATTER.sub("", text)
    text = _ISSUE_REF.sub(r"\1", text)
    text = _HEADING.sub("", text)
    out: list[str] = []
    for raw in text.strip().splitlines():
        line = raw.rstrip()
        if line == "" and out and out[-1] == "":
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _index_of(text: str) -> str | None:
    """Extract the leading user-story index (``01``, ``03a``, …) if present."""
    match = _INDEX.match(text)
    return match.group(1) if match else None


def _unified_diff(local_body: str, linear_body: str, row: Row) -> str:
    """Return a unified diff of the normalised bodies, or '' when they match."""
    local_norm = _normalize(local_body)
    linear_norm = _normalize(linear_body)
    if local_norm == linear_norm:
        return ""
    return "".join(
        difflib.unified_diff(
            local_norm.splitlines(keepends=True),
            linear_norm.splitlines(keepends=True),
            fromfile=f"local:{row.filename}",
            tofile=f"linear:{row.identifier}",
        )
    )


def _build_rows(children: list[dict[str, Any]], archive_dir: Path) -> list[Row]:
    """Match each sub-issue to a local archive file by index and diff the pair."""
    file_by_index: dict[str, Path] = {}
    for path in sorted(archive_dir.iterdir()):
        if path.suffix != ".md":
            continue
        idx = _index_of(path.name)
        if idx is not None:
            file_by_index.setdefault(idx, path)

    rows: list[Row] = []
    used: set[str] = set()
    for child in children:
        idx = _index_of(child["title"]) or "?"
        matched = file_by_index.get(idx)
        if matched is None:
            rows.append(Row(idx, child["identifier"], "(none)", _LINEAR_ONLY))
            continue
        used.add(idx)
        row = Row(idx, child["identifier"], matched.name, _MATCH)
        row.diff = _unified_diff(
            matched.read_text(encoding="utf-8"), child.get("description") or "", row
        )
        row.status = _DIFFERS if row.diff else _MATCH
        rows.append(row)

    for idx, path in file_by_index.items():
        if idx not in used:
            rows.append(Row(idx, "(none)", path.name, _LOCAL_ONLY))

    rows.sort(key=lambda r: r.index)
    return rows


def _emit_human(parent: str, archive_dir: Path, rows: list[Row]) -> None:
    print(f"Parity check — {parent} vs {archive_dir}\n")
    id_width = max((len(r.identifier) for r in rows), default=0)
    idx_width = max((len(r.index) for r in rows), default=0)
    for row in rows:
        print(
            f"  {row.index:<{idx_width}}  {row.identifier:<{id_width}}  "
            f"{row.status:<12}  {row.filename}"
        )

    statuses = (_MATCH, _DIFFERS, _LINEAR_ONLY, _LOCAL_ONLY)
    counts = {status: sum(1 for r in rows if r.status == status) for status in statuses}
    print(
        f"\n{counts[_MATCH]} match, {counts[_DIFFERS]} differs, "
        f"{counts[_LINEAR_ONLY]} linear-only, {counts[_LOCAL_ONLY]} local-only."
    )
    for row in rows:
        if row.diff:
            print(f"\n--- diff: {row.index} ({row.identifier}) ---")
            print(row.diff, end="")
    if counts[_DIFFERS] or counts[_LINEAR_ONLY] or counts[_LOCAL_ONLY]:
        print("\nDrift found — resolve before archiving (see /linear-consolidate Phase 3).")
    else:
        print("\nEvery sub-issue matches its local archive — safe to archive.")


def _emit_json(parent: str, archive_dir: Path, rows: list[Row]) -> None:
    payload: dict[str, Any] = {
        "parent": parent,
        "archive_dir": str(archive_dir),
        "match": sum(1 for r in rows if r.status == _MATCH),
        "differs": sum(1 for r in rows if r.status == _DIFFERS),
        "linear_only": sum(1 for r in rows if r.status == _LINEAR_ONLY),
        "local_only": sum(1 for r in rows if r.status == _LOCAL_ONLY),
        "rows": [
            {
                "index": r.index,
                "identifier": r.identifier,
                "filename": r.filename,
                "status": r.status,
                "diff": r.diff,
            }
            for r in rows
        ],
    }
    print(json.dumps(payload))


def main() -> int:
    configure_utf8_stdio()
    args = _parse_args()

    archive_dir = Path(args.archive_dir)
    if not archive_dir.is_dir():
        print(f"error: archive directory not found: {archive_dir}", file=sys.stderr)
        return 2

    try:
        api_key = load_api_key()
        parent = resolve_issue(api_key, args.parent)
        children = list(
            iter_children(
                api_key,
                parent["id"],
                fields=ISSUE_FIELDS_WITH_BODY,
                include_archived=args.include_archived,
            )
        )
    except LinearError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not children:
        print(f"{args.parent} has no sub-issues to compare")
        return 0

    rows = _build_rows(children, archive_dir)
    if args.json:
        _emit_json(args.parent, archive_dir, rows)
    else:
        _emit_human(args.parent, archive_dir, rows)

    drift = any(r.status != _MATCH for r in rows)
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
