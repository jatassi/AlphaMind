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
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from alphamind._kernel.money import DECIMAL_ZERO, Money, Price, money, price, signed_money
from alphamind.config.models.execution import OrderType, PaperHarness
from alphamind.decision.strategist.models import (
    CloseParameters,
    PositionAssessment,
    ReduceParameters,
)
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg
from alphamind.execution.counterfactual_replay_engine.option_replay import (
    price_option_at_underlying_bar,
)
from alphamind.execution.counterfactual_replay_engine.repos import (
    OhlcvBar,
    OptionsSnapshotRepository,
)
from alphamind.execution.paper_evaluation_harness.harness import (
    compute_live_execution_estimate,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
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
    "StrategistActionResult",
    "replay_strategist_proposal",
]

# A one-shot close fills at the open of the bar *following* the proposal bar
# (market close at proposal time): bars[0] contains the proposal, bars[1] is the
# fill bar — the same window contract the analyst entry simulators use.
_MIN_BARS_FOR_CLOSE = 2

_EQUITY_MULTIPLIER = Decimal(1)


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
    if action == "close":
        assert isinstance(params, CloseParameters)
        return _replay_close_or_reduce(
            state,
            bars,
            effective_quantity=_close_quantity(params, state),
            iv_repo=iv_repo,
            paper_harness_config=paper_harness_config,
            risk_free_rate=risk_free_rate,
            adv=adv,
            realized_volatility=realized_volatility,
        )
    if action == "reduce":
        assert isinstance(params, ReduceParameters)
        return _replay_close_or_reduce(
            state,
            bars,
            effective_quantity=params.quantity,
            iv_repo=iv_repo,
            paper_harness_config=paper_harness_config,
            risk_free_rate=risk_free_rate,
            adv=adv,
            realized_volatility=realized_volatility,
        )
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
    paper_harness_config: PaperHarness,
    risk_free_rate: float,
    adv: float | None,
    realized_volatility: float | None,
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
        config=paper_harness_config,
        adv=adv,
        realized_volatility=realized_volatility,
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
    config: PaperHarness,
    adv: float | None,
    realized_volatility: float | None,
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
        adv_shares=adv,
        realized_volatility=realized_volatility,
        config=config,
    )
    if estimate is None:
        return money(DECIMAL_ZERO), money(DECIMAL_ZERO)
    slippage = money(estimate.estimated_spread_usd + estimate.estimated_impact_usd)
    return slippage, estimate.estimated_regulatory_fees_usd


def _data_missing_sentinel() -> StrategistActionResult:
    """Build the data-missing sentinel (``realized_pl=None``) for the driver."""
    return StrategistActionResult(
        entered=True,
        entry_price=None,
        entry_timestamp=None,
        entry_slippage=None,
        entry_fees=None,
        exit_leg=ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL,
        exit_price=None,
        exit_timestamp=None,
        exit_slippage=None,
        exit_fees=None,
        realized_pl=None,
        same_bar_ambiguity=False,
    )
