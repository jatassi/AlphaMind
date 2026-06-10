"""Entry point for ``python -m alphamind.execution.continuous_monitor`` (story 01).

The continuous monitor is a parallel NSSM service to the collector and the
pipeline scheduler. Story 01 (ALP-432) shipped the supervisor + session +
logging + config; subsequent stories register their long-running tasks:

* 02b (ALP-434) — ``underlying_stream``: live Alpaca StockDataStream / IEX
  feed feeding the shared :class:`UnderlyingPriceCache`.
* 02c (ALP-435) — ``fill_stream_consumer``: drains alpaca-py ``trade_updates``
  and writes each fill to ``fill_records`` via :func:`append_fill_record`.
* 03a (ALP-436) — ``greeks_refresh``: per-position greeks refresh against
  the collector-populated ``options_contract_snapshots`` table on a
  scheduled + move-triggered cadence.

Subcommand layout:

    python -m alphamind.execution.continuous_monitor run [--mode {paper,live}]
    python -m alphamind.execution.continuous_monitor watchdog

``watchdog`` (ALP-941) is the monitor's dedicated out-of-process watchdog — a
separate NSSM service that probes the monitor's file heartbeat
(``monitor.heartbeat``) and runs ``nssm restart alphamind-monitor`` when it
goes stale. The in-process stall watchdog is structurally blind to a freeze of
its own event loop (its ``asyncio.sleep`` never resumes, so it can never reach
``os._exit``); the external watchdog bounds that wedge regardless of cause,
mirroring the safety core's ADR-0004 topology.

Per parent issue ALP-123 § Pre-resolved decision (A), no ``bootstrap`` or
``catch-up`` subcommands — the monitor is forward-only.
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, assert_never, cast

from dotenv import load_dotenv

from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.execution import ExecutionConfig, PaperHarness
from alphamind.config.models.venue import VenueConfig
from alphamind.distillation.realized_vol import read_realized_vol_map
from alphamind.execution.broker_adapter import AccountStateQueries, AlpacaClientFactory
from alphamind.execution.continuous_monitor.activities_backfill import (
    register_fill_backfill_task,
)
from alphamind.execution.continuous_monitor.bracket_stops import (
    AlpacaBracketCloseSubmitter,
    register_options_bracket_watcher_task,
)
from alphamind.execution.continuous_monitor.cascade_dispatch import TriggerIdGenerator
from alphamind.execution.continuous_monitor.control.app import (
    ControlSurfaceDependencies,
    make_control_surface_task,
)
from alphamind.execution.continuous_monitor.control.events import SSEEventEmitter
from alphamind.execution.continuous_monitor.control.halt_mode_repo import (
    HaltModeRepository,
)
from alphamind.execution.continuous_monitor.control.verbs import (
    BrokerErrorCancel,
    BrokerErrorClose,
    OrderState,
    PositionState,
)
from alphamind.execution.continuous_monitor.control.wiring import (
    wrap_fill_enrichment_with_emit,
)
from alphamind.execution.continuous_monitor.entry_window import (
    register_entry_window_watcher_task,
)
from alphamind.execution.continuous_monitor.faulthandler_deadman import (
    register_faulthandler_deadman_task,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer import EnrichmentCallable
from alphamind.execution.continuous_monitor.greeks_refresh import (
    register_greeks_refresh_task,
)
from alphamind.execution.continuous_monitor.logging_setup import (
    configure_monitor_logging,
    log_directory,
)
from alphamind.execution.continuous_monitor.session import (
    MonitorMode,
    MonitorSession,
    new_session,
)
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.execution.continuous_monitor.underlying_stream import (
    register_underlying_stream_task,
)
from alphamind.execution.continuous_monitor.underlying_stream.reader import (
    SqlOpenPositionsReader,
)
from alphamind.execution.paper_evaluation_harness import (
    attach_live_execution_estimate,
)
from alphamind.execution.paper_evaluation_harness.lookups import (
    MapVolLookup,
    SqlAdvLookup,
    SqlOrderLookup,
)
from alphamind.execution.process_supervision import (
    FileHeartbeatProbe,
    FileHeartbeatSink,
    HeartbeatProbe,
    NssmServiceController,
    ProcessController,
    WatchdogLoop,
    run_watchdog,
    supervised_watchdog_loop,
)
from alphamind.execution.venue_configuration.calendar_cache import (
    TradingCalendarCache,
)
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
)
from alphamind.risk_guardrails.guardrail_evaluation import RealizedVolEntry
from alphamind.state.process_lifetime import record_process_lifetime

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
_EXECUTION_CONFIG_PATH = _CONFIG_DIR / "execution.yaml"

# ALP-941 — the NSSM service name the out-of-process watchdog restarts.
# Mirrors install_monitor_service.ps1.
_MONITOR_SERVICE_NAME = "alphamind-monitor"

# The monitor heartbeat file the watchdog probes. Under the AlphaMind log
# directory, parallel to the safety core's safety_core.heartbeat.
_HEARTBEAT_FILENAME = "monitor.heartbeat"

# Per-process rotating-log filenames (ALP-868): on Windows
# ``TimedRotatingFileHandler`` cannot rotate a file held open by another
# process (``WinError 32``), so the monitor and its watchdog — two separate
# NSSM services — each get their own file (and neither shares the safety-core
# services' files).
_MONITOR_LOG_FILENAME = "monitor.log"
_WATCHDOG_LOG_FILENAME = "monitor_watchdog.log"

# Where the faulthandler deadman dumps the frozen main-thread stack (ALP-941
# scope E) — the next wedge self-captures its blocking frame here.
_FAULTHANDLER_LOG_FILENAME = "monitor_faulthandler.log"


def _log_filename_for_subcommand(subcommand: str) -> str:
    """Pick the rotating-log filename for the running process (ALP-868)."""
    if subcommand == "watchdog":
        return _WATCHDOG_LOG_FILENAME
    return _MONITOR_LOG_FILENAME


def _watchdog_stall_bound(config: ContinuousMonitorConfig) -> float:
    """The freeze bound shared by the external watchdog and the deadman (ALP-941).

    One formula for both consumers: the out-of-process watchdog restarts the
    monitor when the heartbeat is older than this, and the faulthandler deadman
    arms its dump timer to it — so the frozen stack is captured at the same
    age that triggers the restart which would destroy it.
    """
    return config.monitor_watchdog_tick_seconds * config.watchdog_cadence_multiplier


# Same default the scheduler uses (``alphamind.scheduler.__main__``) so the
# pip-freeze + process-lifetime snapshots land in the canonical archive root.
# Resolved lazily inside ``_run_daemon`` so tests that ``monkeypatch.setenv``
# ``HOME`` after import don't get the developer's real home directory.
def _default_archive_root() -> Path:
    return Path.home() / "AlphaMind" / "archive"


# ALP-530 — functional refresh cadence for the shared realized-vol map. The
# distillation producer runs at invocation cadence (typically daily /
# on-schedule); the monitor lives across invocations, so a 24h refresh keeps the
# long-lived process from holding a multi-day-stale Mapping.
_REALIZED_VOL_REFRESH_INTERVAL_SECONDS: float = 24 * 60 * 60

# ALP-825 review — short heartbeat cadence for the realized-vol refresh loop.
# The refresh fires only
# once per ``_REALIZED_VOL_REFRESH_INTERVAL_SECONDS`` (24h), but the supervised
# loop must iterate far more often than that so the stall watchdog sees a steady
# heartbeat — pacing the loop on the 24h functional interval would yield a stall
# bound of 24h * watchdog_cadence_multiplier (~240h), so a mid-refresh wedge
# would go undetected for ~10 days. The loop wakes on this short cadence, beats,
# and checks the 24h wall-clock-due condition inside the body. 60s is well under
# any plausible watchdog bound yet coarse enough that minute-granularity on the
# daily refresh is immaterial.
_REALIZED_VOL_HEARTBEAT_CADENCE_SECONDS: float = 60.0


class _EmptyMapAlertOnce:
    """One-shot guard for the empty-realized-vol-map ERROR alert.

    A successful refresh that leaves ``shared_map`` empty is operationally
    indistinguishable from a fresh-DB / never-ran-the-producer state — the
    monitor would happily run forever with the IV-fallback chain silently
    masking the gap. The guard emits a single ERROR log on the first
    successful refresh that leaves the map empty, then permanently disarms
    so repeated empty refreshes do not spam and a populated first refresh
    disables the alert entirely (the bootstrap window has closed).
    """

    def __init__(self) -> None:
        self._armed = True

    def observe(self, *, count: int) -> None:
        if not self._armed:
            return
        self._armed = False
        if count == 0:
            log.error(
                "realized_vol map is empty after successful refresh; "
                "the substrate landed but the producer has not run yet."
            )


async def refresh_realized_vol_map_in_place(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    shared_map: dict[str, RealizedVolEntry],
    tickers: Sequence[str] | None,
) -> int:
    """Refresh ``shared_map`` from ``ticker_realized_vol`` in place.

    Both the harness ``MapVolLookup`` and the breach-loop
    ``SqlOptionsIvProvider`` (ALP-642) hold the same dict reference;
    mutating ``shared_map`` in place updates both consumers without
    re-construction.

    ``tickers`` restricts the fetch to a known underlying set (typically the
    monitor's open-position underlyings); pass ``None`` to fetch every
    populated ticker. Tickers absent from the latest read are removed from
    ``shared_map`` — the consumers' fallback chain handles missing entries.

    Returns the number of entries in ``shared_map`` after the refresh.
    """
    async with session_factory() as session:
        latest = await read_realized_vol_map(session, tickers=tickers)
    shared_map.clear()
    for ticker, vol in latest.items():
        shared_map[ticker] = RealizedVolEntry(underlying=ticker, trailing_30d_realized_vol=vol)
    return len(shared_map)


def _register_realized_vol_refresh_task(
    supervisor: MonitorSupervisor,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    shared_map: dict[str, RealizedVolEntry],
    tickers_provider: Callable[[], Sequence[str] | None],
    empty_map_alert: _EmptyMapAlertOnce,
    interval_seconds: float = _REALIZED_VOL_REFRESH_INTERVAL_SECONDS,
    heartbeat_cadence_seconds: float = _REALIZED_VOL_HEARTBEAT_CADENCE_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Register the 24h shared realized-vol map refresher (ALP-530).

    The functional refresh fires once per ``interval_seconds`` (24h); each
    refresh re-fetches the per-underlying scalars and mutates ``shared_map``
    in place so the harness and breach-loop both observe the fresh values.
    ``tickers_provider`` is invoked per refresh so the monitor's open-position
    set can change over the day without re-registering.

    Watchdog liveness (ALP-825 review). The loop drives the supervisor's
    ``supervised_loop`` seam on a SHORT ``heartbeat_cadence_seconds`` cadence
    rather than the 24h functional interval, so
    the stall bound is ``heartbeat_cadence_seconds * watchdog_cadence_multiplier``
    instead of ~240h — a mid-refresh wedge is detected within minutes, not ~10
    days. Each iteration beats the watchdog (via the seam) and checks whether the
    24h refresh is due against ``now``; the refresh runs only when
    ``now() - last_refresh >= interval_seconds``, so the functional cadence is
    preserved.

    ``empty_map_alert`` is shared with the startup refresh so whichever
    refresh succeeds first fires the one-shot ERROR log if the map is empty.
    ``now`` is a monotonic clock (one of the four sanctioned mock boundaries)
    so the daily-due condition is testable without real waiting.
    """

    async def _refresh_task(_session: MonitorSession, _config: ContinuousMonitorConfig) -> None:
        # The startup refresh in ``_run_daemon`` already populated the map; the
        # first scheduled in-loop refresh is one ``interval_seconds`` later.
        last_refresh = now()
        async for _ in supervisor.supervised_loop(
            "realized_vol_refresh", heartbeat_cadence_seconds
        ):
            if now() - last_refresh < interval_seconds:
                continue
            last_refresh = now()
            try:
                tickers = tickers_provider()
                count = await refresh_realized_vol_map_in_place(
                    session_factory=session_factory,
                    shared_map=shared_map,
                    tickers=tickers,
                )
                log.info("realized_vol map refresh complete: entries=%d", count)
                empty_map_alert.observe(count=count)
            except Exception:
                # Per the LLM-agents-uniformly-Critical posture for monitor
                # tasks, a refresh failure must not crash the supervisor —
                # the stale map still satisfies the fallback chain. Log and
                # retry next cycle.
                log.exception("realized_vol map refresh failed")

    supervisor.register_task(name="realized_vol_refresh", coro_fn=_refresh_task)


def _build_enrichment_callable(
    *,
    mode: MonitorMode,
    paper_harness: PaperHarness,
    session_factory: async_sessionmaker[AsyncSession],
    realized_vol_map: Mapping[str, RealizedVolEntry],
) -> EnrichmentCallable | None:
    """Construct the paper-mode enrichment callable, or ``None`` for live mode.

    The wedge (ALP-528) attaches a ``LiveExecutionEstimate`` to each persisted
    ``FillRecord`` so paper-mode fills carry the estimated live-execution
    drag. Live mode bypasses the wedge entirely — the field stays NULL in
    ``fill_records.live_execution_estimate_json`` and the broker reports
    actual costs separately.

    Today's ``realized_vol_map`` is empty (the per-underlying realized-vol
    substrate lands with ALP-530); per parent decision H the wedge handles
    the empty-map case gracefully by routing every fill through
    ``live_execution_estimate=None``.
    """
    if mode == "live":
        return None
    if mode == "paper":
        order_lookup = SqlOrderLookup(session_factory)
        adv_lookup = SqlAdvLookup(session_factory)
        vol_lookup = MapVolLookup(realized_vol_map)
        return partial(
            attach_live_execution_estimate,
            order_lookup=order_lookup,
            adv_lookup=adv_lookup,
            vol_lookup=vol_lookup,
            config=paper_harness,
        )
    assert_never(mode)


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

    sub.add_parser(
        "watchdog",
        help="Start the out-of-process watchdog that restarts the wedged monitor.",
    )

    return parser.parse_args(argv)


async def _run_daemon(*, mode: MonitorMode) -> None:
    """Daemon path — load config, build supervisor, register tasks, run.

    Stories 02b / 02c register their tasks on the supervisor below. Each
    task wraps its run-forever coroutine in a closure that pre-binds the
    factories the supervisor's uniform ``(session, config)`` task signature
    can't carry. The async engine + session factory share the monitor
    process's lifetime so cross-invocation reads (e.g. underlying-stream
    subscription targets) have a stable handle.
    """
    config = ContinuousMonitorConfig.model_validate(read_yaml_file(_CONFIG_PATH))
    venue_config = VenueConfig.model_validate(read_yaml_file(_VENUE_CONFIG_PATH))
    execution_config = ExecutionConfig.model_validate(read_yaml_file(_EXECUTION_CONFIG_PATH))
    session = new_session(mode=mode)
    log.info(
        "monitor session start: session_id=%s mode=%s",
        session.session_id,
        session.mode,
    )
    engine = make_async_engine()
    db_session_factory = make_async_session_factory(engine)
    # ALP-857 / W4b — the sync engine pair that fed the breach loop's regime
    # provider (``resolve_regime_adaptation`` via ``asyncio.to_thread``) is gone
    # with breach detection; the safety core is a separate process and the
    # monitor proper's surviving tasks are async-only.
    # ALP-719 — the borrow-accrual tick inserts its own ``InvocationRow`` per
    # tick, FK-referencing a ``process_lifetimes`` row owned by this monitor
    # process. Mirrors the scheduler's startup record_process_lifetime call.
    archive_root = _default_archive_root()
    archive_root.mkdir(parents=True, exist_ok=True)
    process_lifetime_id = await record_process_lifetime(
        session_factory=db_session_factory,
        process_role="monitor",
        archive_root=archive_root,
    )
    open_positions_reader = SqlOpenPositionsReader(db_session_factory)
    # One ``AccountStateQueries`` + one ``TradingCalendarCache`` are shared
    # across the breach loop (market-hours predicate, margin-call observer),
    # the greeks-refresh task (market-hours predicate), and the bracket-stops
    # watcher's submitter — so the monitor process holds a single live
    # broker-client reference and a single calendar window in memory.
    client_factory = AlpacaClientFactory(venue_config, mode)
    account_state_queries = AccountStateQueries(client_factory.build_trading_client())
    calendar_cache = TradingCalendarCache(account_state_queries)
    # ALP-941 — the supervisor's watchdog loop beats this file each time its
    # sleep resumes; the out-of-process alphamind-monitor-watchdog service
    # probes it and restarts the monitor when a frozen event loop lets it go
    # stale (the wedge the in-process watchdog structurally cannot catch).
    supervisor = MonitorSupervisor(
        session=session,
        config=config,
        heartbeat=FileHeartbeatSink(path=log_directory() / _HEARTBEAT_FILENAME),
    )
    # ALP-720 — shared SSE event emitter for the /events stream + the
    # production wiring adapters that observe each breach / fill /
    # emergency callsite.
    sse_emitter = SSEEventEmitter()
    underlying_cache = register_underlying_stream_task(
        supervisor,
        repository=open_positions_reader,
        is_market_open=calendar_cache.is_market_open,
    )
    # ALP-528/530/642 — one shared realized-vol dict feeds both the paper-mode
    # enrichment wedge (via MapVolLookup) and the breach-loop's
    # SqlOptionsIvProvider (as its fallback channel). Pre-populate at startup
    # so consumers see real values from the first invocation rather than an
    # empty fallback path, and register a 24h refresh task so a multi-day
    # monitor session does not drift on stale realized vol.
    realized_vol_map: dict[str, RealizedVolEntry] = {}

    def _open_position_underlyings() -> Sequence[str] | None:
        # ``None`` fetches every populated ticker; the open-position set is
        # determined lazily so monitor-time position changes propagate
        # without re-registering the refresh task. Returning ``None`` is
        # cheap and lets the fallback chain handle the bounded result set.
        return None

    empty_map_alert = _EmptyMapAlertOnce()
    try:
        count = await refresh_realized_vol_map_in_place(
            session_factory=db_session_factory,
            shared_map=realized_vol_map,
            tickers=_open_position_underlyings(),
        )
    except Exception as exc:
        # Startup refresh failure (bootstrap path / missing table / transient
        # DB outage) must not block the monitor from coming up — the shared
        # map stays empty and the IV-fallback chain handles missing entries
        # exactly as it did pre-ALP-530. The refresh task continues to retry
        # every 24h.
        log.warning(
            "realized_vol map startup refresh failed (%s); proceeding with "
            "empty map. The 24h refresher will retry.",
            exc,
        )
    else:
        empty_map_alert.observe(count=count)
    _register_realized_vol_refresh_task(
        supervisor,
        session_factory=db_session_factory,
        shared_map=realized_vol_map,
        tickers_provider=_open_position_underlyings,
        empty_map_alert=empty_map_alert,
    )
    enrichment_callable = _build_enrichment_callable(
        mode=session.mode,
        paper_harness=execution_config.paper_harness,
        session_factory=db_session_factory,
        realized_vol_map=realized_vol_map,
    )
    # ALP-720 — wrap enrichment with the SSE fill_received emit. The wrapper
    # passes the FillRecord through to the inner enrichment (or returns it
    # unchanged in live mode where ``inner`` is None) so persistence stays
    # unchanged; emit failures log and continue.
    sse_wrapped_enrichment = wrap_fill_enrichment_with_emit(
        emitter=sse_emitter, inner=enrichment_callable
    )
    _register_fill_stream_consumer(
        supervisor,
        venue_config=venue_config,
        db_session_factory=db_session_factory,
        enrichment_callable=sse_wrapped_enrichment,
        is_market_open=calendar_cache.is_market_open,
        process_lifetime_id=process_lifetime_id,
    )
    # ALP-763 — periodic fill-backfill backstop. Sweeps Alpaca on an interval
    # (no websocket disconnect needed) with an independent generous lookback
    # bound to recover any fill missing from fill_records — closing the gap the
    # reconnect-driven recovery leaves once a later fill advances the
    # max(fill_timestamp) bound past an earlier dropped one. Reuses the same
    # broker factories + enrichment wedge as the fill consumer.
    register_fill_backfill_task(
        supervisor,
        venue_config=venue_config,
        db_session_factory=db_session_factory,
        enrichment_callable=sse_wrapped_enrichment,
        process_lifetime_id=process_lifetime_id,
    )
    register_greeks_refresh_task(
        supervisor,
        repository=open_positions_reader,
        cache=underlying_cache,
        session_factory=db_session_factory,
        market_open=calendar_cache.is_market_open,
    )
    # ALP-855 / W4a — borrow accrual is accounting; it was evicted from the
    # always-on monitor (ADR-0004) into the pipeline's fill-collection write unit (single
    # writer = pipeline, ADR-0005). No monitor registration here.
    #
    # ALP-857 / W4b — breach detection + price-staleness (the lone safety item
    # with no broker floor) is ISOLATED into its own out-of-process safety core
    # (``alphamind.execution.continuous_monitor.safety_core``), supervised by a
    # dedicated out-of-process watchdog. The monitor proper no longer registers a
    # ``breach_loop`` task: it runs only precision/data tasks (options stops,
    # greeks, entry-window, fill stream + recovery sweep) and is fail-safe under
    # the broker floor (ADR-0004). The safety core reads the broker snapshot, not
    # the DB projection, and writes nothing.
    #
    # ``trigger_ids`` is constructed once per monitor session so the
    # bracket-stops watcher mints trigger ids from one monotonic sequence —
    # encoding ``MON.{session}.{trigger}.0`` into the engine-originated
    # ``client_order_id`` (a collision would land two broker submissions with
    # identical IDs).
    trigger_ids = TriggerIdGenerator(session_id=session.session_id)
    register_options_bracket_watcher_task(
        supervisor,
        position_repository=open_positions_reader,
        cache=underlying_cache,
        session_factory=db_session_factory,
        submitter=AlpacaBracketCloseSubmitter(
            client_factory=client_factory,
            execution_config=execution_config,
        ),
        trigger_ids=trigger_ids,
    )
    # ALP-737 — auto-cancel PENDING_ENTRY brackets past their entry_window_deadline.
    register_entry_window_watcher_task(
        supervisor,
        session_factory=db_session_factory,
        client_factory=client_factory,
        execution_config=execution_config,
    )

    # ALP-720 — control surface task: /control/* verbs + /events SSE.
    # ``set_halt_mode`` is wired against the real :class:`HaltModeRepository`;
    # ``cancel_order`` / ``force_close_position`` use stub adapters that
    # return ``not_implemented`` error envelopes until the broker-dispatch
    # path is wired in a follow-up (the verbs' Protocols are stable; only
    # the production seam is deferred).
    halt_mode_repo = HaltModeRepository(session_factory=db_session_factory)
    control_deps = ControlSurfaceDependencies(
        order_lookup=_NotImplementedOrderLookup(),
        position_lookup=_NotImplementedPositionLookup(),
        cancel_emitter=_NotImplementedCancelEmitter(),
        close_submitter=_NotImplementedCloseSubmitter(),
        halt_mode_repo=halt_mode_repo,
        event_emitter=sse_emitter,
    )
    # ALP-826 — the control surface blocks in ``await server.serve()`` with no
    # natural per-iteration heartbeat, so it is the one task registered
    # ``watched=False`` (opted out of the stall watchdog: never warned-about,
    # never tripped). Port-level liveness for it is a noted follow-up.
    supervisor.register_task(
        name="control_surface",
        coro_fn=make_control_surface_task(
            deps=control_deps,
            port=config.control_port,
        ),
        watched=False,
    )

    try:
        await _run_with_faulthandler_deadman(supervisor, config)
    finally:
        await engine.dispose()
        log.info("monitor session end: session_id=%s", session.session_id)


async def _run_with_faulthandler_deadman(
    supervisor: MonitorSupervisor, config: ContinuousMonitorConfig
) -> None:
    """Arm the faulthandler deadman around ``supervisor.run()`` (ALP-941 scope E).

    While the event loop turns, the deadman task re-arms faulthandler's
    C-thread dump timer before it can expire; a loop freeze stops the re-arm
    and the timer dumps the frozen main-thread stack (the exact blocking frame)
    to ``monitor_faulthandler.log`` before the external watchdog's restart
    destroys the evidence.
    """
    faulthandler.enable()
    fault_log_path = log_directory() / _FAULTHANDLER_LOG_FILENAME
    fault_log_path.parent.mkdir(parents=True, exist_ok=True)
    deadman_bound = _watchdog_stall_bound(config)
    with fault_log_path.open("a", encoding="utf-8") as fault_file:
        register_faulthandler_deadman_task(
            supervisor,
            tick_seconds=config.monitor_watchdog_tick_seconds,
            arm=lambda: faulthandler.dump_traceback_later(
                deadman_bound, repeat=False, file=fault_file
            ),
            cancel=faulthandler.cancel_dump_traceback_later,
        )
        try:
            await supervisor.run()
        finally:
            # faulthandler holds the file's fd until the dump fires or is
            # cancelled — cancel before the ``with`` closes the file.
            faulthandler.cancel_dump_traceback_later()


# ---------------------------------------------------------------------------
# Stub verb dependencies (ALP-720 follow-up).
#
# The cancel_order + force_close_position verbs require live broker-dispatch
# wiring (the OmsCloseSubmitter needs the breach-loop's ``submit_envelope``
# closure; the cancel-emitter needs a real broker adapter). Both are out of
# scope for the binding-fix PR but the control surface must construct
# *some* implementation of every Protocol. These stubs return the error
# envelopes documented in the schema's per-verb error table.
# ---------------------------------------------------------------------------


class _NotImplementedOrderLookup:
    """Returns ``None`` for every order_id → verb sees ``not_found``."""

    async def fetch(self, order_id: str) -> OrderState | None:
        del order_id
        return None


class _NotImplementedPositionLookup:
    """Returns ``None`` for every position_id → verb sees ``not_found``."""

    async def fetch(self, position_id: str) -> PositionState | None:
        del position_id
        return None


class _NotImplementedCancelEmitter:
    """Cancel verb wiring deferred; returns a broker-error envelope."""

    async def submit_cancel(self, *, order_id: str) -> BrokerErrorCancel:
        del order_id
        return BrokerErrorCancel(
            broker_message=(
                "cancel_order verb not yet wired to the broker adapter; see ALP-720 follow-up"
            )
        )


class _NotImplementedCloseSubmitter:
    """Force-close verb wiring deferred; returns a broker-error envelope."""

    async def submit_close(
        self,
        *,
        position_id: str,
        position_selection_rationale: str,
        rule_breached: str,
        breach_details_current: float,
        breach_details_limit: float,
    ) -> BrokerErrorClose:
        del (
            position_id,
            position_selection_rationale,
            rule_breached,
            breach_details_current,
            breach_details_limit,
        )
        return BrokerErrorClose(
            broker_message=(
                "force_close_position verb not yet wired to the OMS submit path; "
                "see ALP-720 follow-up"
            )
        )


def _register_fill_stream_consumer(
    supervisor: MonitorSupervisor,
    *,
    venue_config: VenueConfig,
    db_session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable | None,
    is_market_open: Callable[[datetime], bool],
    process_lifetime_id: str | None = None,
) -> None:
    """Register the ``fill_stream_consumer`` task (story 02c).

    The supervisor's task signature is ``(session, config) -> Awaitable[None]``
    — extras flow through this closure, which pre-binds the alpaca-py factories
    and the SQLAlchemy session factory.

    Paper-mode wiring (ALP-528) passes a non-None ``enrichment_callable`` that
    enriches each translated ``FillRecord`` with a ``LiveExecutionEstimate``
    before persistence. Live mode passes ``None`` so the hot path is
    unchanged.

    ``is_market_open`` (ALP-819) gates the connected-but-silent staleness check
    to RTH (fills are legitimately sparse off-hours), and ``supervisor.beat``
    feeds the stall watchdog the fill-flow heartbeat so a starved consumer
    trips ``os._exit(1)`` → NSSM restart.

    The reconnect-driven loop is not a fixed-cadence ``supervised_loop``, so the
    consumer declares its poll cadence to the watchdog itself (ALP-828) via the
    ``register_watch`` seam wired here to ``supervisor.register_watch`` — a bare
    ``beat`` without a declared cadence would leave the task watched-but-
    unbounded, which the watchdog can never trip.
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
            enrichment_callable=enrichment_callable,
            process_lifetime_id=process_lifetime_id,
            is_market_open=is_market_open,
            beat=lambda: supervisor.beat("fill_stream_consumer"),
            register_watch=lambda cadence: supervisor.register_watch(
                "fill_stream_consumer", cadence
            ),
        )

    supervisor.register_task(name="fill_stream_consumer", coro_fn=_fill_stream_consumer_task)


async def _run_watchdog_daemon(
    *,
    probe: HeartbeatProbe | None = None,
    controller: ProcessController | None = None,
    loop: WatchdogLoop | None = None,
    now: Callable[[], float] | None = None,
) -> None:
    """Wire the monitor's out-of-process watchdog (ALP-941).

    Probes the monitor's file heartbeat and restarts the ``alphamind-monitor``
    NSSM service when the beat is older than ``monitor_watchdog_tick_seconds *
    watchdog_cadence_multiplier`` — the same cadence x multiplier stall-bound
    shape as the safety core's watchdog. The seams default to the production
    wiring; tests inject a scripted probe + recording controller.
    """
    config = ContinuousMonitorConfig.model_validate(read_yaml_file(_CONFIG_PATH))
    tick = config.monitor_watchdog_tick_seconds
    stall_bound = _watchdog_stall_bound(config)

    log.info(
        "monitor watchdog starting: stall_bound=%.0fs (tick=%.0fs x %.1f)",
        stall_bound,
        tick,
        config.watchdog_cadence_multiplier,
    )
    await run_watchdog(
        probe=probe
        if probe is not None
        else FileHeartbeatProbe(path=log_directory() / _HEARTBEAT_FILENAME),
        controller=controller
        if controller is not None
        else NssmServiceController(service_name=_MONITOR_SERVICE_NAME),
        stall_bound_seconds=stall_bound,
        loop=loop if loop is not None else supervised_watchdog_loop(tick),
        now=now,
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    load_dotenv()
    configure_monitor_logging(filename=_log_filename_for_subcommand(args.subcommand))

    if args.subcommand == "run":
        asyncio.run(_run_daemon(mode=cast(MonitorMode, args.mode)))
    elif args.subcommand == "watchdog":
        asyncio.run(_run_watchdog_daemon())
    else:
        # ``required=True`` on the subparser makes this unreachable; defensive
        # so a future subcommand addition does not silently fall through.
        msg = f"unknown subcommand: {args.subcommand!r}"
        raise RuntimeError(msg)


if __name__ == "__main__":  # pragma: no cover - exercised via ``python -m``
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:
        # Outermost supervisor per runtime §G1: log + exit 1 so NSSM's restart
        # policy fires. ``BaseException`` (vs ``Exception``) catches
        # ``KeyboardInterrupt`` / ``SystemExit`` paths that the inner ``main``
        # entry can synthesize; ``SystemExit`` is rethrown above so the
        # explicit exit code threads through unchanged.
        log.exception("continuous monitor exited with error")
        sys.exit(1)
