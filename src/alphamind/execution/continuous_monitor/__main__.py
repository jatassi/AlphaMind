"""Entry point for ``python -m alphamind.execution.continuous_monitor`` (story 01).

The continuous monitor is a parallel NSSM service to the collector and the
pipeline scheduler. Story 01 (ALP-432) shipped the supervisor + session +
logging + config; subsequent stories register their long-running tasks:

* 02c (ALP-435) — ``fill_stream_consumer``: drains alpaca-py ``trade_updates``
  and writes each fill to ``fill_records`` via :func:`append_fill_record`.

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
from typing import TYPE_CHECKING, cast

from dotenv import load_dotenv

from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.broker_adapter import AccountStateQueries, AlpacaClientFactory
from alphamind.execution.continuous_monitor.logging_setup import (
    configure_monitor_logging,
)
from alphamind.execution.continuous_monitor.session import (
    MonitorMode,
    MonitorSession,
    new_session,
)
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.persistence.session import make_async_engine, make_async_session_factory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ``python -m alphamind.execution.continuous_monitor`` sets ``__name__`` to
# ``__main__`` (outside the alphamind hierarchy) so messages would not reach
# the file handler attached to the ``alphamind`` root logger. Name the logger
# under the package explicitly so ``configure_monitor_logging`` captures it.
log = logging.getLogger("alphamind.execution.continuous_monitor")

_CONFIG_DIR = Path(__file__).parents[4] / "config"
_CONFIG_PATH = _CONFIG_DIR / "continuous_monitor.yaml"
_VENUE_CONFIG_PATH = _CONFIG_DIR / "venue.yaml"


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
    """Daemon path — load config, build supervisor, register tasks, run.

    Story 02c (ALP-435) registers the fill-stream consumer; subsequent
    stories register their tasks here in the same shape (each task wraps
    its run-forever coroutine in a closure that pre-binds the factories the
    supervisor's uniform ``(session, config)`` task signature can't carry).
    """
    configure_monitor_logging()
    config = ContinuousMonitorConfig.model_validate(read_yaml_file(_CONFIG_PATH))
    venue_config = VenueConfig.model_validate(read_yaml_file(_VENUE_CONFIG_PATH))
    session = new_session(mode=mode)
    log.info(
        "monitor session start: session_id=%s mode=%s",
        session.session_id,
        session.mode,
    )

    engine = make_async_engine()
    db_session_factory = make_async_session_factory(engine)
    supervisor = MonitorSupervisor(session=session, config=config)
    _register_fill_stream_consumer(
        supervisor,
        venue_config=venue_config,
        db_session_factory=db_session_factory,
    )

    try:
        await supervisor.run()
    finally:
        await engine.dispose()
        log.info("monitor session end: session_id=%s", session.session_id)


def _register_fill_stream_consumer(
    supervisor: MonitorSupervisor,
    *,
    venue_config: VenueConfig,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Register the ``fill_stream_consumer`` task (story 02c).

    The supervisor's task signature is ``(session, config) -> Awaitable[None]``
    — extras flow through this closure, which pre-binds the alpaca-py factories
    and the SQLAlchemy session factory.
    """
    from alpaca.trading.client import TradingClient

    from alphamind.execution.continuous_monitor.fill_stream_consumer import (
        run_fill_stream_consumer,
    )

    def _stream_factory(monitor_mode: MonitorMode) -> object:
        return AlpacaClientFactory(venue_config, monitor_mode).build_trading_stream()

    def _trading_client_factory(monitor_mode: MonitorMode) -> TradingClient:
        return AlpacaClientFactory(venue_config, monitor_mode).build_trading_client()

    def _queries_factory(client: object) -> AccountStateQueries:
        return AccountStateQueries(cast(TradingClient, client))

    async def _fill_stream_consumer_task(s: MonitorSession, c: ContinuousMonitorConfig) -> None:
        await run_fill_stream_consumer(
            s,
            c,
            session_factory=db_session_factory,
            stream_factory=_stream_factory,
            trading_client_factory=_trading_client_factory,
            account_state_queries_factory=_queries_factory,
        )

    supervisor.register_task(name="fill_stream_consumer", coro_fn=_fill_stream_consumer_task)


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
