"""
Entry point for ``python -m alphamind.collector``.

Subcommands
-----------
run        Long-running APScheduler process (NSSM service target).
bootstrap  One-time historical backfill via bootstrap.run_all().
catch-up   One-shot sweep: calls every collector with ``since=None``.
"""

from __future__ import annotations

import argparse

from alphamind.collector.bootstrap import run_all as bootstrap_run_all
from alphamind.collector.catchup import run_all as catchup_run_all
from alphamind.collector.scheduler import start_blocking


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m alphamind.collector",
        description="AlphaMind collector — schedule, bootstrap, or catch-up.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("run", help="Start the long-running scheduler (blocks).")

    bootstrap_p = sub.add_parser("bootstrap", help="One-time historical backfill.")
    bootstrap_p.add_argument(
        "--only",
        metavar="VENDOR",
        default=None,
        help="Limit bootstrap to a single vendor.",
    )

    sub.add_parser(
        "catch-up",
        help="One-shot sweep: call every collector with since=None, then exit.",
    )

    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.subcommand == "run":
        start_blocking()
    elif args.subcommand == "bootstrap":
        bootstrap_run_all(only_vendor=args.only)
    elif args.subcommand == "catch-up":
        catchup_run_all()


if __name__ == "__main__":
    main()
