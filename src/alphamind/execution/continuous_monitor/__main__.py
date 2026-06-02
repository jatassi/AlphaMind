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

Per parent issue ALP-123 § Pre-resolved decision (A), no ``bootstrap`` or
``catch-up`` subcommands — the monitor is forward-only.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, assert_never, cast

from dotenv import load_dotenv
from sqlalchemy.orm import Session, sessionmaker

from alphamind.config.guardrails_helpers import (
    load_cumulative_drawdown_progressive_tiers,
)
from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.execution import ExecutionConfig, PaperHarness
from alphamind.config.models.guardrails import BreachResponse, GuardrailsConfig, ProgressiveTier
from alphamind.config.models.venue import VenueConfig
from alphamind.distillation.realized_vol import read_realized_vol_map
from alphamind.execution.broker_adapter import AccountStateQueries, AlpacaClientFactory
from alphamind.execution.continuous_monitor.activities_backfill import (
    register_fill_backfill_task,
)
from alphamind.execution.continuous_monitor.borrow_accrual import (
    register_borrow_accrual_task,
)
from alphamind.execution.continuous_monitor.bracket_stops import (
    AlpacaBracketCloseSubmitter,
    register_options_bracket_watcher_task,
)
from alphamind.execution.continuous_monitor.breach_loop import (
    register_breach_loop_task,
)
from alphamind.execution.continuous_monitor.breach_loop.production_substrate import (
    load_breach_loop_loaded_config,
    load_breach_loop_resolved_config,
    make_adv_provider,
    make_assembled_snapshot_provider,
    make_dispatch_context_provider,
    make_library_config_factory,
    make_library_snapshot_translator,
    make_regime_provider,
    make_snapshot_provider,
    make_submit_envelope,
)
from alphamind.execution.continuous_monitor.cascade_dispatch import TriggerIdGenerator
from alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher import (
    CascadeDispatcher,
    DeferralEvent,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.per_rule_kwargs import (
    build_per_rule_kwargs_providers,
)
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
    make_breach_loop_health_emit,
    wrap_fill_enrichment_with_emit,
    wrap_on_immediate_breach,
)
from alphamind.execution.continuous_monitor.emergency_trigger import (
    AlpacaMarginCallObserver,
    make_emergency_callback,
)
from alphamind.execution.continuous_monitor.emergency_trigger.margin_call_observer import (
    AccountQueriesProtocol,
)
from alphamind.execution.continuous_monitor.entry_window import (
    register_entry_window_watcher_task,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer import EnrichmentCallable
from alphamind.execution.continuous_monitor.greeks_refresh import (
    register_greeks_refresh_task,
)
from alphamind.execution.continuous_monitor.greeks_refresh.wiring import (
    make_activity_log_emitter,
)
from alphamind.execution.continuous_monitor.logging_setup import (
    configure_monitor_logging,
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
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
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
from alphamind.execution.venue_configuration.calendar_cache import (
    TradingCalendarCache,
)
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state import load_portfolio_state_config
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    load_breach_behavior_config,
)
from alphamind.risk_guardrails.guardrail_evaluation import RealizedVolEntry, SqlOptionsIvProvider
from alphamind.risk_guardrails.regime_adaptation import load_config_fan
from alphamind.scripts._common import load_distillation_config
from alphamind.state.config import (
    StatePersistenceConfig,
    load_state_persistence_config,
)
from alphamind.state.process_lifetime import record_process_lifetime
from alphamind.state.repository import (
    build_sql_portfolio_state_repository,
)

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
_GUARDRAILS_CONFIG_PATH = _CONFIG_DIR / "guardrails.yaml"
_BREACH_BEHAVIOR_CONFIG_PATH = _CONFIG_DIR / "breach_behavior.yaml"
_EXECUTION_CONFIG_PATH = _CONFIG_DIR / "execution.yaml"


# Same default the scheduler uses (``alphamind.scheduler.__main__``) so the
# pip-freeze + process-lifetime snapshots land in the canonical archive root.
# Resolved lazily inside ``_run_daemon`` so tests that ``monkeypatch.setenv``
# ``HOME`` after import don't get the developer's real home directory.
def _default_archive_root() -> Path:
    return Path.home() / "AlphaMind" / "archive"


_MAIN_CONFIG_PATH = _CONFIG_DIR / "main.yaml"

# ALP-530 — refresh cadence for the shared realized-vol map. The distillation
# producer runs at invocation cadence (typically daily / on-schedule); the
# monitor lives across invocations, so a 24h refresh keeps the long-lived
# process from holding a multi-day-stale Mapping.
_REALIZED_VOL_REFRESH_INTERVAL_SECONDS: float = 24 * 60 * 60


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
) -> None:
    """Register the 24h shared realized-vol map refresher (ALP-530).

    The task sleeps ``interval_seconds`` between refreshes; each iteration
    re-fetches the per-underlying scalars and mutates ``shared_map`` in
    place so the harness and breach-loop both observe the fresh values.
    ``tickers_provider`` is invoked per refresh so the monitor's
    open-position set can change over the day without re-registering.

    ``empty_map_alert`` is shared with the startup refresh so whichever
    refresh succeeds first fires the one-shot ERROR log if the map is empty.
    """

    async def _refresh_task(_session: MonitorSession, _config: ContinuousMonitorConfig) -> None:
        # Initial refresh already happened at startup; this task drives the
        # subsequent daily cadence through the supervisor's supervised_loop seam
        # (ALP-826) — the loop beats the stall watchdog at the top of each
        # iteration and paces at ``interval_seconds``, so the task is
        # liveness-watched with no hand-wired beat() and no trailing sleep.
        async for _ in supervisor.supervised_loop("realized_vol_refresh", interval_seconds):
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


