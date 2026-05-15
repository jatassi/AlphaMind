"""Supervisor-side wiring + production submitter for the bracket-stops watcher (story 04c).

Two pieces:

* :class:`SqlBracketRepository` — narrow :class:`BracketRepository` wrapper
  around an ``AsyncSession`` factory that delegates to the same SQL path
  ``SqlPortfolioStateRepository`` uses. Reused so the watcher reads bracket
  records with the same row → record codec the rest of the system uses.
* :class:`AlpacaBracketCloseSubmitter` — concrete :class:`BracketCloseSubmitter`
  that wraps the broker_adapter's :func:`submit_options_close` and
  :func:`submit_mleg_close`. The combined-close → per-leg fallback for
  strategy positions is implemented here so the task kernel stays simple.
* :func:`register_options_bracket_watcher_task` — supervisor wiring helper.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    OccSymbol,
)
from alphamind.commands.command_models import (
    CloseCommand,
    StrategyType,
)
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter.order_mleg import (
    MLEGLegAck,
    PositionIntentLiteral,
    submit_mleg_close,
)
from alphamind.execution.broker_adapter.order_options import (
    PermanentRejectionError,
    build_occ_symbol,
    submit_options_close,
)
from alphamind.execution.broker_adapter.retry import GatewaySubmissionFailed, Submitted
from alphamind.execution.continuous_monitor.bracket_stops.closer import (
    BracketCloseSubmitter,
    CloseSubmissionResult,
)
from alphamind.execution.continuous_monitor.bracket_stops.task import (
    run_options_bracket_watcher,
)
from alphamind.execution.continuous_monitor.cascade_dispatch import TriggerIdGenerator
from alphamind.execution.continuous_monitor.greeks_refresh.wiring import (
    make_activity_log_emitter,
    make_invocation_id_provider,
    make_risk_free_rate_provider,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
)
from alphamind.execution.continuous_monitor.underlying_stream.subscriptions import (
    OpenPositionsReader,
)
from alphamind.portfolio_state.records.orders import BracketRecord
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
)
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import (
    rows_to_record as bracket_rows_to_record,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SqlBracketRepository
# ---------------------------------------------------------------------------


class SqlBracketRepository:
    """SQL-backed :class:`BracketRepository` for the bracket-stops watcher.

    Mirrors :meth:`SqlPortfolioStateRepository.get_brackets_for_positions`
    but opens a fresh session per call so it can outlive any one invocation
    context (the monitor runs across invocations).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]:
        if not position_ids:
            return ()
        async with self._session_factory() as session:
            bracket_rows = list(
                (
                    await session.execute(
                        select(BracketRow)
                        .where(BracketRow.position_id.in_(position_ids))
                        .order_by(BracketRow.bracket_id.asc())
                    )
                ).scalars()
            )
            if not bracket_rows:
                return ()
            bracket_ids = [b.bracket_id for b in bracket_rows]
            leg_rows = list(
                (
                    await session.execute(
                        select(BracketLegRow)
                        .where(BracketLegRow.bracket_id.in_(bracket_ids))
                        .order_by(BracketLegRow.bracket_id.asc(), BracketLegRow.leg_index.asc())
                    )
                ).scalars()
            )
        legs_by_bracket: dict[str, list[BracketLegRow]] = {bid: [] for bid in bracket_ids}
        for leg in leg_rows:
            legs_by_bracket[leg.bracket_id].append(leg)
        return tuple(
            bracket_rows_to_record(b, tuple(legs_by_bracket[b.bracket_id])) for b in bracket_rows
        )


# ---------------------------------------------------------------------------
# AlpacaBracketCloseSubmitter
# ---------------------------------------------------------------------------


_DIRECTION_TO_CLOSE_INTENT: dict[Direction, str] = {
    Direction.LONG: "sell_to_close",
    Direction.SHORT: "buy_to_close",
}


