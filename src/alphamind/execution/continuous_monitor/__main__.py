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
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from dotenv import load_dotenv

from alphamind.config.guardrails_helpers import (
    load_cumulative_drawdown_progressive_tiers,
)
from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.execution import ExecutionConfig
from alphamind.config.models.guardrails import BreachResponse, GuardrailsConfig, ProgressiveTier
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.broker_adapter import AccountStateQueries, AlpacaClientFactory
from alphamind.execution.continuous_monitor.bracket_stops import (
    AlpacaBracketCloseSubmitter,
    register_options_bracket_watcher_task,
)
from alphamind.execution.continuous_monitor.breach_loop import (
    register_breach_loop_task,
)
from alphamind.execution.continuous_monitor.breach_loop.production_substrate import (
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
from alphamind.execution.continuous_monitor.emergency_trigger import (
    AlpacaMarginCallObserver,
    make_emergency_callback,
)
from alphamind.execution.continuous_monitor.emergency_trigger.margin_call_observer import (
    AccountQueriesProtocol,
)
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
from alphamind.execution.venue_configuration.calendar_cache import (
    TradingCalendarCache,
)
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.portfolio_state import load_portfolio_state_config
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    load_breach_behavior_config,
)
from alphamind.risk_guardrails.guardrail_evaluation import FixtureIvProvider
from alphamind.state.config import (
    StatePersistenceConfig,
    load_state_persistence_config,
)
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
_MAIN_CONFIG_PATH = _CONFIG_DIR / "main.yaml"


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


async def _run_daemon(*, mode: MonitorMode) -> None:
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
    underlying_cache = register_underlying_stream_task(supervisor, repository=open_positions_reader)
    _register_fill_stream_consumer(
        supervisor,
        venue_config=venue_config,
        db_session_factory=db_session_factory,
    )
    register_greeks_refresh_task(
        supervisor,
        repository=open_positions_reader,
        cache=underlying_cache,
        session_factory=db_session_factory,
        market_open=calendar_cache.is_market_open,
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
        trigger_ids=trigger_ids,
        progressive_tiers=progressive_tiers,
        account_state_queries=account_state_queries,
        calendar_cache=calendar_cache,
        state_persistence_config=state_persistence_config,
        config_dir=_CONFIG_DIR,
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


def _register_breach_loop(  # noqa: PLR0913 — composition root; each parameter is one production-substrate seam
    supervisor: MonitorSupervisor,
    *,
    underlying_cache: object,
    session: MonitorSession,
    breach_behavior_config: BreachBehaviorConfig,
    breach_response_lookup: Mapping[str, BreachResponse],
    db_session_factory: async_sessionmaker[AsyncSession],
    trigger_ids: TriggerIdGenerator,
    progressive_tiers: tuple[ProgressiveTier, ...],
    account_state_queries: AccountQueriesProtocol,
    calendar_cache: TradingCalendarCache,
    state_persistence_config: StatePersistenceConfig,
    config_dir: Path,
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
    iv_provider = FixtureIvProvider(surface={}, realized_vol={})
    # ALP-510 — one AssembledSnapshot provider + translator shared by the
    # breach-loop's library-snapshot provider and the cascade dispatcher's
    # context provider, so the assemble pipeline runs at most once per
    # consumer call rather than twice per immediate-breach event.
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
    regime_provider = make_regime_provider(
        session_factory=db_session_factory,
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
        on_immediate_breach=dispatcher.handle_immediate_breach,
        on_emergency_input=on_emergency_input,
        activity_log_sink=_activity_log_sink,
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
