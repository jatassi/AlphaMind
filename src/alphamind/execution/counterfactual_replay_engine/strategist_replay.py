"""Per-proposal strategist position-action replay (ALP-563, design Steps 2-4).

Replays a strategist :class:`PositionAssessment` whose ``recommended_action`` is
``close`` / ``reduce`` / ``adjust-bracket`` / ``add`` against the actual
underlying bar path, plus the narrower :class:`PendingOrderAssessment`
``cancel`` / ``modify`` surface. Each per-action simulator consumes the hydrated
proposal, the as-of :class:`PositionStateSnapshot` (story 07 §1), and the bar
stream over the replay window, and returns a unified
:class:`StrategistActionResult` the engine driver (story 08) folds into a
``CounterfactualReplayRecord`` — the field set mirrors
:class:`~.equity_replay.EquityReplayResult` /
:class:`~.option_replay.OptionReplayResult` so the driver maps straight through.

Counterfactual framing per action (each is "what if the PM had let the
strategist proposal go through?"):

* **CLOSE / REDUCE** — a one-shot exit at the proposal-following bar's open. No
  bracket walk; ``exit_leg = STRATEGIST_CLOSE_AT_PROPOSAL``. Only the close side
  carries drag — the position's entry already happened and is not re-charged.
* **ADJUST-BRACKET** — re-walk the bracket simulation from ``as_of`` forward with
  the proposed absolute target / stop / time levels. The position is already
  open at its ``average_cost_basis``, so only the exit side carries drag.
* **ADD** — simulate a fresh entry for the added quantity, then walk its
  brackets; P/L is on the added portion only, with both entry and exit drag.

ADJUST-BRACKET and ADD reuse the analyst-side walkers
(:func:`~.equity_replay.simulate_equity_brackets` etc.) by synthesizing a
minimal analyst :class:`~alphamind.decision.analyst.models.Recommendation` from
the proposed strategist levels plus the position snapshot — the walkers are the
deep, cross-story-stable trigger primitives; the strategist-to-Recommendation
translation is the only strategist-specific seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from alphamind._kernel.ids import Symbol
from alphamind._kernel.money import DECIMAL_ZERO, Money, Price, money, price, signed_money
from alphamind.config.models.execution import OrderType, PaperHarness
from alphamind.decision.analyst.models import (
    EntryOrder,
    GuardrailValidationResult,
    InstrumentEquity,
    InstrumentOption,
    InvalidationLeg,
    InvalidationRationale,
    PositionSize,
    PriceCondition,
    Recommendation,
    Target,
    TimeCondition,
)
from alphamind.decision.strategist.models import (
    AddParameters,
    AdjustBracketParameters,
    CloseParameters,
    PendingOrderAssessment,
    PositionAssessment,
    ReduceParameters,
)
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg, UnevaluableReason
from alphamind.execution.counterfactual_replay_engine.equity_replay import (
    EquityBracketResult,
    EquityEntryResult,
    EquityReplayResult,
    replay_equity_proposal,
    simulate_equity_brackets,
    simulate_equity_entry,
)
from alphamind.execution.counterfactual_replay_engine.option_replay import (
    OptionBracketResult,
    OptionEntryResult,
    OptionReplayResult,
    price_option_at_underlying_bar,
    replay_option_proposal,
    simulate_option_brackets,
    simulate_option_entry,
)
from alphamind.execution.counterfactual_replay_engine.repos import (
    OhlcvBar,
    OptionsSnapshotRepository,
)
from alphamind.execution.paper_evaluation_harness.harness import (
    compute_live_execution_estimate,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegType,
    PriceTrigger,
    TimeTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    OptionsPositionDetails,
)

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING only: the snapshot is supplied by the caller,
    # so this module needs the type for annotations but not at runtime. The
    # runtime import would pull in ``state.repository.__init__``, which carries a
    # latent circular import (it eager-imports ``activity_log_queries`` →
    # ``invocation_context`` → back into ``activity_log_queries``); deferring it
    # keeps this engine module cold-importable regardless of import order.
    from alphamind.state.repository.position_state import PositionStateSnapshot

__all__ = [
    "PendingOrderReplayResult",
    "StrategistActionResult",
    "replay_pending_order_proposal",
    "replay_strategist_proposal",
]

# A one-shot close fills at the open of the bar *following* the proposal bar
# (market close at proposal time): bars[0] contains the proposal, bars[1] is the
# fill bar — the same window contract the analyst entry simulators use.
_MIN_BARS_FOR_CLOSE = 2

_EQUITY_MULTIPLIER = Decimal(1)

# entry_order.type → the paper harness's OrderType for a fresh ADD fill.
# stop_limit maps to the harness's stop coefficient (its only stop-like member),
# matching the analyst entry mapping.
_ENTRY_ORDER_TYPE_MAP: dict[str, OrderType] = {
    "market": OrderType.market,
    "limit": OrderType.limit,
    "stop_limit": OrderType.stop,
}

# Exit leg → the order type the exit crossed as. A target fills as a resting
# limit; a price stop as a stop; a time / strategist-close as a market close.
# Mirrors the analyst exit mapping.
_EXIT_ORDER_TYPE: dict[ExitLeg, OrderType] = {
    ExitLeg.TARGET_HIT: OrderType.limit,
    ExitLeg.STOP_HIT: OrderType.stop,
    ExitLeg.TIME_STOP_FIRED: OrderType.market,
    ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL: OrderType.market,
}


@dataclass(frozen=True, slots=True)
class _HarnessInputs:
    """The paper-harness drag inputs threaded through every per-action simulator.

    ``adv`` is shares for equity, contracts for options. Bundled so the simulator
    helpers carry one parameter instead of three; built once by the public driver
    from its keyword arguments.
    """

    config: PaperHarness
    adv: float | None
    realized_volatility: float | None


@dataclass(frozen=True, slots=True)
class StrategistActionResult:
    """The flat per-proposal strategist-action outcome the engine driver folds
    into a ``CounterfactualReplayRecord`` (story 08).

    The field set mirrors :class:`~.equity_replay.EquityReplayResult` so the
    driver maps straight through. ``entered`` is ``True`` for CLOSE / REDUCE /
    ADJUST-BRACKET (the position is already open) and reflects the add fill for
    ADD (``False`` → ``exit_leg = ENTRY_WINDOW_EXPIRED_UNFILLED``, all monetary
    fields ``None``). For CLOSE / REDUCE / ADJUST-BRACKET the entry side
    represents the already-open position, so ``entry_slippage`` / ``entry_fees``
    are zero (the entry drag was already realized in the actual trade and is not
    re-charged). ``realized_pl`` is signed (losses negative); ``None`` is the
    data-missing sentinel (a per-contract IV snapshot was unavailable), which the
    driver maps to ``DATA_MISSING``.
    """

    entered: bool
    entry_price: Price | None
    entry_timestamp: datetime | None
    entry_slippage: Money | None
    entry_fees: Money | None
    exit_leg: ExitLeg
    exit_price: Price | None
    exit_timestamp: datetime | None
    exit_slippage: Money | None
    exit_fees: Money | None
    realized_pl: Money | None
    same_bar_ambiguity: bool


def replay_strategist_proposal(
    proposal: PositionAssessment,
    state: PositionStateSnapshot,
    bars: tuple[OhlcvBar, ...],
    *,
    iv_repo: OptionsSnapshotRepository,
    paper_harness_config: PaperHarness,
    risk_free_rate: float,
    adv: float | None,
    realized_volatility: float | None,
) -> StrategistActionResult:
    """Replay one strategist position-action proposal end-to-end (Steps 2-4).

    Dispatches on ``proposal.recommended_action``. A ``hold`` action raises —
    eligibility (story 04) filters it before this point.
    """
    action = proposal.recommended_action
    params = proposal.action_parameters
    harness = _HarnessInputs(
        config=paper_harness_config, adv=adv, realized_volatility=realized_volatility
    )
    if action == "close":
        assert isinstance(params, CloseParameters)
        return _replay_close_or_reduce(
            state,
            bars,
            effective_quantity=_close_quantity(params, state),
            iv_repo=iv_repo,
            risk_free_rate=risk_free_rate,
            harness=harness,
        )
    if action == "reduce":
        assert isinstance(params, ReduceParameters)
        return _replay_close_or_reduce(
            state,
            bars,
            effective_quantity=params.quantity,
            iv_repo=iv_repo,
            risk_free_rate=risk_free_rate,
            harness=harness,
        )
    if action == "adjust-bracket":
        assert isinstance(params, AdjustBracketParameters)
        return _replay_adjust_bracket(
            params, state, bars, iv_repo=iv_repo, risk_free_rate=risk_free_rate, harness=harness
        )
    if action == "add":
        assert isinstance(params, AddParameters)
        return _replay_add(
            params, state, bars, iv_repo=iv_repo, risk_free_rate=risk_free_rate, harness=harness
        )
    # ``hold`` is filtered upstream by eligibility (story 04); reaching it here is
    # an internal contract violation, not a normal "skip this replay".
    msg = f"replay_strategist_proposal: unsupported recommended_action {action!r}"
    raise ValueError(msg)


def _close_quantity(params: CloseParameters, state: PositionStateSnapshot) -> float:
    """Resolve the effective close quantity (absolute share / contract count).

    ``quantity == "all"`` closes the whole as-of net position; otherwise the
    explicit absolute count is used.
    """
    if params.quantity == "all":
        return abs(state.net_quantity_as_of)
    return float(params.quantity)


def _replay_close_or_reduce(
    state: PositionStateSnapshot,
    bars: tuple[OhlcvBar, ...],
    *,
    effective_quantity: float,
    iv_repo: OptionsSnapshotRepository,
    risk_free_rate: float,
    harness: _HarnessInputs,
) -> StrategistActionResult:
    """One-shot exit at the proposal-following bar (CLOSE / REDUCE).

    ``exit_leg = STRATEGIST_CLOSE_AT_PROPOSAL``. ``realized_pl = (exit_price -
    average_cost_basis) * effective_quantity * direction_sign * multiplier -
    close_slippage - close_fees``; only the close side carries drag.
    """
    if len(bars) < _MIN_BARS_FOR_CLOSE:
        msg = "strategist close/reduce requires the proposal bar plus the following fill bar"
        raise ValueError(msg)

    fill_bar = bars[1]
    exit_underlying_price = fill_bar.open
    exit_timestamp = fill_bar.period_start
    instrument_type = state.details.instrument_type
    multiplier = (
        Decimal(str(state.details.contract_multiplier))
        if isinstance(state.details, OptionsPositionDetails)
        else _EQUITY_MULTIPLIER
    )

    exit_price = _exit_price_for_close(
        state,
        exit_underlying_price=exit_underlying_price,
        exit_timestamp=exit_timestamp,
        iv_repo=iv_repo,
        risk_free_rate=risk_free_rate,
    )
    if exit_price is None:
        return _data_missing_sentinel()

    exit_side: Literal["buy", "sell"] = "sell" if state.direction is Direction.LONG else "buy"
    exit_slippage, exit_fees = _side_drag(
        fill_price=exit_price,
        order_type=OrderType.market,
        side=exit_side,
        quantity=effective_quantity,
        instrument_type=instrument_type,
        harness=harness,
    )

    direction_sign = Decimal(1) if state.direction is Direction.LONG else Decimal(-1)
    gross = (
        (Decimal(exit_price) - Decimal(state.average_cost_basis))
        * Decimal(str(effective_quantity))
        * multiplier
        * direction_sign
    )
    realized = gross - exit_slippage - exit_fees

    return StrategistActionResult(
        entered=True,
        entry_price=state.average_cost_basis,
        entry_timestamp=state.opened_at,
        entry_slippage=money(DECIMAL_ZERO),
        entry_fees=money(DECIMAL_ZERO),
        exit_leg=ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL,
        exit_price=exit_price,
        exit_timestamp=exit_timestamp,
        exit_slippage=exit_slippage,
        exit_fees=exit_fees,
        realized_pl=signed_money(realized),
        same_bar_ambiguity=False,
    )


def _exit_price_for_close(
    state: PositionStateSnapshot,
    *,
    exit_underlying_price: float,
    exit_timestamp: datetime,
    iv_repo: OptionsSnapshotRepository,
    risk_free_rate: float,
) -> Price | None:
    """Resolve the close fill price.

    Equity → the underlying bar open. Option → the BS-derived premium at the
    exit-timestamp IV snapshot (``None`` when the snapshot is missing — the
    data-missing sentinel the driver maps to ``DATA_MISSING``).
    """
    details = state.details
    if not isinstance(details, OptionsPositionDetails):
        return price(str(exit_underlying_price))

    contract_ticker = iv_repo.resolve_contract_ticker(
        underlying=details.underlying_ticker,
        strike=Decimal(str(details.strike_price)),
        expiration=details.expiration_date,
        contract_type=_contract_type_literal(details),
    )
    iv = iv_repo.lookup_iv(contract_ticker=contract_ticker, target_ts=exit_timestamp)
    if iv is None:
        return None
    return price_option_at_underlying_bar(
        underlying_open=exit_underlying_price,
        strike=Decimal(str(details.strike_price)),
        expiration=details.expiration_date,
        contract_type=_contract_type_literal(details),
        bar_timestamp=exit_timestamp,
        implied_volatility=iv.implied_volatility,
        risk_free_rate=risk_free_rate,
    )


def _contract_type_literal(details: OptionsPositionDetails) -> Literal["call", "put"]:
    return "call" if details.contract_type.value == "CALL" else "put"


def _side_drag(
    *,
    fill_price: Price,
    order_type: OrderType,
    side: Literal["buy", "sell"],
    quantity: float,
    instrument_type: InstrumentType,
    harness: _HarnessInputs,
) -> tuple[Money, Money]:
    """Return ``(slippage, fees)`` for one fill side via the paper harness.

    ``slippage = estimated_spread_usd + estimated_impact_usd``;
    ``fees = estimated_regulatory_fees_usd``. A ``None`` harness estimate
    (missing ADV / realized vol) records both as zero (design Step 4). ``adv``
    is shares for equity, contracts for options.
    """
    estimate = compute_live_execution_estimate(
        fill_price=fill_price,
        fill_quantity=quantity,
        instrument_type=instrument_type,
        side=side,
        order_type=order_type,
        adv_shares=harness.adv,
        realized_volatility=harness.realized_volatility,
        config=harness.config,
    )
    if estimate is None:
        return money(DECIMAL_ZERO), money(DECIMAL_ZERO)
    slippage = money(estimate.estimated_spread_usd + estimate.estimated_impact_usd)
    return slippage, estimate.estimated_regulatory_fees_usd


def _data_missing_sentinel(
    *,
    exit_leg: ExitLeg = ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL,
) -> StrategistActionResult:
    """Build the data-missing sentinel (``realized_pl=None``) for the driver.

    ``realized_pl is None`` is the signal the driver maps to ``DATA_MISSING``;
    ``exit_leg`` is diagnostic only on the sentinel.
    """
    return StrategistActionResult(
        entered=True,
        entry_price=None,
        entry_timestamp=None,
        entry_slippage=None,
        entry_fees=None,
        exit_leg=exit_leg,
        exit_price=None,
        exit_timestamp=None,
        exit_slippage=None,
        exit_fees=None,
        realized_pl=None,
        same_bar_ambiguity=False,
    )


# ---------------------------------------------------------------------------
# ADJUST-BRACKET — re-walk the position's brackets with the proposed levels
# ---------------------------------------------------------------------------


def _replay_adjust_bracket(
    params: AdjustBracketParameters,
    state: PositionStateSnapshot,
    bars: tuple[OhlcvBar, ...],
    *,
    iv_repo: OptionsSnapshotRepository,
    risk_free_rate: float,
    harness: _HarnessInputs,
) -> StrategistActionResult:
    """Re-walk the position's brackets from ``as_of`` with the proposed levels.

    Synthesizes a minimal analyst :class:`Recommendation` carrying the proposed
    absolute target / stop / time levels (merged over the position's existing
    bracket levels for any block the proposal leaves unset), then drives the
    analyst-side walker — :func:`simulate_equity_brackets` for equity,
    :func:`simulate_option_brackets` for options. The position is already open at
    its ``average_cost_basis``, so the synthetic entry fills at that basis on the
    proposal-following bar and only the exit side carries drag.
    """
    if len(bars) < _MIN_BARS_FOR_CLOSE:
        msg = "strategist adjust-bracket requires the proposal bar plus the following bar"
        raise ValueError(msg)

    quantity = abs(state.net_quantity_as_of)
    synthetic = _synthesize_recommendation(
        state,
        quantity=quantity,
        target_price=_resolve_target(params, state),
        stop_price=_resolve_stop(params, state),
        time_deadline=_resolve_time(params, state),
    )
    entry_timestamp = bars[1].period_start

    if isinstance(state.details, OptionsPositionDetails):
        option_entry = OptionEntryResult(
            entered=True,
            entry_price=state.average_cost_basis,
            entry_timestamp=entry_timestamp,
            entry_iv_lag_minutes=0.0,
        )
        option_walk = simulate_option_brackets(
            synthetic, option_entry, bars, iv_repo=iv_repo, risk_free_rate=risk_free_rate
        )
        return _result_from_option_walk(option_walk, state, quantity=quantity, harness=harness)

    equity_entry = EquityEntryResult(
        entered=True,
        entry_price=state.average_cost_basis,
        entry_timestamp=entry_timestamp,
    )
    equity_walk = simulate_equity_brackets(synthetic, equity_entry, bars)
    return _result_from_equity_walk(equity_walk, state, quantity=quantity, harness=harness)


def _resolve_target(params: AdjustBracketParameters, state: PositionStateSnapshot) -> Decimal:
    """Proposed target, else the existing TAKE_PROFIT level."""
    if params.new_target_level is not None:
        return Decimal(params.new_target_level.price)
    existing = _existing_level(state, BracketLegType.TAKE_PROFIT)
    if existing is not None:
        return existing
    msg = (
        "adjust-bracket replay needs a target level: the proposal set no "
        "new_target_level and the position has no existing TAKE_PROFIT leg"
    )
    raise ValueError(msg)


def _resolve_stop(params: AdjustBracketParameters, state: PositionStateSnapshot) -> Decimal | None:
    """Proposed stop, else the existing PRICE_STOP level (``None`` if neither)."""
    if params.new_stop_level is not None:
        return Decimal(params.new_stop_level.trigger_price)
    return _existing_level(state, BracketLegType.PRICE_STOP)


def _resolve_time(params: AdjustBracketParameters, state: PositionStateSnapshot) -> datetime | None:
    """Proposed time expiration, else the existing TIME_EXPIRATION deadline."""
    if params.new_time_expiration is not None:
        return params.new_time_expiration
    return _existing_time(state)


def _existing_level(state: PositionStateSnapshot, leg_type: BracketLegType) -> Decimal | None:
    for leg in _legs_of_type(state, leg_type):
        if isinstance(leg.trigger, PriceTrigger):
            return Decimal(str(leg.trigger.threshold_usd))
    return None


def _existing_time(state: PositionStateSnapshot) -> datetime | None:
    for leg in _legs_of_type(state, BracketLegType.TIME_EXPIRATION):
        if isinstance(leg.trigger, TimeTrigger):
            return leg.trigger.deadline
    return None


def _legs_of_type(state: PositionStateSnapshot, leg_type: BracketLegType) -> list[BracketLeg]:
    return [
        leg
        for bracket in state.open_brackets
        for leg in bracket.protective_legs
        if leg.leg_type is leg_type
    ]


# ---------------------------------------------------------------------------
# ADD — simulate a fresh entry for the added quantity, then walk its brackets
# ---------------------------------------------------------------------------


def _replay_add(
    params: AddParameters,
    state: PositionStateSnapshot,
    bars: tuple[OhlcvBar, ...],
    *,
    iv_repo: OptionsSnapshotRepository,
    risk_free_rate: float,
    harness: _HarnessInputs,
) -> StrategistActionResult:
    """Simulate the add fill for the added quantity, then walk its brackets.

    Synthesizes a :class:`Recommendation` with the add's ``entry_order``, the
    added quantity, and the bracket levels from ``bracket_adjustment`` (when
    present) else the position's existing brackets. Reuses
    :func:`simulate_equity_entry` / :func:`simulate_option_entry` for the entry
    and the bracket walkers for the exit. P/L is on the added portion only and
    carries both entry and exit drag (it is a fresh fill). When the entry never
    fills, ``exit_leg = ENTRY_WINDOW_EXPIRED_UNFILLED`` and the monetary fields
    are ``None``.
    """
    if len(bars) < _MIN_BARS_FOR_CLOSE:
        msg = "strategist add requires the proposal bar plus the following fill bar"
        raise ValueError(msg)

    quantity = params.additional_quantity
    bracket_source = params.bracket_adjustment
    synthetic = _synthesize_recommendation(
        state,
        quantity=quantity,
        target_price=_resolve_add_target(bracket_source, state),
        stop_price=_resolve_add_stop(bracket_source, state),
        time_deadline=_resolve_add_time(bracket_source, state),
        entry_order=_synthesize_entry_order(params),
    )

    if isinstance(state.details, OptionsPositionDetails):
        option_entry = simulate_option_entry(
            synthetic, bars, iv_repo=iv_repo, risk_free_rate=risk_free_rate
        )
        option_walk = simulate_option_brackets(
            synthetic, option_entry, bars, iv_repo=iv_repo, risk_free_rate=risk_free_rate
        )
        return _result_from_option_walk(
            option_walk, state, quantity=quantity, harness=harness, entry=option_entry
        )

    equity_entry = simulate_equity_entry(synthetic, bars)
    equity_walk = simulate_equity_brackets(synthetic, equity_entry, bars)
    return _result_from_equity_walk(
        equity_walk,
        state,
        quantity=quantity,
        harness=harness,
        entry=equity_entry,
        entry_order_type=_ENTRY_ORDER_TYPE_MAP[params.entry_order.type],
    )


def _resolve_add_target(
    bracket_source: AdjustBracketParameters | None, state: PositionStateSnapshot
) -> Decimal:
    if bracket_source is not None and bracket_source.new_target_level is not None:
        return Decimal(bracket_source.new_target_level.price)
    existing = _existing_level(state, BracketLegType.TAKE_PROFIT)
    if existing is not None:
        return existing
    msg = "add replay needs a target: no bracket_adjustment target and no existing TAKE_PROFIT leg"
    raise ValueError(msg)


def _resolve_add_stop(
    bracket_source: AdjustBracketParameters | None, state: PositionStateSnapshot
) -> Decimal | None:
    if bracket_source is not None and bracket_source.new_stop_level is not None:
        return Decimal(bracket_source.new_stop_level.trigger_price)
    return _existing_level(state, BracketLegType.PRICE_STOP)


def _resolve_add_time(
    bracket_source: AdjustBracketParameters | None, state: PositionStateSnapshot
) -> datetime | None:
    if bracket_source is not None and bracket_source.new_time_expiration is not None:
        return bracket_source.new_time_expiration
    return _existing_time(state)


def _synthesize_entry_order(params: AddParameters) -> EntryOrder:
    order = params.entry_order
    return EntryOrder(
        type=order.type,
        limit_price=order.limit_price,
        stop_price=order.stop_price,
    )


# ---------------------------------------------------------------------------
# Walk → unified result, and the synthetic-Recommendation builder
# ---------------------------------------------------------------------------


def _result_from_equity_walk(
    walk: EquityBracketResult,
    state: PositionStateSnapshot,
    *,
    quantity: float,
    harness: _HarnessInputs,
    entry: EquityEntryResult | None = None,
    entry_order_type: OrderType = OrderType.market,
) -> StrategistActionResult:
    """Fold an equity bracket walk into a :class:`StrategistActionResult`.

    *entry* is the fresh-add entry (ADD) or ``None`` for the already-open
    re-walk (ADJUST-BRACKET). *entry_order_type* is the harness order type the
    fresh add fill crossed as (ignored on the re-walk). When the add never
    filled the window-expired leg is returned with all monetary fields ``None``.
    """
    if entry is not None and not entry.entered:
        return _entry_window_expired()

    assert walk.exit_price is not None
    assert walk.exit_timestamp is not None
    entry_price = entry.entry_price if entry is not None else state.average_cost_basis
    assert entry_price is not None
    entry_timestamp = entry.entry_timestamp if entry is not None else state.opened_at

    entry_slippage, entry_fees = _entry_drag(
        state,
        entry_price=entry_price,
        quantity=quantity,
        order_type=entry_order_type,
        instrument_type=InstrumentType.EQUITY,
        harness=harness,
        is_fresh_entry=entry is not None,
    )
    exit_side: Literal["buy", "sell"] = "sell" if state.direction is Direction.LONG else "buy"
    exit_slippage, exit_fees = _side_drag(
        fill_price=walk.exit_price,
        order_type=_EXIT_ORDER_TYPE[walk.exit_leg],
        side=exit_side,
        quantity=quantity,
        instrument_type=InstrumentType.EQUITY,
        harness=harness,
    )
    realized = _compose_pl(
        state,
        entry_price=entry_price,
        exit_price=walk.exit_price,
        quantity=quantity,
        multiplier=_EQUITY_MULTIPLIER,
        entry_drag=(entry_slippage, entry_fees),
        exit_drag=(exit_slippage, exit_fees),
    )
    return StrategistActionResult(
        entered=True,
        entry_price=entry_price,
        entry_timestamp=entry_timestamp,
        entry_slippage=entry_slippage,
        entry_fees=entry_fees,
        exit_leg=walk.exit_leg,
        exit_price=walk.exit_price,
        exit_timestamp=walk.exit_timestamp,
        exit_slippage=exit_slippage,
        exit_fees=exit_fees,
        realized_pl=signed_money(realized),
        same_bar_ambiguity=walk.same_bar_ambiguity,
    )


def _result_from_option_walk(
    walk: OptionBracketResult,
    state: PositionStateSnapshot,
    *,
    quantity: float,
    harness: _HarnessInputs,
    entry: OptionEntryResult | None = None,
) -> StrategistActionResult:
    """Fold an option bracket walk into a :class:`StrategistActionResult`.

    An exit-IV miss (``walk.exit_price is None``) yields the data-missing
    sentinel the driver maps to ``DATA_MISSING``.
    """
    if walk.exit_price is None:
        return _data_missing_sentinel(exit_leg=walk.exit_leg)

    assert isinstance(state.details, OptionsPositionDetails)
    multiplier = Decimal(str(state.details.contract_multiplier))
    entry_price = entry.entry_price if entry is not None else state.average_cost_basis
    entry_timestamp = entry.entry_timestamp if entry is not None else state.opened_at

    entry_slippage, entry_fees = _entry_drag(
        state,
        entry_price=entry_price,
        quantity=quantity,
        order_type=OrderType.market,
        instrument_type=InstrumentType.OPTIONS,
        harness=harness,
        is_fresh_entry=entry is not None,
    )
    exit_side: Literal["buy", "sell"] = "sell" if state.direction is Direction.LONG else "buy"
    exit_slippage, exit_fees = _side_drag(
        fill_price=walk.exit_price,
        order_type=_EXIT_ORDER_TYPE[walk.exit_leg],
        side=exit_side,
        quantity=quantity,
        instrument_type=InstrumentType.OPTIONS,
        harness=harness,
    )
    realized = _compose_pl(
        state,
        entry_price=entry_price,
        exit_price=walk.exit_price,
        quantity=quantity,
        multiplier=multiplier,
        entry_drag=(entry_slippage, entry_fees),
        exit_drag=(exit_slippage, exit_fees),
    )
    return StrategistActionResult(
        entered=True,
        entry_price=entry_price,
        entry_timestamp=entry_timestamp,
        entry_slippage=entry_slippage,
        entry_fees=entry_fees,
        exit_leg=walk.exit_leg,
        exit_price=walk.exit_price,
        exit_timestamp=walk.exit_timestamp,
        exit_slippage=exit_slippage,
        exit_fees=exit_fees,
        realized_pl=signed_money(realized),
        same_bar_ambiguity=walk.same_bar_ambiguity,
    )


def _entry_drag(
    state: PositionStateSnapshot,
    *,
    entry_price: Price,
    quantity: float,
    order_type: OrderType,
    instrument_type: InstrumentType,
    harness: _HarnessInputs,
    is_fresh_entry: bool,
) -> tuple[Money, Money]:
    """Entry-side drag — charged only on a fresh ADD fill; zero on the re-walk.

    For ADJUST-BRACKET the position's entry already happened in the actual trade
    and is not re-charged, so the entry side is zero. For ADD the added portion
    is a fresh fill, so it carries entry drag.
    """
    if not is_fresh_entry:
        return money(DECIMAL_ZERO), money(DECIMAL_ZERO)
    entry_side: Literal["buy", "sell"] = "buy" if state.direction is Direction.LONG else "sell"
    return _side_drag(
        fill_price=entry_price,
        order_type=order_type,
        side=entry_side,
        quantity=quantity,
        instrument_type=instrument_type,
        harness=harness,
    )


def _compose_pl(
    state: PositionStateSnapshot,
    *,
    entry_price: Price,
    exit_price: Price,
    quantity: float,
    multiplier: Decimal,
    entry_drag: tuple[Money, Money],
    exit_drag: tuple[Money, Money],
) -> Decimal:
    direction_sign = Decimal(1) if state.direction is Direction.LONG else Decimal(-1)
    gross = (
        (Decimal(exit_price) - Decimal(entry_price))
        * Decimal(str(quantity))
        * multiplier
        * direction_sign
    )
    entry_slippage, entry_fees = entry_drag
    exit_slippage, exit_fees = exit_drag
    return gross - entry_slippage - entry_fees - exit_slippage - exit_fees


def _entry_window_expired() -> StrategistActionResult:
    """The ADD entry never filled across the window — no exit, no P/L."""
    return StrategistActionResult(
        entered=False,
        entry_price=None,
        entry_timestamp=None,
        entry_slippage=None,
        entry_fees=None,
        exit_leg=ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED,
        exit_price=None,
        exit_timestamp=None,
        exit_slippage=None,
        exit_fees=None,
        realized_pl=None,
        same_bar_ambiguity=False,
    )


def _synthesize_recommendation(
    state: PositionStateSnapshot,
    *,
    quantity: float,
    target_price: Decimal,
    stop_price: Decimal | None,
    time_deadline: datetime | None,
    entry_order: EntryOrder | None = None,
) -> Recommendation:
    """Build a minimal analyst :class:`Recommendation` for the walkers.

    Carries only what :func:`simulate_equity_brackets` /
    :func:`simulate_option_brackets` / :func:`simulate_equity_entry` /
    :func:`simulate_option_entry` read: the instrument (with the position's
    direction), the target price, the hard price-stop and/or time invalidation
    legs, the entry order, and the sizing quantity. The remaining
    ``Recommendation`` fields are filled with inert placeholders the walkers
    never inspect.
    """
    instrument = _synthesize_instrument(state)
    invalidation_legs, invalidation_rationale = _synthesize_invalidation(
        state, stop_price=stop_price, time_deadline=time_deadline
    )
    return Recommendation(
        recommendation_id="REC-0",  # type: ignore[arg-type]
        instrument=instrument,
        underlying=_underlying_symbol(state),
        sector="tech",
        conviction_level=3,
        entry_order=entry_order if entry_order is not None else EntryOrder(type="market"),
        position_size=PositionSize(
            quantity=quantity,
            dollar_value=money("1"),
            pct_of_portfolio=0.01,
        ),
        target=Target(
            target_type="absolute_price",
            price=price(target_price),
            dollar_pl_target=signed_money(DECIMAL_ZERO),
        ),
        invalidation_legs=invalidation_legs,
        time_expectation_hours=24.0,
        guardrail_validation_result=GuardrailValidationResult(
            overall="PASS",
            per_rule=(),
            checked_at=state.opened_at,
        ),
        thesis_narrative="synthetic",
        target_rationale="synthetic",
        invalidation_rationale=invalidation_rationale,
        position_size_rationale="synthetic",
        counterarguments_acknowledged="synthetic",
    )


def _synthesize_instrument(
    state: PositionStateSnapshot,
) -> InstrumentEquity | InstrumentOption:
    direction: Literal["long", "short"] = "long" if state.direction is Direction.LONG else "short"
    details = state.details
    if isinstance(details, OptionsPositionDetails):
        return InstrumentOption(
            asset_type="option",
            underlying=details.underlying_ticker,
            strike=price(str(details.strike_price)),
            expiration=details.expiration_date,
            contract_type=_contract_type_literal(details),
            direction=direction,
        )
    assert isinstance(details, EquityPositionDetails)
    return InstrumentEquity(asset_type="equity", ticker=details.ticker, direction=direction)


def _synthesize_invalidation(
    state: PositionStateSnapshot,
    *,
    stop_price: Decimal | None,
    time_deadline: datetime | None,
) -> tuple[tuple[InvalidationLeg, ...], tuple[InvalidationRationale, ...]]:
    """Build the hard price-stop and/or time invalidation legs for the walker.

    The walker reads a hard price leg for the stop and a hard time leg for the
    time stop; at least one hard leg is required by the analyst schema. When the
    proposal has neither, a non-binding far-out time leg keeps the synthetic
    valid — the walk then resolves at the window end (the thesis-duration
    deadline) exactly as a target-only re-walk should.
    """
    underlying = _underlying_symbol(state)
    legs: list[InvalidationLeg] = []
    if stop_price is not None:
        comparator: Literal["<=", ">="] = ">=" if state.direction is Direction.SHORT else "<="
        legs.append(
            InvalidationLeg(
                leg_id="INV-1",
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=underlying,
                    comparator=comparator,
                    trigger_price=price(stop_price),
                ),
                order_parameters={"order_type": "stop"},  # type: ignore[arg-type]
            )
        )
    if time_deadline is not None:
        legs.append(
            InvalidationLeg(
                leg_id="INV-2",
                type="time",
                is_hard=True,
                condition=TimeCondition(deadline=time_deadline),
                order_parameters={"order_type": "market"},  # type: ignore[arg-type]
            )
        )
    if not legs:
        legs.append(
            InvalidationLeg(
                leg_id="INV-1",
                type="time",
                is_hard=True,
                condition=TimeCondition(deadline=_window_end_deadline(state)),
                order_parameters={"order_type": "market"},  # type: ignore[arg-type]
            )
        )
    rationale = tuple(
        InvalidationRationale(leg_id=leg.leg_id, rationale="synthetic") for leg in legs
    )
    return tuple(legs), rationale


def _window_end_deadline(state: PositionStateSnapshot) -> datetime:
    """A far-out deadline so a stop-/target-less re-walk never time-stops early.

    The walker's end-of-window fallback resolves the exit; this leg exists only
    to satisfy the analyst schema's "at least one hard leg" requirement.
    """
    return state.opened_at + timedelta(days=3650)


def _underlying_symbol(state: PositionStateSnapshot) -> Symbol:
    details = state.details
    if isinstance(details, OptionsPositionDetails):
        return details.underlying_ticker
    assert isinstance(details, EquityPositionDetails)
    return details.ticker


# ---------------------------------------------------------------------------
# PendingOrderAssessment — CANCEL / MODIFY (story 07 §4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingOrderReplayResult:
    """Outcome of a pending-order CANCEL / MODIFY replay (story 07 §4).

    CANCEL produces an evaluated ``action_result`` (the would-have-filled entry
    + bracket walk); ``unevaluable_reason`` is then ``None``. MODIFY is a v2
    simplification: a faithful re-simulation of an arbitrary modification is
    unbounded, so it is written ``UNEVALUABLE`` with
    ``unevaluable_reason = STRATEGIST_POSITION_ACTION_NOT_SUPPORTED`` and
    ``action_result = None``, mirroring the engine's other unevaluable paths.
    """

    action_result: StrategistActionResult | None
    unevaluable_reason: UnevaluableReason | None


def replay_pending_order_proposal(
    proposal: PendingOrderAssessment,
    entry_proposal: Recommendation,
    bars: tuple[OhlcvBar, ...],
    *,
    iv_repo: OptionsSnapshotRepository,
    paper_harness_config: PaperHarness,
    risk_free_rate: float,
    adv: float | None,
    realized_volatility: float | None,
) -> PendingOrderReplayResult:
    """Replay a pending-order CANCEL / MODIFY proposal (story 07 §4).

    *entry_proposal* is the pending order's own entry reconstructed as an analyst
    :class:`Recommendation` (the order was born from one), supplied by the engine
    driver (story 08) which owns the order-record read.

    CANCEL — "would the cancelled order have filled, and what would it have
    earned?" — is a thin wrapper around the analyst entry + bracket replay
    drivers (:func:`replay_equity_proposal` / :func:`replay_option_proposal`).

    MODIFY is a v2 simplification: a faithful simulation of an arbitrary
    modification is unbounded, so it is written ``UNEVALUABLE`` rather than
    half-implemented (``maintain`` is filtered upstream by story 04).
    """
    action = proposal.recommended_action
    if action == "cancel":
        return PendingOrderReplayResult(
            action_result=_replay_pending_entry(
                entry_proposal,
                bars,
                iv_repo=iv_repo,
                paper_harness_config=paper_harness_config,
                risk_free_rate=risk_free_rate,
                adv=adv,
                realized_volatility=realized_volatility,
            ),
            unevaluable_reason=None,
        )
    # MODIFY (and any non-cancel that reaches here): v2 declines a faithful
    # simulation — an arbitrary modification (new limit / trigger / deadline /
    # order type) reshapes the fill geometry in ways the entry + bracket
    # primitives cannot bound without re-deriving the order's full lifecycle.
    return PendingOrderReplayResult(
        action_result=None,
        unevaluable_reason=UnevaluableReason.STRATEGIST_POSITION_ACTION_NOT_SUPPORTED,
    )


def _replay_pending_entry(
    entry_proposal: Recommendation,
    bars: tuple[OhlcvBar, ...],
    *,
    iv_repo: OptionsSnapshotRepository,
    paper_harness_config: PaperHarness,
    risk_free_rate: float,
    adv: float | None,
    realized_volatility: float | None,
) -> StrategistActionResult:
    """Run the pending order's entry + bracket replay and fold it to the result.

    Delegates to the analyst replay drivers — an equity instrument to
    :func:`replay_equity_proposal`, an option instrument to
    :func:`replay_option_proposal` — then maps the flat analyst result into the
    unified :class:`StrategistActionResult`.
    """
    if isinstance(entry_proposal.instrument, InstrumentOption):
        option_result = replay_option_proposal(
            entry_proposal,
            bars,
            iv_repo=iv_repo,
            paper_harness_config=paper_harness_config,
            risk_free_rate=risk_free_rate,
            adv_contracts=adv,
            realized_volatility=realized_volatility,
        )
        return _from_option_replay_result(option_result)

    equity_result = replay_equity_proposal(
        entry_proposal,
        bars,
        paper_harness_config=paper_harness_config,
        adv_shares=adv,
        realized_volatility=realized_volatility,
    )
    return _from_equity_replay_result(equity_result)


def _from_equity_replay_result(result: EquityReplayResult) -> StrategistActionResult:
    return StrategistActionResult(
        entered=result.entered,
        entry_price=result.entry_price,
        entry_timestamp=result.entry_timestamp,
        entry_slippage=result.entry_slippage,
        entry_fees=result.entry_fees,
        exit_leg=result.exit_leg,
        exit_price=result.exit_price,
        exit_timestamp=result.exit_timestamp,
        exit_slippage=result.exit_slippage,
        exit_fees=result.exit_fees,
        realized_pl=result.realized_pl,
        same_bar_ambiguity=result.same_bar_ambiguity,
    )


def _from_option_replay_result(result: OptionReplayResult) -> StrategistActionResult:
    return StrategistActionResult(
        entered=result.entered,
        entry_price=result.entry_price,
        entry_timestamp=result.entry_timestamp,
        entry_slippage=result.entry_slippage,
        entry_fees=result.entry_fees,
        exit_leg=result.exit_leg,
        exit_price=result.exit_price,
        exit_timestamp=result.exit_timestamp,
        exit_slippage=result.exit_slippage,
        exit_fees=result.exit_fees,
        realized_pl=result.realized_pl,
        same_bar_ambiguity=result.same_bar_ambiguity,
    )
