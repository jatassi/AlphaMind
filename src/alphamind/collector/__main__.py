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
import logging
import os
import sys

from dotenv import load_dotenv

from alphamind.collector.bootstrap import run_all as bootstrap_run_all
from alphamind.collector.catchup import run_all as catchup_run_all
from alphamind.collector.scheduler import start_blocking


def _setup_stdout_logging() -> None:
    """Configure root logger to emit INFO+ to stdout.

    The ``run`` subcommand additionally attaches a TimedRotatingFileHandler
    in scheduler.start_blocking; this stdout handler is always on so that
    ``catch-up`` and ``bootstrap`` produce visible progress.
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)


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

    catchup_p = sub.add_parser(
        "catch-up",
        help="One-shot sweep: call every collector with since=None, then exit.",
    )
    catchup_p.add_argument(
        "--only",
        metavar="IDS",
        default=None,
        help="Comma-separated list of collector IDs or vendor prefixes to run.",
    )
    catchup_p.add_argument(
        "--skip",
        metavar="IDS",
        default=None,
        help="Comma-separated list of collector IDs or vendor prefixes to skip.",
    )

    return parser.parse_args()


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [p.strip() for p in value.split(",") if p.strip()]


def main() -> None:
    args = _parse_args()
    _setup_stdout_logging()
    load_dotenv()  # populate API keys from project-root .env before any subcommand runs

    if args.subcommand == "run":
        start_blocking()
    elif args.subcommand == "bootstrap":
        bootstrap_run_all(only_vendor=args.only)
    elif args.subcommand == "catch-up":
        catchup_run_all(only=_split_csv(args.only), skip=_split_csv(args.skip))


if __name__ == "__main__":
    main()