def _build_breach_response_lookup(
    guardrails_config: GuardrailsConfig,
) -> Mapping[str, BreachResponse]:
    """Project ``GuardrailsConfig.rules`` into a ``rule_id → breach_response`` map.

    The breach loop and the emergency-trigger evaluator both consult this
    lookup to classify a BLOCKED rule's downstream path (``immediate_engine``
    → cascade dispatch; ``deferred_to_pm`` → emergency-invocation count).
    """
    return {rule.id: rule.breach_response for rule in guardrails_config.rules}


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


async def _run_daemon(*, mode: MonitorMode) -> None:  # noqa: PLR0915 — composition root; each statement wires one task seam, extraction would not simplify the dependency graph
    """Daemon path — load config, build supervisor, register tasks, run.

    Stories 02b / 02c register their tasks on the supervisor below. Each
    task wraps its run-forever coroutine in a closure that pre-binds the
    factories the supervisor's uniform ``(session, config)`` task signature
    can't carry. The async engine + session factory share the monitor
    process's lifetime so cross-invocation reads (e.g. underlying-stream
    subscription targets) have a stable handle.
    """
    configure_monitor_logging()
    config = ContinuousMonitorConfig.model_validate(read_yaml_file(_CONFIG_PATH))
    venue_config = VenueConfig.model_validate(read_yaml_file(_VENUE_CONFIG_PATH))
    guardrails_config = GuardrailsConfig.model_validate(read_yaml_file(_GUARDRAILS_CONFIG_PATH))
    breach_behavior_config = load_breach_behavior_config(_BREACH_BEHAVIOR_CONFIG_PATH)
    breach_response_lookup = _build_breach_response_lookup(guardrails_config)
    execution_config = ExecutionConfig.model_validate(read_yaml_file(_EXECUTION_CONFIG_PATH))
    state_persistence_config = load_state_persistence_config(read_yaml_file(_MAIN_CONFIG_PATH))
    session = new_session(mode=mode)
    log.info(
        "monitor session start: session_id=%s mode=%s",
        session.session_id,
        session.mode,
    )
    engine = make_async_engine()
    db_session_factory = make_async_session_factory(engine)
    # Sync engine pair for ``resolve_regime_adaptation`` (sync ``Session`` per
    # ALP-454 (C)). The regime resolver runs inside ``asyncio.to_thread`` from
    # the breach loop's regime provider; threading through a sync engine
    # avoids the async-bridge cost and matches the scheduler's pair pattern.
    sync_engine = make_engine()
    sync_session_factory: sessionmaker[Session] = make_session_factory(sync_engine)
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
    supervisor = MonitorSupervisor(session=session, config=config)
    # ALP-720 — shared SSE event emitter for the /events stream + the
    # production wiring adapters that observe each breach / fill /
    # emergency callsite.
    sse_emitter = SSEEventEmitter()
    underlying_cache = register_underlying_stream_task(supervisor, repository=open_positions_reader)
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
    register_borrow_accrual_task(
        supervisor,
        session_factory=db_session_factory,
        sync_session_factory=sync_session_factory,
        calendar_cache=calendar_cache,
        process_lifetime_id=process_lifetime_id,
    )
    # Constructed once per monitor session so the cascade dispatcher and
    # bracket-stops watcher mint trigger ids from the same monotonic
    # sequence — both encode ``MON.{session}.{trigger}.0`` into the
    # engine-originated ``client_order_id`` and a collision would land two
    # broker submissions with identical IDs.
    trigger_ids = TriggerIdGenerator(session_id=session.session_id)
    progressive_tiers = load_cumulative_drawdown_progressive_tiers()
    _register_breach_loop(
        supervisor,
        underlying_cache=underlying_cache,
        session=session,
        breach_behavior_config=breach_behavior_config,
        breach_response_lookup=breach_response_lookup,
        db_session_factory=db_session_factory,
        sync_session_factory=sync_session_factory,
        trigger_ids=trigger_ids,
        progressive_tiers=progressive_tiers,
        account_state_queries=account_state_queries,
        calendar_cache=calendar_cache,
        state_persistence_config=state_persistence_config,
        config_dir=_CONFIG_DIR,
        realized_vol_map=realized_vol_map,
        sse_emitter=sse_emitter,
    )
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
        await supervisor.run()
    finally:
        sync_engine.dispose()
        await engine.dispose()
        log.info("monitor session end: session_id=%s", session.session_id)


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


