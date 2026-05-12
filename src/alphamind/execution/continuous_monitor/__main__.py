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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from dotenv import load_dotenv

from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.execution import ExecutionConfig
from alphamind.config.models.guardrails import BreachResponse, GuardrailsConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.broker_adapter import AccountStateQueries, AlpacaClientFactory
from alphamind.execution.continuous_monitor.bracket_stops import (
    AlpacaBracketCloseSubmitter,
    register_options_bracket_watcher_task,
)
from alphamind.execution.continuous_monitor.breach_loop import (
    register_breach_loop_task,
)
from alphamind.execution.continuous_monitor.greeks_refresh import (
    register_greeks_refresh_task,
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
from alphamind.execution.continuous_monitor.underlying_stream.reader import (
    SqlOpenPositionsReader,
)
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    load_breach_behavior_config,
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
    session = new_session(mode=mode)
    log.info(
        "monitor session start: session_id=%s mode=%s",
        session.session_id,
        session.mode,
    )
    engine = make_async_engine()
    db_session_factory = make_async_session_factory(engine)
    open_positions_reader = SqlOpenPositionsReader(db_session_factory)
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
    )
    _register_breach_loop(
        supervisor,
        underlying_cache=underlying_cache,
        session=session,
        breach_behavior_config=breach_behavior_config,
        breach_response_lookup=breach_response_lookup,
        db_session_factory=db_session_factory,
    )
    register_options_bracket_watcher_task(
        supervisor,
        position_repository=open_positions_reader,
        cache=underlying_cache,
        session_factory=db_session_factory,
        submitter=AlpacaBracketCloseSubmitter(
            client_factory=AlpacaClientFactory(venue_config, mode),
            execution_config=execution_config,
        ),
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


def _register_breach_loop(
    supervisor: MonitorSupervisor,
    *,
    underlying_cache: object,
    session: MonitorSession,
    breach_behavior_config: BreachBehaviorConfig,
    breach_response_lookup: Mapping[str, BreachResponse],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Register the ``breach_loop`` task (story 03b / ALP-437) with the cascade
    dispatcher (story 04a / ALP-438) on ``on_immediate_breach`` and the
    emergency-trigger callback (story 04b / ALP-439) on ``on_emergency_input``.

    Story 03b shipped the loop itself with no-op callbacks; this helper
    replaces both with real callbacks. ``on_immediate_breach`` is
    :meth:`CascadeDispatcher.handle_immediate_breach` (one dispatcher
    instance per session so the :class:`TriggerIdGenerator` stays monotonic
    across breaches). ``on_emergency_input`` is the callback returned by
    :func:`make_emergency_callback` — it consults the live cooldown tracker,
    runs the trigger evaluators, and writes ``EMERGENCY_INVOCATION_REQUESTED``
    activity-log entries through ``db_session_factory``.

    The production wiring of the loop's non-callback dependencies
    (``snapshot_provider``, ``regime_provider``, ``library_config_factory``,
    ``market_hours`` via ``TradingCalendarCache``, ``progressive_tiers``
    from the cumulative-drawdown rule) and the dispatcher's per-tick
    ``context_provider`` + ``submit_envelope`` paths remain deferred to
    follow-up stories that wire the SQL repository, regime resolver,
    calendar cache, and invocation-handle factory into the monitor process.
    Until those land, the loop's ``market_hours`` stub reports closed so the
    loop sleeps indefinitely without exercising either path.
    """
    from collections.abc import Iterable
    from datetime import datetime

    from alphamind.execution.continuous_monitor.breach_loop.result import (
        BreachLoopResult,
    )
    from alphamind.execution.continuous_monitor.cascade_dispatch import (
        TriggerIdGenerator,
    )
    from alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher import (
        BreachDispatchContext,
        CascadeDispatcher,
        DeferralEvent,
    )
    from alphamind.execution.continuous_monitor.emergency_trigger import (
        make_emergency_callback,
    )
    from alphamind.execution.continuous_monitor.underlying_stream.cache import (
        UnderlyingPriceCache,
    )
    from alphamind.execution.oms.engine_envelope import (
        EngineEnvelope as OmsEngineEnvelope,
    )
    from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
    from alphamind.risk_guardrails.guardrail_evaluation import (
        FixtureIvProvider,
    )

    class _ClosedMarket:
        def is_market_open(self, at: datetime) -> bool:
            del at
            return False

    async def _no_op_sink(_entries: Iterable[ActivityLogEntry]) -> None:
        return None

    async def _empty_snapshot_provider() -> object:
        msg = "snapshot_provider not yet wired; deferred follow-up"
        raise RuntimeError(msg)

    async def _empty_regime_provider() -> object:
        msg = "regime_provider not yet wired; deferred follow-up"
        raise RuntimeError(msg)

    def _empty_library_config_factory(_active: object) -> object:
        msg = "library_config_factory not yet wired; deferred follow-up"
        raise RuntimeError(msg)

    def _empty_dispatch_context_provider() -> BreachDispatchContext:
        msg = "cascade dispatch context_provider not yet wired; deferred follow-up"
        raise RuntimeError(msg)

    async def _empty_submit_envelope(_envelope: OmsEngineEnvelope) -> Any:
        msg = "submit_envelope path not yet wired; deferred follow-up"
        raise RuntimeError(msg)

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

    trigger_ids = TriggerIdGenerator(session_id=session.session_id)
    dispatcher = CascadeDispatcher(
        monitor_session_id=session.session_id,
        breach_config=breach_behavior_config,
        trigger_ids=trigger_ids,
        context_provider=_empty_dispatch_context_provider,
        submit_envelope=_empty_submit_envelope,
        deferral_sink=_log_deferral,
        per_rule_kwargs_providers={},
    )
    on_emergency_input = make_emergency_callback(
        session=session,
        breach_behavior_config=breach_behavior_config,
        breach_response_lookup=breach_response_lookup,
        session_factory=db_session_factory,
    )

    register_breach_loop_task(
        supervisor,
        repository=cast(Any, None),
        cache=cast(UnderlyingPriceCache, underlying_cache),
        snapshot_provider=cast(Any, _empty_snapshot_provider),
        regime_provider=cast(Any, _empty_regime_provider),
        progressive_tiers=(),
        library_config_factory=cast(Any, _empty_library_config_factory),
        iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
        risk_free_rate=0.045,
        breach_response_lookup=breach_response_lookup,
        market_hours=_ClosedMarket(),
        on_immediate_breach=dispatcher.handle_immediate_breach,
        on_emergency_input=on_emergency_input,
        activity_log_sink=_no_op_sink,
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
        log.exception("continuous monitor exited with error")
        sys.exit(1)
