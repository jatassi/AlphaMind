"""Run-forever options bracket-stop watcher (story 04c / ALP-440).

The long-running asyncio task the supervisor registers as ``bracket_stops``.
Each cycle:

1. Snapshots open option / strategy positions.
2. Loads each position's bracket (with all legs).
3. For each price-based invalidation leg: reads spot from the cache, runs
   :func:`evaluate_price_based_trigger`; on True, calls
   :func:`submit_options_bracket_close`.
4. For each P/L-based leg: reads spot + the position's freshly-refreshed
   greeks (via 03a), runs :func:`evaluate_pl_target_trigger`; on True,
   calls :func:`submit_options_bracket_close`.
5. Tracks ``(bracket_id, leg_id)`` already-fired pairs in the closure so
   each leg fires at most once per monitor-process lifetime.

Per parent issue ALP-123 § Pre-resolved decision (I), the closer submits
directly to the broker adapter — no engine envelope. A strategy position's
P/L-target leg routes into the strategy net-P/L evaluator (ALP-601);
price-based invalidation on the underlying fires for strategies too,
matching ``orders-and-brackets.md § Options price-based stops``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.bracket_stops.closer import (
    ActivityLogEmitter,
    BracketCloseSubmitter,
    submit_options_bracket_close,
)
from alphamind.execution.continuous_monitor.bracket_stops.triggers import (
    evaluate_pl_target_trigger,
    evaluate_price_based_trigger,
    evaluate_strategy_pl_target_trigger,
)
from alphamind.execution.continuous_monitor.cascade_dispatch import TriggerIdGenerator
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
)
from alphamind.execution.continuous_monitor.underlying_stream.subscriptions import (
    OpenPositionsReader,
)
from alphamind.portfolio_state.events.activity_log import PositionExitMethod
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
)

log = logging.getLogger(__name__)


NowProvider = Callable[[], datetime]
# Async-native: invoked from the supervisor's running loop, so the wiring
# layer can hit the DB directly without bridging through ``asyncio.run``.
RiskFreeRateProvider = Callable[[], Awaitable[float]]
InvocationIdProvider = Callable[[], Awaitable[str]]
SleepCallable = Callable[[float], Awaitable[None]]


@runtime_checkable
class BracketRepository(Protocol):
    """Narrow read surface the watcher needs to fetch active bracket records.

    Mirrors :meth:`SqlPortfolioStateRepository.get_brackets_for_positions`
    so production wiring uses the same SQL path the assembler uses.
    """

    async def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]: ...


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


_FiredLegKey = tuple[str, str]  # (bracket_id, leg_id)


def _spot_for_position(position: PositionRecord, cache: UnderlyingPriceCache) -> float | None:
    """Resolve the underlying spot for a position from the live cache."""
    details = position.details
    if isinstance(details, OptionsPositionDetails):
        ticker = details.underlying_ticker
    elif isinstance(details, StrategyPositionDetails) and details.legs:
        ticker = details.legs[0].options.underlying_ticker
    else:
        return None
    quote = cache.get(ticker)
    return None if quote is None else quote.price


def _is_options_or_strategy(position: PositionRecord) -> bool:
    return isinstance(position.details, OptionsPositionDetails | StrategyPositionDetails)


def _is_active_eligible_leg(leg: BracketLeg) -> bool:
    """Return True when *leg* is a price-based active leg eligible for firing."""
    if leg.status is not BracketLegStatus.ACTIVE:
        return False
    if leg.leg_type not in (BracketLegType.PRICE_STOP, BracketLegType.TAKE_PROFIT):
        return False
    return isinstance(leg.trigger, PriceTrigger)


def _trigger_reason_for_leg(leg: BracketLeg) -> PositionExitMethod:
    if leg.leg_type is BracketLegType.TAKE_PROFIT:
        return PositionExitMethod.TARGET_REACHED
    return PositionExitMethod.STOP_TRIGGERED


def _estimated_exit_price_for(position: PositionRecord, spot: float) -> tuple[float, float]:
    """Coarse pre-fill estimates for the POSITION_CLOSED activity-log entry.

    Returns ``(estimated_exit_price, estimated_realized_pnl_usd)`` based on
    the prior greeks-derived option price (intrinsic + time value via the
    BS closed form would require recomputing here, which the closer's
    caller already does for P/L-target firings). For the activity-log
    entry only, we surface a reasonable scalar so operators see
    *something* — Phase 1 reconciliation overwrites the persisted P/L with
    the actual fill once the order lands.

    Single-leg options: estimated exit = prior premium per contract — a
    conservative-by-design placeholder. Strategy positions: zero — the
    persisted net P/L gets the truth at fill time.
    """
    del spot
    details = position.details
    if isinstance(details, OptionsPositionDetails):
        estimated_exit = details.premium_paid_per_contract
        # If the position has fills, derive realised P/L from the difference
        # (placeholder — actual fill cascade overwrites this).
        if position.execution_history:
            entry_price = float(position.execution_history[0].fill_price)
            qty = details.contract_count * details.contract_multiplier
            pnl = (estimated_exit - entry_price) * qty
            return estimated_exit, pnl
        return estimated_exit, 0.0
    return 0.0, 0.0


# ---------------------------------------------------------------------------
# Single-cycle kernel
# ---------------------------------------------------------------------------


async def _run_bracket_stop_cycle(  # noqa: PLR0913 — kernel surfaces every collaborator as an injectable seam for tests
    *,
    config: ContinuousMonitorConfig,
    position_repository: OpenPositionsReader,
    bracket_repository: BracketRepository,
    cache: UnderlyingPriceCache,
    submitter: BracketCloseSubmitter,
    activity_log: ActivityLogEmitter,
    invocation_id_provider: InvocationIdProvider,
    trigger_ids: TriggerIdGenerator,
    monitor_session_id: str,
    now: datetime,
    risk_free_rate: float,
    fired_legs: set[_FiredLegKey],
) -> None:
    """One inspection-cycle pass — public for testability.

    Walks open option / strategy positions, evaluates each active price-based
    or P/L-based leg, fires the closer on satisfied triggers, and marks the
    fired ``(bracket_id, leg_id)`` pair so subsequent cycles don't double-fire.
    """
    del config  # cadence consumed by the run-forever loop; kernel is per-cycle
    positions = await position_repository.get_open_positions()
    eligible_positions = tuple(p for p in positions if _is_options_or_strategy(p))
    if not eligible_positions:
        return
    position_ids = tuple(p.position_id for p in eligible_positions)
    brackets = await bracket_repository.get_brackets_for_positions(position_ids=position_ids)
    if not brackets:
        return
    brackets_by_position: dict[str, BracketRecord] = {b.position_id: b for b in brackets}
    for position in eligible_positions:
        bracket = brackets_by_position.get(position.position_id)
        if bracket is None:
            continue
        spot = _spot_for_position(position, cache)
        if spot is None or spot <= 0.0:
            continue
        await _evaluate_bracket_legs(
            position=position,
            bracket=bracket,
            spot=spot,
            submitter=submitter,
            activity_log=activity_log,
            invocation_id_provider=invocation_id_provider,
            trigger_ids=trigger_ids,
            monitor_session_id=monitor_session_id,
            now=now,
            risk_free_rate=risk_free_rate,
            fired_legs=fired_legs,
        )


async def _evaluate_bracket_legs(  # noqa: PLR0913 — fans out the cycle's per-position context
    *,
    position: PositionRecord,
    bracket: BracketRecord,
    spot: float,
    submitter: BracketCloseSubmitter,
    activity_log: ActivityLogEmitter,
    invocation_id_provider: InvocationIdProvider,
    trigger_ids: TriggerIdGenerator,
    monitor_session_id: str,
    now: datetime,
    risk_free_rate: float,
    fired_legs: set[_FiredLegKey],
) -> None:
    for leg in bracket.protective_legs:
        if not _is_active_eligible_leg(leg):
            continue
        key = (bracket.bracket_id, leg.leg_id)
        if key in fired_legs:
            continue
        if not _leg_should_fire(
            position=position,
            leg=leg,
            spot=spot,
            now=now,
            risk_free_rate=risk_free_rate,
        ):
            continue
        # Mark fired BEFORE submission so a retry-on-exception cycle does
        # not re-fire the same leg.
        fired_legs.add(key)
        await _fire_leg(
            position=position,
            bracket=bracket,
            leg=leg,
            spot=spot,
            submitter=submitter,
            activity_log=activity_log,
            invocation_id_provider=invocation_id_provider,
            trigger_ids=trigger_ids,
            monitor_session_id=monitor_session_id,
            now=now,
        )


def _leg_should_fire(
    *,
    position: PositionRecord,
    leg: BracketLeg,
    spot: float,
    now: datetime,
    risk_free_rate: float,
) -> bool:
    """Route to the right evaluator based on leg geometry.

    A P/L-anchored leg on a :class:`StrategyPositionDetails` position routes
    into :func:`evaluate_strategy_pl_target_trigger` (the strategy net-P/L
    evaluator); a single-leg options position routes into
    :func:`evaluate_pl_target_trigger`. A non-P/L leg evaluates against the
    underlying price.
    """
    if leg.pl_anchor is not None:
        try:
            if isinstance(position.details, StrategyPositionDetails):
                return evaluate_strategy_pl_target_trigger(
                    position=position,
                    leg=leg,
                    spot=spot,
                    risk_free_rate=risk_free_rate,
                    as_of=now,
                    buffer_pct=0.0,
                )
            return evaluate_pl_target_trigger(
                position=position,
                leg=leg,
                spot=spot,
                risk_free_rate=risk_free_rate,
                as_of=now,
                buffer_pct=0.0,
            )
        except ValueError:
            # Anchor missing actual_entry_price (pre-fill state) or invalid
            # IV — log via the structured logger; the loop continues so other
            # legs still evaluate.
            log.exception(
                "bracket_stops: P/L trigger evaluation skipped for leg %s on position %s",
                leg.leg_id,
                position.position_id,
            )
            return False
    return evaluate_price_based_trigger(position=position, leg=leg, spot=spot)


async def _fire_leg(  # noqa: PLR0913 — fan-out parameters for the closer call
    *,
    position: PositionRecord,
    bracket: BracketRecord,
    leg: BracketLeg,
    spot: float,
    submitter: BracketCloseSubmitter,
    activity_log: ActivityLogEmitter,
    invocation_id_provider: InvocationIdProvider,
    trigger_ids: TriggerIdGenerator,
    monitor_session_id: str,
    now: datetime,
) -> None:
    """Submit the closing order + write the POSITION_CLOSED activity-log entry."""
    trigger_reason = _trigger_reason_for_leg(leg)
    estimated_exit_price, realized_pnl_usd = _estimated_exit_price_for(position, spot)
    # The same monotonic per-session counter the cascade dispatcher uses;
    # threaded in from ``_register_breach_loop`` so a bracket-stop fire and a
    # cascade dispatch in the same session cannot collide on
    # ``MON.{session}.{trigger}.0`` (the engine-originated client_order_id
    # pattern shared by both). The prior PYTHONHASHSEED-randomized hash had
    # an unbounded collision space against the cascade's 1-based counter.
    trigger_id = trigger_ids.next()
    try:
        await submit_options_bracket_close(
            position=position,
            bracket=bracket,
            trigger_reason=trigger_reason,
            submitter=submitter,
            activity_log=activity_log,
            invocation_id_provider=invocation_id_provider,
            monitor_session_id=monitor_session_id,
            trigger_id=trigger_id,
            now=now,
            estimated_exit_price=estimated_exit_price,
            realized_pnl_usd=realized_pnl_usd,
        )
    except Exception:
        # Per-submission supervisor per runtime §G1: the submission raised —
        # keep the leg marked fired (already added to fired_legs above) to
        # avoid re-fire storms. NSSM's restart policy and the operator's
        # monitor log surface the failure for recovery. ``BaseException``
        # (``CancelledError``) propagates so supervisor shutdown bubbles up.
        log.exception(
            "bracket_stops: close submission failed for bracket %s leg %s position %s",
            bracket.bracket_id,
            leg.leg_id,
            position.position_id,
        )


# ---------------------------------------------------------------------------
# Run-forever entry point
# ---------------------------------------------------------------------------


async def run_options_bracket_watcher(  # noqa: PLR0913 — orchestrator surface dictated by parent issue ALP-123
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    position_repository: OpenPositionsReader,
    bracket_repository: BracketRepository,
    cache: UnderlyingPriceCache,
    submitter: BracketCloseSubmitter,
    activity_log: ActivityLogEmitter,
    invocation_id_provider: InvocationIdProvider,
    risk_free_rate_provider: RiskFreeRateProvider,
    trigger_ids: TriggerIdGenerator,
    now: NowProvider = lambda: datetime.now(UTC),
    sleep: SleepCallable = asyncio.sleep,
    beat: Callable[[], None] = lambda: None,
) -> None:
    """Long-running task the supervisor registers as ``bracket_stops``.

    The loop body delegates to :func:`_run_bracket_stop_cycle` so the same
    code path the tests exercise drives the production loop. The
    ``fired_legs`` set is private to this task — per-session memory of which
    leg has already fired so reads of the same trigger state in subsequent
    cycles do NOT re-fire.

    Per parent issue ALP-123 § Pre-resolved decision (I), the watcher
    submits closing orders directly via the broker adapter — no engine
    envelope — and persists the resulting ``POSITION_CLOSED`` entries with
    ``event_source=BRACKET_MANAGER``.

    ``beat`` (ALP-819), when supplied, is invoked at the top of every cycle so
    the supervisor's stall watchdog watches this stop-enforcement loop; a
    wedged cycle stops beating and trips ``os._exit(1)`` → NSSM restart.
    """
    fired_legs: set[_FiredLegKey] = set()
    while True:
        beat()
        try:
            await _run_bracket_stop_cycle(
                config=config,
                position_repository=position_repository,
                bracket_repository=bracket_repository,
                cache=cache,
                submitter=submitter,
                activity_log=activity_log,
                invocation_id_provider=invocation_id_provider,
                trigger_ids=trigger_ids,
                monitor_session_id=session.session_id,
                now=now(),
                risk_free_rate=await risk_free_rate_provider(),
                fired_legs=fired_legs,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Per-cycle supervisor per runtime §G1: the cycle should not raise
            # — each per-position branch handles its own failures. A raise
            # here indicates a programming bug; log and continue so the loop
            # survives transient consistency issues. ``BaseException``
            # (``CancelledError``) re-raised above; other ``BaseException``
            # subclasses propagate through this branch as it catches only
            # ``Exception``.
            log.exception("bracket_stops cycle raised; continuing after sleep")
        await sleep(float(config.bracket_stop_evaluation_cadence_seconds))