def _register_breach_loop(  # noqa: PLR0913 — composition root; each parameter is one production-substrate seam
    supervisor: MonitorSupervisor,
    *,
    underlying_cache: object,
    session: MonitorSession,
    breach_behavior_config: BreachBehaviorConfig,
    breach_response_lookup: Mapping[str, BreachResponse],
    db_session_factory: async_sessionmaker[AsyncSession],
    sync_session_factory: sessionmaker[Session],
    trigger_ids: TriggerIdGenerator,
    progressive_tiers: tuple[ProgressiveTier, ...],
    account_state_queries: AccountQueriesProtocol,
    calendar_cache: TradingCalendarCache,
    state_persistence_config: StatePersistenceConfig,
    config_dir: Path,
    realized_vol_map: Mapping[str, RealizedVolEntry],
    sse_emitter: SSEEventEmitter | None = None,
) -> None:
    """Register the ``breach_loop`` task (story 03b / ALP-437) with the cascade
    dispatcher (story 04a / ALP-438) on ``on_immediate_breach`` and the
    emergency-trigger callback (story 04b / ALP-439) on ``on_emergency_input``.

    Each per-tick dependency is built from :mod:`production_substrate`:

    * ``snapshot_provider`` — :func:`make_snapshot_provider` (SQL repository
      + :func:`assemble_snapshot` + :func:`to_library_snapshot`).
    * ``regime_provider`` — :func:`make_regime_provider` (wraps the latest
      invocation's persisted parameter set in a synthetic
      :class:`RegimeAdaptationOutput`).
    * ``library_config_factory`` — :func:`make_library_config_factory`
      (composes the resolved config once, overrides per-tick effective
      limits from the supplied :class:`ActiveRiskParameterSet`).
    * Dispatcher ``context_provider`` — :func:`make_dispatch_context_provider`
      (builds :class:`BreachDispatchContext` from snapshot + regime per call).
    * Dispatcher ``submit_envelope`` — :func:`make_submit_envelope` (wraps
      :func:`submit_engine_envelope` with a per-emit :class:`InvocationHandle`).

    ``market_hours`` is backed by a :class:`TradingCalendarCache` over the
    supplied :class:`AccountStateQueries`; ``progressive_tiers`` is the
    cumulative-drawdown tier sequence loaded once at daemon startup.
    ``margin_call_observer`` is :class:`AlpacaMarginCallObserver` over the
    same queries — emergencies fire from live broker state instead of
    :class:`NoMarginCallObserver`.
    """
    underlying_cache_typed = cast(UnderlyingPriceCache, underlying_cache)
    _single_entry_emitter = make_activity_log_emitter(db_session_factory)

    async def _activity_log_sink(entries: Iterable[ActivityLogEntry]) -> None:
        for entry in entries:
            await _single_entry_emitter(entry)

    # ``async def`` without ``await`` is intentional: satisfies the
    # ``DeferralSink = Callable[[DeferralEvent], Awaitable[None]]``
    # Protocol the cascade dispatcher awaits at each deferral.
    async def _log_deferral(event: DeferralEvent) -> None:
        log.info(
            "engine envelope deferred to PM: rule=%s position=%s session=%s "
            "trigger=%d cascade=%s reason=%s",
            event.rule_breached,
            event.candidate_position_id,
            event.monitor_session_id,
            event.trigger_id,
            event.cascade_id,
            event.reason,
        )

    # Build the production substrate. Each helper takes the shared dependencies
    # the daemon owns and returns a closure the breach loop / dispatcher consume.
    # Resolved config + portfolio_state config load once at startup and feed
    # all three builders, avoiding the ~87 YAML reads three independent loads
    # would cost.
    resolved_config = load_breach_loop_resolved_config(config_dir)
    portfolio_state_config = load_portfolio_state_config(config_dir / "portfolio_state.yaml")
    # One IvProvider shared by the breach-loop evaluator and the cascade
    # dispatcher's re-projection so both observe identical IV values.
    # ALP-642 — production-side adapter resolving exact-OCC hits against
    # ``options_contract_snapshots`` written by the Polygon collector,
    # with the realized-vol scalar as the fallback channel. ALP-530 — the
    # realized_vol map is the same dict reference owned by ``_run_daemon``
    # and refreshed every 24h; in-place mutations propagate to this
    # provider without re-construction.
    iv_provider = SqlOptionsIvProvider(
        sync_session_factory=sync_session_factory,
        realized_vol=realized_vol_map,
    )
    # ALP-510 — one assembled-snapshot provider + translator shared across
    # the breach-loop snapshot provider and the dispatcher's context provider.
    assembled_snapshot_provider = make_assembled_snapshot_provider(
        session_factory=db_session_factory,
        underlying_cache=underlying_cache_typed,
        resolved=resolved_config,
        portfolio_state_config=portfolio_state_config,
        state_persistence_config=state_persistence_config,
    )
    library_snapshot_translator = make_library_snapshot_translator(resolved=resolved_config)
    snapshot_provider = make_snapshot_provider(
        assembled_snapshot_provider=assembled_snapshot_provider,
        library_snapshot_translator=library_snapshot_translator,
    )
    # ALP-513 — assemble the resolver's slow-changing input fan once at daemon
    # startup; the per-tick regime provider reuses it for every
    # ``resolve_regime_adaptation`` invocation.
    regime_config_fan = load_config_fan(
        config_dir=config_dir,
        loaded_config=load_breach_loop_loaded_config(config_dir),
        distillation_config=load_distillation_config(config_dir / "distillation.yaml"),
    )
    regime_provider = make_regime_provider(
        session_factory=db_session_factory,
        sync_session_factory=sync_session_factory,
        config_fan=regime_config_fan,
        resolved=resolved_config,
    )
    library_config_factory = make_library_config_factory(resolved=resolved_config)
    adv_provider = make_adv_provider(session_factory=db_session_factory)
    dispatch_context_provider = make_dispatch_context_provider(
        assembled_snapshot_provider=assembled_snapshot_provider,
        library_snapshot_translator=library_snapshot_translator,
        regime_provider=regime_provider,
        library_config_factory=library_config_factory,
        underlying_cache=underlying_cache_typed,
        iv_provider=iv_provider,
        adv_provider=adv_provider,
        progressive_tiers=progressive_tiers,
    )
    submit_envelope = make_submit_envelope(
        session_factory=db_session_factory,
        monitor_session_id=session.session_id,
        state_persistence_config=state_persistence_config,
    )

    # Repository for the breach loop's ``get_drawdown_state`` read. The
    # monitor runs across invocations; the bootstrap-style providers below
    # are unused by ``get_drawdown_state`` (singleton-table read) but are
    # required by the factory's signature. Synchronous per ALP-454 (C).
    def _bootstrap_active_provider() -> ActiveRiskParameterSet:
        msg = "active_risk_parameters_provider invoked from the breach loop path"
        raise RuntimeError(msg)

    def _bootstrap_prior_provider(_path: str) -> ActiveRiskParameterSet:
        msg = "prior_active_risk_parameters_provider invoked from the breach loop path"
        raise RuntimeError(msg)

    breach_loop_repository = build_sql_portfolio_state_repository(
        session_factory=db_session_factory,
        invocation_id="monitor-bootstrap",
        active_risk_parameters_provider=_bootstrap_active_provider,
        prior_active_risk_parameters_provider=_bootstrap_prior_provider,
        config=state_persistence_config,
    )

    dispatcher = CascadeDispatcher(
        monitor_session_id=session.session_id,
        breach_config=breach_behavior_config,
        trigger_ids=trigger_ids,
        context_provider=dispatch_context_provider,
        submit_envelope=submit_envelope,
        deferral_sink=_log_deferral,
        per_rule_kwargs_providers=build_per_rule_kwargs_providers(
            breach_behavior_config=breach_behavior_config
        ),
    )
    on_emergency_input = make_emergency_callback(
        session=session,
        breach_behavior_config=breach_behavior_config,
        breach_response_lookup=breach_response_lookup,
        session_factory=db_session_factory,
        trigger_ids=trigger_ids,
        margin_call_observer=AlpacaMarginCallObserver(queries=account_state_queries),
    )

    # ALP-720 — wrap the cascade dispatcher's immediate-breach handler so each
    # immediate-classification breach also emits a ``breach_detected`` SSE
    # event for the operator console. The wrapper delegates to the inner
    # dispatch path unchanged; SSE emit failures log and continue.
    on_immediate_breach = (
        wrap_on_immediate_breach(emitter=sse_emitter, inner=dispatcher.handle_immediate_breach)
        if sse_emitter is not None
        else dispatcher.handle_immediate_breach
    )

    # ALP-732 — surface sustained breach-loop failure as a monitor health
    # signal on the SSE ``/events`` stream. When the HTTP surface is degraded
    # (no emitter) the loop still escalates via its loud degraded/recovered
    # logging; ``on_health_signal`` is simply left at the loop's no-op default.
    on_health_signal = (
        make_breach_loop_health_emit(emitter=sse_emitter) if sse_emitter is not None else None
    )

    register_breach_loop_task(
        supervisor,
        repository=breach_loop_repository,
        cache=underlying_cache_typed,
        snapshot_provider=snapshot_provider,
        regime_provider=regime_provider,
        progressive_tiers=progressive_tiers,
        library_config_factory=library_config_factory,
        iv_provider=iv_provider,
        risk_free_rate=0.045,
        breach_response_lookup=breach_response_lookup,
        market_hours=calendar_cache,
        max_price_age_seconds=portfolio_state_config.snapshot_freshness_max_price_age_seconds,
        on_immediate_breach=on_immediate_breach,
        on_emergency_input=on_emergency_input,
        activity_log_sink=_activity_log_sink,
        on_health_signal=on_health_signal,
    )


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
        # Outermost supervisor per runtime §G1: log + exit 1 so NSSM's restart
        # policy fires. ``BaseException`` (vs ``Exception``) catches
        # ``KeyboardInterrupt`` / ``SystemExit`` paths that the inner ``main``
        # entry can synthesize; ``SystemExit`` is rethrown above so the
        # explicit exit code threads through unchanged.
        log.exception("continuous monitor exited with error")
        sys.exit(1)