class AlpacaBracketCloseSubmitter:
    """Production :class:`BracketCloseSubmitter` backed by the broker adapter.

    Wraps :func:`submit_options_close` for single-leg options positions and
    :func:`submit_mleg_close` for strategy positions, with a per-leg fallback
    on Alpaca rejection of the combined close (per
    ``docs/design/05-execution-layer/broker-adapter.md`` § Known gaps).
    """

    def __init__(
        self,
        *,
        client_factory: object,  # AlpacaClientFactory; typed object to avoid import cycle
        execution_config: ExecutionConfig,
    ) -> None:
        self._client_factory = client_factory
        self._execution_config = execution_config

    @property
    def _trading_client(self) -> object:
        """Lazily build the trading client.

        ``AlpacaClientFactory.build_trading_client()`` returns a live SDK
        client; the submitter caches one per session. Tests substitute the
        full :class:`AlpacaBracketCloseSubmitter` with a fake; in production,
        this property is invoked once per submission.
        """
        from typing import cast

        from alpaca.trading.client import TradingClient

        return cast(TradingClient, self._client_factory.build_trading_client())  # type: ignore[attr-defined]

    async def submit_options_close(
        self,
        *,
        position: PositionRecord,
        details: OptionsPositionDetails,
        client_order_id: str,
    ) -> CloseSubmissionResult:
        occ_symbol = build_occ_symbol(
            details.underlying_ticker,
            details.expiration_date,
            details.contract_type,
            details.strike_price,
        )
        intent = _DIRECTION_TO_CLOSE_INTENT[position.direction]
        outcome = await submit_options_close(
            command=_build_synthetic_close_command(position),
            client=self._trading_client,  # type: ignore[arg-type]
            execution=self._execution_config,
            client_order_id=client_order_id,
            occ_symbol=occ_symbol,
            position_qty=details.contract_count,
            position_intent=intent,  # type: ignore[arg-type]
        )
        if isinstance(outcome, GatewaySubmissionFailed):
            # TRY004 false positive: ``GatewaySubmissionFailed`` is a runtime
            # retry-exhaustion outcome, not a type-validation failure — RuntimeError
            # is the right shape.
            msg = (
                f"bracket-stop close exhausted retries: "
                f"reason={outcome.reason!r}, last_error={outcome.last_error_class!r}"
            )
            raise RuntimeError(msg)  # noqa: TRY004
        # Submitted
        payload = outcome.payload
        return CloseSubmissionResult(
            order_ids=(payload.alpaca_order_id,),
            mode="single_leg",
        )

    async def submit_strategy_close(
        self,
        *,
        position: PositionRecord,
        details: StrategyPositionDetails,
        client_order_id_base: str,
    ) -> CloseSubmissionResult:
        open_legs = _open_legs_from_strategy(details)
        strategy_type = _strategy_type_from_label(details.strategy_type_label)
        try:
            outcome = await submit_mleg_close(
                command=_build_synthetic_close_command(position),
                client=self._trading_client,  # type: ignore[arg-type]
                execution=self._execution_config,
                client_order_id=client_order_id_base,
                open_legs=open_legs,
                strategy_type=strategy_type,
                position_units=1.0,  # bracket close: one strategy unit
            )
        except PermanentRejectionError:
            log.warning(
                "bracket_stops: combined-close rejected for position %s; "
                "falling back to per-leg close",
                position.position_id,
            )
            return await self._per_leg_fallback(
                position=position, details=details, client_order_id_base=client_order_id_base
            )
        if isinstance(outcome, GatewaySubmissionFailed):
            log.warning(
                "bracket_stops: combined-close gateway failure for position %s; "
                "falling back to per-leg close",
                position.position_id,
            )
            return await self._per_leg_fallback(
                position=position, details=details, client_order_id_base=client_order_id_base
            )
        payload = outcome.payload
        return CloseSubmissionResult(
            order_ids=(payload.alpaca_order_id,),
            mode="strategy_combined",
        )

    async def _per_leg_fallback(
        self,
        *,
        position: PositionRecord,
        details: StrategyPositionDetails,
        client_order_id_base: str,
    ) -> CloseSubmissionResult:
        """Per-leg market-order fallback when the combined mleg close is rejected."""
        order_ids: list[str] = []
        for idx, leg in enumerate(details.legs):
            leg_client_id = f"{client_order_id_base}-leg{idx}"
            leg_details = leg.options
            occ_symbol = build_occ_symbol(
                leg_details.underlying_ticker,
                leg_details.expiration_date,
                leg_details.contract_type,
                leg_details.strike_price,
            )
            leg_direction = leg.direction or position.direction
            intent = _DIRECTION_TO_CLOSE_INTENT[leg_direction]
            outcome = await submit_options_close(
                command=_build_synthetic_close_command(position),
                client=self._trading_client,  # type: ignore[arg-type]
                execution=self._execution_config,
                client_order_id=leg_client_id,
                occ_symbol=occ_symbol,
                position_qty=leg_details.contract_count,
                position_intent=intent,  # type: ignore[arg-type]
            )
            if isinstance(outcome, Submitted):
                order_ids.append(outcome.payload.alpaca_order_id)
            else:
                # Per-leg leg failed: surface the partial-result so the
                # operator's monitor log carries the trail. The activity
                # log entry was already written above; this is best-effort
                # cleanup. Continue with remaining legs.
                log.error(
                    "bracket_stops: per-leg fallback failed for position %s leg %s: %s",
                    position.position_id,
                    leg.leg_id,
                    outcome,
                )
        return CloseSubmissionResult(
            order_ids=tuple(order_ids),
            mode="strategy_per_leg",
        )


def _build_synthetic_close_command(position: PositionRecord) -> CloseCommand:
    """Construct a CloseCommand the broker_adapter close path expects.

    The watcher does not route through the OMS, so we synthesize the minimum
    fields the broker_adapter's translator reads — ``position_id``,
    ``quantity="all"``, ``order_type="market"``, and ``close_rationale_type``.
    """
    return CloseCommand(
        command_id=None,
        command_type="close",
        position_id=position.position_id,
        quantity="all",
        order_type="market",
        limit_price=None,
        close_rationale_type="risk_management",
        risk_management_subtype="engine_guardrail",
    )


