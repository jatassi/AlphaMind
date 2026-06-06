"""Entrypoint for the isolated safety core + its watchdog (ALP-857 / ADR-0004).

Two NSSM services run off this one module:

* ``python -m alphamind.execution.continuous_monitor.safety_core run`` — the
  safety core itself: reads the broker snapshot + live price stream, evaluates
  the no-floor safety items, beats a file heartbeat, writes nothing to the DB.
* ``python -m alphamind.execution.continuous_monitor.safety_core watchdog`` —
  the dedicated out-of-process watchdog: probes the core's heartbeat file and
  restarts the core's NSSM service on staleness.

The watchdog is a *separate process* (separate NSSM service) precisely so it can
catch a freeze of the core's loop — a loop-resident watchdog cannot (ALP-841).

The composition body is exercised via ``python -m`` in production and marked
``# pragma: no cover``; ``_parse_args`` and the wired collaborators (the pure
core, the shell loop, the watchdog, the heartbeat, the NSSM controller) are unit
tested directly. The safety core imports neither pipeline nor monitor internals
beyond the shared price-stream transport (``underlying_stream``) and the broker
adapter — an ``.importlinter`` contract enforces the boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.broker_adapter import AccountStateQueries, AlpacaClientFactory
from alphamind.execution.continuous_monitor.logging_setup import configure_monitor_logging
from alphamind.execution.continuous_monitor.safety_core.evaluation import SafetyLimits
from alphamind.execution.continuous_monitor.safety_core.heartbeat import (
    FileHeartbeatProbe,
    FileHeartbeatSink,
)
from alphamind.execution.continuous_monitor.safety_core.loop import run_safety_core
from alphamind.execution.continuous_monitor.safety_core.process_control import (
    NssmServiceController,
)
from alphamind.execution.continuous_monitor.safety_core.watchdog import (
    run_watchdog,
    supervised_watchdog_loop,
)
from alphamind.execution.continuous_monitor.session import MonitorMode, new_session
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
)
from alphamind.execution.continuous_monitor.underlying_stream.task import (
    DefaultAlpacaStreamFactory,
    StockDataStreamProtocol,
    _quote_to_underlying,
)

log = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parents[5] / "config"
_MONITOR_CONFIG_PATH = _CONFIG_DIR / "continuous_monitor.yaml"
_VENUE_CONFIG_PATH = _CONFIG_DIR / "venue.yaml"

# The safety-core heartbeat file the watchdog probes. A file under the AlphaMind
# log directory (not the DB) so it crosses the process boundary without a DB
# write (ADR-0004).
_HEARTBEAT_FILENAME = "safety_core.heartbeat"

# The NSSM service name the watchdog restarts. Mirrors the install script.
_SAFETY_CORE_SERVICE_NAME = "alphamind-safety-core"

# Per-process rotating-log filenames (ALP-868). The safety core, its watchdog,
# and the continuous monitor are three separate processes; on Windows
# ``TimedRotatingFileHandler`` cannot rotate a file held open by another process
# (``WinError 32``), so each gets its own file. These parallel the per-service
# NSSM stdout/stderr naming in ``install_safety_core_service.ps1`` (which targets
# ``safety_core.out.log`` / ``safety_core.err.log``) — distinct files, same prefix.
_SAFETY_CORE_LOG_FILENAME = "safety_core.log"
_WATCHDOG_LOG_FILENAME = "safety_core_watchdog.log"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m alphamind.execution.continuous_monitor.safety_core",
        description="AlphaMind isolated safety core + out-of-process watchdog.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    run_p = sub.add_parser("run", help="Start the isolated safety core (blocks).")
    run_p.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode for the broker snapshot reads (default: paper).",
    )

    sub.add_parser(
        "watchdog",
        help="Start the out-of-process watchdog that restarts the wedged safety core.",
    )

    return parser.parse_args(argv)


def _log_filename_for_subcommand(subcommand: str) -> str:
    """Pick the rotating-log filename for the running process (ALP-868).

    ``run`` and ``watchdog`` are distinct NSSM services (distinct processes), and
    neither may share the monitor's ``monitor.log`` — see the module constants.
    """
    if subcommand == "watchdog":
        return _WATCHDOG_LOG_FILENAME
    return _SAFETY_CORE_LOG_FILENAME


def _heartbeat_path() -> Path:  # pragma: no cover - filesystem path resolution
    base = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(base) / "AlphaMind" / "logs" / _HEARTBEAT_FILENAME


def _load_safety_limits() -> SafetyLimits:  # pragma: no cover - exercised via python -m
    """Source the two safety limits from the resolved guardrails rule values.

    Composes directly off the ``alphamind.config`` resolver — the safety core
    must NOT import the monitor-internal breach-loop substrate (the
    ``.importlinter`` ``safety-core-isolation`` contract forbids it). The monitor
    does not drive regime transitions; compose against the conservative
    ``normal`` defaults at startup (an operator restart re-reads amended config).
    """
    from alphamind.config.load import parse_loaded_config
    from alphamind.config.models.modes import Mode
    from alphamind.config.models.regimes import Regime
    from alphamind.config.models.run_types import RunType
    from alphamind.config.resolver import RuntimeDimensions, compose_config

    loaded = parse_loaded_config(_CONFIG_DIR)
    runtime = RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.market_hours_rolling,
    )
    resolved = compose_config(loaded, runtime)
    rule_values = resolved.rule_values
    return SafetyLimits(
        max_gross_exposure_pct=float(rule_values["gross_exposure_pct"]),
        max_position_concentration_pct=float(rule_values["position_max_size_pct"]),
    )


async def _run_safety_core_daemon(*, mode: MonitorMode) -> None:  # pragma: no cover - python -m
    """Wire the safety core's ports and run its loop alongside the price feed.

    Both the breach math (off broker market values) and the price-staleness
    guard key on the BROKER snapshot — the Broker-Owned Fact — never the DB
    projection. The price feed subscribes to the broker snapshot's equity
    symbols; no projection read occurs anywhere in this process.
    """
    monitor_config = ContinuousMonitorConfig.model_validate(read_yaml_file(_MONITOR_CONFIG_PATH))
    venue_config = VenueConfig.model_validate(read_yaml_file(_VENUE_CONFIG_PATH))
    client_factory = AlpacaClientFactory(venue_config, mode)
    queries = AccountStateQueries(client_factory.build_trading_client())
    cache = UnderlyingPriceCache()
    session = new_session(mode=mode)
    heartbeat = FileHeartbeatSink(path=_heartbeat_path())
    limits = _load_safety_limits()
    cadence = float(monitor_config.breach_evaluation_cadence_seconds)

    log.info("safety core starting: session=%s mode=%s", session.session_id, mode)
    async with asyncio.TaskGroup() as tg:
        tg.create_task(
            _run_underlying_feed(
                queries=queries,
                cache=cache,
                mode=mode,
                refresh_seconds=float(monitor_config.subscription_refresh_seconds),
            ),
            name="safety_core:underlying_feed",
        )
        tg.create_task(
            run_safety_core(
                get_positions=queries.get_positions,
                get_account=queries.get_account,
                price_cache=cache,
                heartbeat=heartbeat,
                limits=limits,
                max_age_seconds=monitor_config.underlying_price_max_age_seconds,
                loop=lambda: _cadence_loop(cadence),
                now=lambda: datetime.now(UTC),
            ),
            name="safety_core:safety_loop",
        )


async def _run_underlying_feed(  # pragma: no cover - exercised via python -m
    *,
    queries: AccountStateQueries,
    cache: UnderlyingPriceCache,
    mode: MonitorMode,
    refresh_seconds: float,
) -> None:
    """Subscribe the IEX quote stream to the broker snapshot's equity symbols.

    Drains quotes into the shared cache the safety loop reads. The subscription
    target set is computed from the BROKER snapshot (``get_positions``), not the
    DB projection. A crash propagates so the TaskGroup tears down the process and
    NSSM restarts it (fail-safe: positions stay broker-protected meanwhile).
    """

    def _equity_symbols() -> frozenset[str]:
        return frozenset(
            snap.symbol for snap in queries.get_positions() if snap.asset_class == "us_equity"
        )

    async def _handler(payload: Any) -> None:
        await cache.update(_quote_to_underlying(payload))

    stream: StockDataStreamProtocol = DefaultAlpacaStreamFactory().build(mode=mode)
    subscribed = _equity_symbols()
    if subscribed:
        stream.subscribe_quotes(_handler, *sorted(subscribed))

    async def _refresh() -> None:
        nonlocal subscribed
        while True:
            await asyncio.sleep(refresh_seconds)
            target = _equity_symbols()
            added = target - subscribed
            removed = subscribed - target
            if added:
                stream.subscribe_quotes(_handler, *sorted(added))
            if removed:
                stream.unsubscribe_quotes(*sorted(removed))
            subscribed = target

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(stream._run_forever(), name="safety_core:stream_run_forever")
            tg.create_task(_refresh(), name="safety_core:stream_refresh")
    finally:
        with contextlib.suppress(Exception):
            await stream.stop_ws()


async def _run_watchdog_daemon() -> None:  # pragma: no cover - exercised via python -m
    """Wire the out-of-process watchdog: probe the heartbeat, restart on staleness."""
    monitor_config = ContinuousMonitorConfig.model_validate(read_yaml_file(_MONITOR_CONFIG_PATH))
    cadence = float(monitor_config.breach_evaluation_cadence_seconds)
    stall_bound = cadence * monitor_config.watchdog_cadence_multiplier
    probe = FileHeartbeatProbe(path=_heartbeat_path())
    controller = NssmServiceController(service_name=_SAFETY_CORE_SERVICE_NAME)

    log.info(
        "safety-core watchdog starting: stall_bound=%.0fs (cadence=%.0fs x %.1f)",
        stall_bound,
        cadence,
        monitor_config.watchdog_cadence_multiplier,
    )
    await run_watchdog(
        probe=probe,
        controller=controller,
        stall_bound_seconds=stall_bound,
        loop=supervised_watchdog_loop(cadence),
    )


async def _cadence_loop(  # pragma: no cover - python -m
    cadence_seconds: float,
) -> AsyncIterator[None]:
    """One safety tick per cadence: yield, then sleep ``cadence_seconds``."""
    while True:
        yield
        await asyncio.sleep(cadence_seconds)


def main(argv: Sequence[str] | None = None) -> None:  # pragma: no cover - python -m
    args = _parse_args(argv)
    load_dotenv()
    configure_monitor_logging(filename=_log_filename_for_subcommand(args.subcommand))

    if args.subcommand == "run":
        asyncio.run(_run_safety_core_daemon(mode=cast(MonitorMode, args.mode)))
    elif args.subcommand == "watchdog":
        asyncio.run(_run_watchdog_daemon())
    else:
        msg = f"unknown subcommand: {args.subcommand!r}"
        raise RuntimeError(msg)


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m``
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:
        logging.getLogger("alphamind").exception("safety core terminated abnormally")
        raise
