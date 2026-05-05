"""Proposal translator — ALP-314.

Pure functions that map analyst ``Recommendation`` and strategist
``PositionAssessment`` records to the guardrail-evaluation library's
``ProposedDelta`` shape. These are the input adapters for the combined-set
guardrail check (story 03 consumes the translated deltas).

No resolver callbacks, no clock reads, no I/O.
"""

from __future__ import annotations

from alphamind.decision.analyst.models import (
    InstrumentEquity,
    InstrumentOption,
    InstrumentStrategy,
    Recommendation,
)
from alphamind.decision.strategist.models import (
    AddParameters,
    CloseParameters,
    PositionAssessment,
    ReduceParameters,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    AssetType,
    ContractType,
    Direction,
    ExistingPosition,
    OptionLeg,
    PortfolioStateSnapshot,
    ProposedDelta,
)

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class TranslatorError(Exception):
    """Raised when a record cannot be translated to ProposedDelta.

    Cases:
    - Hold-action PositionAssessment passed to translate_position_assessment_to_proposed_delta.
    - PositionAssessment.position_id not present in snapshot.existing_positions.
    - SHORT EQUITY recommendation with no same-ticker short in the snapshot
      (no borrow cost available; caller must seed or skip).
    """


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def translate_recommendation_to_proposed_delta(
    recommendation: Recommendation,
    *,
    snapshot: PortfolioStateSnapshot,
) -> ProposedDelta:
    """Convert an analyst Recommendation to the guardrail-evaluation library's ProposedDelta shape.

    The recommendation always represents an OPEN action — analyst recommendations
    are new entries, never additions/closes (existing-position management is the
    strategist's responsibility). The library validates this invariant.
    """
    instrument = recommendation.instrument
    direction = _direction_from_instrument(instrument)
    asset_type = _asset_type_from_str(instrument.asset_type)

    notional_usd = _notional_for_recommendation(recommendation)
    option_legs = _build_option_legs_from_recommendation(recommendation)
    daily_borrow_cost_usd = _resolve_borrow_cost_for_recommendation(
        recommendation, direction=direction, asset_type=asset_type, snapshot=snapshot
    )
    reserves_capital = recommendation.entry_order.type in ("limit", "stop_limit")

    return ProposedDelta(
        id=recommendation.recommendation_id,
        underlying=recommendation.underlying,
        sector=recommendation.sector,
        direction=direction,
        asset_type=asset_type,
        notional_usd=notional_usd,
        quantity=float(recommendation.position_size.quantity),
        option_legs=option_legs,
        action=Action.OPEN,
        existing_position_id=None,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
        reserves_capital=reserves_capital,
    )


