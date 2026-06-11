#!/usr/bin/env python3
"""Export every sub-issue of a Linear parent issue to individual Markdown files.

Fetches the full issue body for each child via the GraphQL API (no 5 KB
truncation), writing one ``.md`` file per child into a caller-specified
directory. Designed to replace manual re-fetching through the Linear MCP
server during orchestration handovers.

Usage:
    uv run python scripts/linear/export_linear_issues.py PARENT --out-dir DIR
    uv run python scripts/linear/export_linear_issues.py ALP-114 --out-dir /tmp/export
    uv run python scripts/linear/export_linear_issues.py ALP-114 --out-dir /tmp/export \
        --include-archived
    uv run python scripts/linear/export_linear_issues.py ALP-114 --out-dir /tmp/export --json

Exit codes:
    0   completed successfully (including when the parent has zero children)
    2   configuration or API error (missing key, network failure, bad identifier)

Environment:
    LINEAR_API_KEY    required (read from the repo-root .env if present)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export every sub-issue of a Linear parent issue to Markdown files.",
    )
    parser.add_argument(
        "parent",
        metavar="PARENT",
        help="Linear issue identifier of the parent, e.g. ALP-114",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        metavar="DIR",
        help="directory to write .md files into (created if absent)",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="also export archived sub-issues (default: non-archived only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a single-line JSON summary instead of a human-readable table",
    )
    return parser.parse_args()


def _slugify(title: str) -> str:
    """Derive a safe filename slug from *title*.

    Algorithm: lowercase; replace every run of characters not in ``[a-z0-9]``
    with a single ``-``; strip leading/trailing ``-``.

    Examples:
        "03a — Bracket-thesis coverage cross-validator"
        -> "03a-bracket-thesis-coverage-cross-validator"
    """
    lowered = title.lower()
    slugged = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slugged.strip("-")


def _unique_filename(slug: str, seen: set[str]) -> str:
    """Return *slug* + ``.md``, suffixing ``-2``, ``-3``, … on collision."""
    candidate = slug
    counter = 2
    while candidate in seen:
        candidate = f"{slug}-{counter}"
        counter += 1
    seen.add(candidate)
    return candidate + ".md"


def _write_files(
    children: list[dict[str, Any]],
    out_dir: Path,
) -> list[dict[str, Any]]:
    """Write each child's description to *out_dir*.

    Returns a list of row dicts with keys ``identifier``, ``file``,
    ``bytes``, and ``status`` (``"WRITTEN"`` or ``"EMPTY"``).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    seen_slugs: set[str] = set()
    rows: list[dict[str, Any]] = []

    for child in children:
        identifier: str = child.get("identifier") or ""
        title: str = child.get("title") or identifier
        description: str | None = child.get("description") or None

        slug = _slugify(title) or _slugify(identifier) or "untitled"
        filename = _unique_filename(slug, seen_slugs)

        if not description:
            rows.append(
                {
                    "identifier": identifier,
                    "file": filename,
                    "bytes": 0,
                    "status": "EMPTY",
                }
            )
            continue

        target = out_dir / filename
        encoded = description.encode()
        target.write_bytes(encoded)
        rows.append(
            {
                "identifier": identifier,
                "file": filename,
                "bytes": len(encoded),
                "status": "WRITTEN",
            }
        )

    return rows


def _print_table(
    rows: list[dict[str, Any]],
    written: int,
    empty: int,
    out_dir: Path,
) -> None:
    if not rows:
        print("No sub-issues found.")
        return

    id_w = max(len(r["identifier"]) for r in rows)
    file_w = max(len(r["file"]) for r in rows)
    print(f"{'IDENTIFIER':<{id_w}}  {'FILE':<{file_w}}  {'BYTES':>8}  STATUS")
    print("-" * (id_w + file_w + 22))
    for row in rows:
        print(
            f"{row['identifier']:<{id_w}}  {row['file']:<{file_w}}"
            f"  {row['bytes']:>8}  {row['status']}"
        )
    print(f"\n{written} written, {empty} empty -> {out_dir}")


def main() -> int:
    configure_utf8_stdio()
    args = _parse_args()
    out_dir = Path(args.out_dir)

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

    rows = _write_files(children, out_dir)
    written = sum(1 for r in rows if r["status"] == "WRITTEN")
    empty = sum(1 for r in rows if r["status"] == "EMPTY")

    if args.json:
        result: dict[str, Any] = {
            "parent": args.parent.upper(),
            "out_dir": str(out_dir),
            "written": written,
            "empty": empty,
            "files": rows,
        }
        print(json.dumps(result))
    else:
        _print_table(rows, written, empty, out_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