def _open_legs_from_strategy(
    details: StrategyPositionDetails,
) -> Sequence[MLEGLegAck]:
    """Reconstruct the open-legs ack tuple the mleg close path needs from a strategy position.

    Mirrors the layout :func:`submit_mleg_open` produces — one ack per leg
    with the original open-side intent, sized at ratio=1 (we use the
    strategy's per-unit ratios). The ``occ_symbol`` per leg is rebuilt from
    the leg's options details.
    """
    acks: list[MLEGLegAck] = []
    for leg in details.legs:
        opt = leg.options
        occ = build_occ_symbol(
            opt.underlying_ticker, opt.expiration_date, opt.contract_type, opt.strike_price
        )
        leg_direction = leg.direction or Direction.LONG
        side: str = "buy" if leg_direction is Direction.LONG else "sell"
        intent: PositionIntentLiteral = (
            "buy_to_open" if leg_direction is Direction.LONG else "sell_to_open"
        )
        acks.append(
            MLEGLegAck(
                occ_symbol=OccSymbol(occ),
                side=side,  # type: ignore[arg-type]
                ratio_qty=int(opt.contract_count),
                position_intent=intent,
            )
        )
    return tuple(acks)


_KNOWN_STRATEGY_TYPES: frozenset[str] = frozenset(
    {"vertical_spread", "calendar_spread", "straddle", "strangle", "iron_condor", "custom"}
)

# Substring-keyed fallback for free-form ``strategy_type_label`` values like
# ``"vertical_call_spread"`` or ``"bear_put_spread"``. First match wins; order
# matters — narrower keys (``iron_condor``) precede broader ones (``spread``).
_SUBSTRING_TO_STRATEGY_TYPE: tuple[tuple[str, StrategyType], ...] = (
    ("iron_condor", "iron_condor"),
    ("condor", "iron_condor"),
    ("straddle", "straddle"),
    ("strangle", "strangle"),
    ("calendar", "calendar_spread"),
    ("vertical", "vertical_spread"),
    ("spread", "vertical_spread"),
)


def _strategy_type_from_label(label: str) -> StrategyType:
    """Map a strategy_type_label string to the canonical :data:`StrategyType` literal.

    The label is a free-form string stored on the position record. For
    bracket-stop closes we only need a syntactically-valid value; the broker
    adapter's mleg-close path passes it through to the ack record. Fall back
    to ``"custom"`` when the label does not match any known strategy.
    """
    if label in _KNOWN_STRATEGY_TYPES:
        return label  # type: ignore[return-value]
    for substring, mapped in _SUBSTRING_TO_STRATEGY_TYPE:
        if substring in label:
            return mapped
    return "custom"


# ---------------------------------------------------------------------------
# Supervisor registration
# ---------------------------------------------------------------------------


def register_options_bracket_watcher_task(
    supervisor: MonitorSupervisor,
    *,
    position_repository: OpenPositionsReader,
    cache: UnderlyingPriceCache,
    session_factory: async_sessionmaker[AsyncSession],
    submitter: BracketCloseSubmitter,
    trigger_ids: TriggerIdGenerator,
) -> None:
    """Register the ``bracket_stops`` task on *supervisor*.

    The bracket repository is constructed from the same session factory the
    fill-stream consumer + greeks refresh share; the activity-log emitter,
    risk-free-rate provider, and invocation-id provider re-use the
    greeks-refresh wiring so the monitor process has one canonical set of
    these helpers (no near-duplicate envelopes).

    ``trigger_ids`` is the cascade-dispatch :class:`TriggerIdGenerator`
    constructed once per monitor session in ``_register_breach_loop``.
    Sharing the instance keeps a bracket-stop fire and a cascade dispatch
    in the same session from minting the same engine-originated
    ``client_order_id`` (both encode ``MON.{session}.{trigger}.0``).
    """
    bracket_repository = SqlBracketRepository(session_factory)
    activity_log = make_activity_log_emitter(session_factory)
    invocation_id_provider = make_invocation_id_provider(session_factory)
    risk_free_rate_provider = make_risk_free_rate_provider(session_factory)

    async def _coro(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
        await run_options_bracket_watcher(
            session,
            config,
            position_repository=position_repository,
            bracket_repository=bracket_repository,
            cache=cache,
            submitter=submitter,
            activity_log=activity_log,
            invocation_id_provider=invocation_id_provider,
            risk_free_rate_provider=risk_free_rate_provider,
            trigger_ids=trigger_ids,
        )

    supervisor.register_task(name="bracket_stops", coro_fn=_coro)


__all__ = [
    "AlpacaBracketCloseSubmitter",
    "SqlBracketRepository",
    "register_options_bracket_watcher_task",
]
