"""Entry point for ``python -m alphamind.execution.continuous_monitor`` (story 01).

The continuous monitor is a parallel NSSM service to the collector and the
pipeline scheduler. This story ships the empty-registry shell so the operator
can install the service and verify start / clean shutdown before later stories
register the fill-stream / underlying-stream / breach / greeks-refresh /
cascade-dispatcher / emergency-trigger / bracket-stop tasks.

Subcommand layout:

    python -m alphamind.execution.continuous_monitor run [--mode {paper,live}]

Per parent issue ALP-123 § Pre-resolved decision (A), no ``bootstrap`` or
``catch-up`` subcommands — the monitor is forward-only.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.logging_setup import (
    configure_monitor_logging,
)
from alphamind.execution.continuous_monitor.session import (
    MonitorMode,
    new_session,
)
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor

# ``python -m alphamind.execution.continuous_monitor`` sets ``__name__`` to
# ``__main__`` (outside the alphamind hierarchy) so messages would not reach
# the file handler attached to the ``alphamind`` root logger. Name the logger
# under the package explicitly so ``configure_monitor_logging`` captures it.
log = logging.getLogger("alphamind.execution.continuous_monitor")

_CONFIG_PATH = Path(__file__).parents[4] / "config" / "continuous_monitor.yaml"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m alphamind.execution.continuous_monitor",
        description="AlphaMind continuous monitor — long-running runner process.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    run_p = sub.add_parser("run", help="Start the long-running monitor (blocks).")
    run_p.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode for the session (default: paper).",
    )

    return parser.parse_args(argv)


async def _run_daemon(*, mode: MonitorMode) -> None:
    """Daemon path — load config, build supervisor, run until shutdown.

    The registry is empty in story 01; later stories register their tasks here.
    """
    configure_monitor_logging()
    config = ContinuousMonitorConfig.model_validate(read_yaml_file(_CONFIG_PATH))
    session = new_session(mode=mode)
    log.info(
        "monitor session start: session_id=%s mode=%s",
        session.session_id,
        session.mode,
    )
    supervisor = MonitorSupervisor(session=session, config=config)
    try:
        await supervisor.run()
    finally:
        log.info("monitor session end: session_id=%s", session.session_id)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    load_dotenv()

    if args.subcommand != "run":
        # ``required=True`` on the subparser makes this unreachable; defensive
        # so a future subcommand addition does not silently fall through.
        msg = f"unknown subcommand: {args.subcommand!r}"
        raise RuntimeError(msg)

    asyncio.run(_run_daemon(mode=cast(MonitorMode, args.mode)))


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m``
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:
        log.exception("continuous monitor exited with error")
        sys.exit(1)