def translate_position_assessment_to_proposed_delta(
    assessment: PositionAssessment,
    *,
    snapshot: PortfolioStateSnapshot,
) -> ProposedDelta:
    """Convert a non-hold strategist PositionAssessment to ProposedDelta shape.

    Caller is responsible for filtering hold actions before calling this function;
    a hold-action assessment raises TranslatorError. Adjust-bracket actions are
    accepted and translated to Action.ADJUST (zero-exposure-change projection).
    """
    if assessment.recommended_action == "hold":
        raise TranslatorError(
            f"hold-action PositionAssessment {assessment.assessment_id!r} cannot be "
            "translated; caller must filter holds before invoking this function"
        )

    pos_id = assessment.position_id
    existing = snapshot.existing_positions.get(pos_id)
    if existing is None:
        raise TranslatorError(
            f"PositionAssessment {assessment.assessment_id!r}: position_id {pos_id!r} "
            "not found in snapshot.existing_positions"
        )

    action = _action_from_recommended_action(assessment.recommended_action)
    notional_usd, quantity = _notional_and_quantity_for_assessment(assessment, existing=existing)

    return ProposedDelta(
        id=assessment.assessment_id,
        underlying=assessment.underlying,
        sector=assessment.sector,
        direction=existing.direction,
        asset_type=existing.asset_type,
        notional_usd=notional_usd,
        quantity=quantity,
        option_legs=None,
        action=action,
        existing_position_id=pos_id,
        daily_borrow_cost_usd=None,
        reserves_capital=False,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


_DIRECTION_MAP: dict[str, Direction] = {
    "long": Direction.LONG,
    "short": Direction.SHORT,
}

_ASSET_TYPE_MAP: dict[str, AssetType] = {
    "equity": AssetType.EQUITY,
    "option": AssetType.OPTION,
    "strategy": AssetType.STRATEGY,
}

_CONTRACT_TYPE_MAP: dict[str, ContractType] = {
    "call": ContractType.CALL,
    "put": ContractType.PUT,
}

_ACTION_MAP: dict[str, Action] = {
    "close": Action.CLOSE,
    "reduce": Action.CLOSE,  # per parent decision D
    "add": Action.ADD,
    "adjust-bracket": Action.ADJUST,
}


def _direction_from_str(direction: str) -> Direction:
    return _DIRECTION_MAP[direction]


def _direction_from_instrument(
    instrument: InstrumentEquity | InstrumentOption | InstrumentStrategy,
) -> Direction:
    """Derive the overall direction from the instrument variant.

    Equity and option carry a top-level ``direction`` field. Strategy has no
    top-level direction; the conventional direction is taken from the first leg
    (the primary leg that sets the trade's net directional bias).
    """
    if isinstance(instrument, InstrumentStrategy):
        return _direction_from_str(instrument.legs[0].direction)
    return _direction_from_str(instrument.direction)


def _asset_type_from_str(asset_type: str) -> AssetType:
    return _ASSET_TYPE_MAP[asset_type]


def _action_from_recommended_action(recommended_action: str) -> Action:
    """Map strategist recommended_action string to library Action enum.

    close → CLOSE, reduce → CLOSE (per parent decision D),
    add → ADD, adjust-bracket → ADJUST.
    """
    return _ACTION_MAP[recommended_action]


def _notional_for_recommendation(recommendation: Recommendation) -> float:
    """Resolve notional_usd from recommendation position_size.

    For options/strategy: use premium_at_risk when set, else dollar_value.
    For equity: use dollar_value.
    """
    asset_type = recommendation.instrument.asset_type
    ps = recommendation.position_size
    if asset_type in ("option", "strategy") and ps.premium_at_risk is not None:
        return ps.premium_at_risk
    return ps.dollar_value


def _build_option_legs_from_recommendation(
    recommendation: Recommendation,
) -> tuple[OptionLeg, ...] | None:
    """Build option legs from the recommendation's instrument variant.

    Returns None for equity. For option, a one-tuple. For strategy, one
    OptionLeg per strategy leg with quantity signed by leg direction.
    """
    instrument = recommendation.instrument
    qty = int(recommendation.position_size.quantity)
    if isinstance(instrument, InstrumentEquity):
        return None
    if isinstance(instrument, InstrumentOption):
        leg_sign = 1 if instrument.direction == "long" else -1
        return (
            OptionLeg(
                contract_type=_CONTRACT_TYPE_MAP[instrument.contract_type],
                strike=instrument.strike,
                expiration=instrument.expiration,
                quantity=leg_sign * qty,
            ),
        )
    # InstrumentStrategy: legs carry per-leg direction and quantity_ratio.
    legs = []
    for leg in instrument.legs:
        leg_sign = 1 if leg.direction == "long" else -1
        legs.append(
            OptionLeg(
                contract_type=_CONTRACT_TYPE_MAP[leg.contract_type],
                strike=leg.strike,
                expiration=leg.expiration,
                quantity=leg_sign * leg.quantity_ratio * qty,
            )
        )
    return tuple(legs)


def _resolve_borrow_cost_for_recommendation(
    recommendation: Recommendation,
    *,
    direction: Direction,
    asset_type: AssetType,
    snapshot: PortfolioStateSnapshot,
) -> float | None:
    """Return daily_borrow_cost_usd for SHORT EQUITY recommendations.

    Scans snapshot.existing_positions for a same-ticker SHORT EQUITY position
    and returns its daily_borrow_cost_usd. Raises TranslatorError if no such
    position exists (caller must seed snapshot or skip translation). Returns
    None for all non-short-equity cases.
    """
    if direction is not Direction.SHORT or asset_type is not AssetType.EQUITY:
        return None

    ticker = recommendation.underlying
    for pos in snapshot.existing_positions.values():
        if (
            pos.underlying == ticker
            and pos.direction is Direction.SHORT
            and pos.asset_type is AssetType.EQUITY
        ):
            return pos.daily_borrow_cost_usd

    raise TranslatorError(
        f"borrow cost unavailable for new short on {ticker!r} — "
        "caller must seed snapshot or skip translation"
    )


def _resolve_close_quantity_and_notional(
    close_params: CloseParameters,
    *,
    existing: ExistingPosition,
) -> tuple[float, float]:
    """Resolve (notional_usd, quantity) for a close action.

    ``quantity="all"`` → full existing position size.
    Numeric quantity → partial close; notional pro-rated from existing.
    """
    if close_params.quantity == "all":
        return existing.notional_usd, existing.quantity
    qty = float(close_params.quantity)
    notional = qty / existing.quantity * existing.notional_usd if existing.quantity > 0 else 0.0
    return notional, qty


def _notional_and_quantity_for_assessment(
    assessment: PositionAssessment,
    *,
    existing: ExistingPosition,
) -> tuple[float, float]:
    """Return (notional_usd, quantity) for the assessment action."""
    params = assessment.action_parameters

    if isinstance(params, CloseParameters):
        return _resolve_close_quantity_and_notional(params, existing=existing)

    if isinstance(params, ReduceParameters):
        qty = float(params.quantity)
        notional = qty / existing.quantity * existing.notional_usd if existing.quantity > 0 else 0.0
        return notional, qty

    if isinstance(params, AddParameters):
        return float(params.additional_dollar_value), float(params.additional_quantity)

    # AdjustBracketParameters — no exposure change.
    return 0.0, 0.0
