"""Tests for the Phase 2 OPEN write path's position builder (ALP-594).

Story 01c extends ``_build_pending_position`` so a :class:`StrategyInstrument`
produces a PENDING strategy :class:`PositionRecord` carrying a
:class:`StrategyPositionDetails` skeleton — replacing the prior
``NotImplementedError``. Payoff metrics are zeroed here; story 02 recomputes
them from the filled legs.
"""

from __future__ import annotations

from alphamind._kernel.money import price
from alphamind.commands.command_models import (
    EquityInstrument,
    StrategyInstrument,
    Target,
)
from alphamind.commands.command_models import (
    StrategyLeg as WireStrategyLeg,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.write_paths.phase2.open import (
    _build_pending_bracket,
    _build_pending_position,
    _direction_from_instrument,
)
from alphamind.portfolio_state.records.orders import (
    BracketLegType,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionRecord,
    PositionStatus,
    StrategyPositionDetails,
)


def _vertical_spread() -> StrategyInstrument:
    """A two-leg vertical call spread: long the 800 strike, short the 810."""
    return StrategyInstrument(
        asset_type="strategy",
        strategy_type="vertical_spread",
        underlying="NVDA",
        legs=(
            WireStrategyLeg(
                strike=price(800.0),
                expiration="2026-06-19",
                contract_type="call",
                direction="long",
                quantity_ratio=1,
            ),
            WireStrategyLeg(
                strike=price(810.0),
                expiration="2026-06-19",
                contract_type="call",
                direction="short",
                quantity_ratio=1,
            ),
        ),
    )


def _build_strategy_position(
    instrument: StrategyInstrument | None = None,
) -> PositionRecord:
    """Build a PENDING strategy position through ``_build_pending_position``."""
    return _build_pending_position(
        position_id="POS-NVDA-abc123",
        thesis_id="THE-NVDA-abc123",
        bracket_id="BRK-NVDA-abc123",
        instrument=instrument if instrument is not None else _vertical_spread(),
        direction=Direction.LONG,
    )


def test_strategy_instrument_yields_strategy_position_details() -> None:
    """A StrategyInstrument no longer raises NotImplementedError — it lands a
    PositionRecord carrying StrategyPositionDetails."""
    record = _build_strategy_position()

    assert isinstance(record, PositionRecord)
    assert isinstance(record.details, StrategyPositionDetails)


def test_strategy_has_one_leg_per_wire_leg_with_deterministic_ids() -> None:
    """One record-form StrategyLeg per wire leg, each with a deterministic
    {position_id}-leg-{idx} leg_id and the wire leg's direction carried
    through."""
    record = _build_strategy_position()
    details = record.details
    assert isinstance(details, StrategyPositionDetails)

    assert len(details.legs) == 2
    assert details.legs[0].leg_id == "POS-NVDA-abc123-leg-0"
    assert details.legs[1].leg_id == "POS-NVDA-abc123-leg-1"
    assert details.legs[0].direction is Direction.LONG
    assert details.legs[1].direction is Direction.SHORT


def test_strategy_leg_options_are_zeroed_skeletons() -> None:
    """Each leg carries an OptionsPositionDetails skeleton: the wire leg's
    strike/expiration/contract_type, contract_count=0.0 and
    premium_paid_per_contract=0.0."""
    record = _build_strategy_position()
    details = record.details
    assert isinstance(details, StrategyPositionDetails)

    long_leg, short_leg = details.legs
    assert isinstance(long_leg.options, OptionsPositionDetails)
    assert long_leg.options.underlying_ticker == "NVDA"
    assert long_leg.options.strike_price == 800.0
    assert long_leg.options.expiration_date.isoformat() == "2026-06-19"
    assert long_leg.options.contract_type is OptionContractType.CALL
    assert short_leg.options.strike_price == 810.0
    for leg in details.legs:
        assert leg.options.contract_count == 0.0
        assert leg.options.premium_paid_per_contract == 0.0
        assert leg.options.contract_multiplier == LISTED_OPTION_CONTRACT_MULTIPLIER


def test_strategy_payoff_metrics_are_zeroed_on_the_skeleton() -> None:
    """net_premium_usd, max_profit_usd, max_loss_usd are 0.0 and
    breakeven_levels is empty — story 02 recomputes them from filled legs."""
    record = _build_strategy_position()
    details = record.details
    assert isinstance(details, StrategyPositionDetails)

    assert details.net_premium_usd == 0.0
    assert details.max_profit_usd == 0.0
    assert details.max_loss_usd == 0.0
    assert details.breakeven_levels == ()
    assert details.strategy_type_label == "vertical_spread"


def test_strategy_and_leg_greeks_are_zeroed() -> None:
    """strategy_greeks and every leg's greeks are zeroed OptionGreeks — the
    strategy branch does not seed greeks from validation metadata (parent
    ALP-588 § Surfacing conditions: the validation strategy greeks are a
    per-leg average, not a net)."""
    record = _build_strategy_position()
    details = record.details
    assert isinstance(details, StrategyPositionDetails)

    zeroed = OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0)
    assert details.strategy_greeks == zeroed
    for leg in details.legs:
        assert leg.options.greeks == zeroed


def test_strategy_branch_ignores_validation_greeks_and_iv() -> None:
    """Passing non-None validation_greeks / validation_iv does not change the
    strategy skeleton — they are neither required nor consumed."""
    from alphamind.risk_guardrails.guardrail_evaluation import Greeks

    record = _build_pending_position(
        position_id="POS-NVDA-abc123",
        thesis_id="THE-NVDA-abc123",
        bracket_id="BRK-NVDA-abc123",
        instrument=_vertical_spread(),
        direction=Direction.LONG,
        validation_greeks=Greeks(delta=0.5, gamma=0.02, theta=-0.1, vega=0.3),
        validation_iv=0.45,
    )
    details = record.details
    assert isinstance(details, StrategyPositionDetails)

    zeroed = OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0)
    assert details.strategy_greeks == zeroed
    for leg in details.legs:
        assert leg.options.greeks == zeroed


def test_strategy_position_is_pending_and_passes_post_init() -> None:
    """The built strategy PositionRecord is PENDING with empty
    execution_history — PositionRecord.__post_init__ accepts it."""
    record = _build_strategy_position()

    assert record.status is PositionStatus.PENDING
    assert record.execution_history == ()
    assert record.entry_timestamp is None
    assert record.realized_pnl_to_date_usd is None


def test_direction_from_strategy_instrument_is_inert_long_placeholder() -> None:
    """_direction_from_instrument returns Direction.LONG for a strategy and its
    docstring states this is an inert placeholder, not a meaningful
    direction."""
    assert _direction_from_instrument(_vertical_spread()) is Direction.LONG
    assert _direction_from_instrument.__doc__ is not None
    docstring = " ".join(_direction_from_instrument.__doc__.split())
    assert "inert placeholder" in docstring


# ---------------------------------------------------------------------------
# Strategy take-profit bracket build (ALP-601)
# ---------------------------------------------------------------------------


def _pl_percentage_target() -> Target:
    """A P/L-percentage take-profit: capture 80% of the strategy's profit."""
    return Target(
        target_type="pl_percentage",
        price=price(3.0),
        pl_percentage=80.0,
        order_type="limit",
    )


def _build_strategy_bracket(instrument: StrategyInstrument | None = None) -> object:
    return _build_pending_bracket(
        bracket_id="BRK-NVDA-abc123",
        position_id="POS-NVDA-abc123",
        ticker="NVDA",
        entry_order_id="ORD-NVDA-entry-abc123",
        target=_pl_percentage_target(),
        target_order_id="ORD-NVDA-target-abc123",
        invalidation_leg_orders=(),
        instrument=instrument if instrument is not None else _vertical_spread(),
    )


def test_strategy_bracket_take_profit_leg_carries_pl_anchor() -> None:
    """A strategy TAKE_PROFIT leg carries a PLAnchorSpec — the representation
    the strategy net-P/L evaluator consumes — with pct = pl_percentage / 100."""
    bracket = _build_strategy_bracket()
    target_leg = bracket.protective_legs[0]  # type: ignore[attr-defined]

    assert target_leg.leg_type is BracketLegType.TAKE_PROFIT
    assert target_leg.pl_anchor is not None
    assert target_leg.pl_anchor.spec_type == "target"
    assert target_leg.pl_anchor.pct == 0.80


def test_strategy_take_profit_does_not_apply_hard_coded_long_direction() -> None:
    """The hard-coded-LONG GTE PriceTrigger direction from _target_to_bracket_leg
    is NOT the firing logic for a strategy take-profit — a strategy take-profit
    references the strategy's net P/L (parent ALP-588 decision F). The leg's
    PLAnchorSpec, not the PriceTrigger direction, drives firing."""
    bracket = _build_strategy_bracket()
    target_leg = bracket.protective_legs[0]  # type: ignore[attr-defined]

    # A pl_anchor on the leg means the strategy net-P/L evaluator scores it;
    # the structurally-required PriceTrigger's direction is inert.
    assert target_leg.pl_anchor is not None
    assert isinstance(target_leg.trigger, PriceTrigger)


def test_single_leg_bracket_take_profit_build_unchanged() -> None:
    """Regression — an equity OPEN's take-profit leg keeps its plain
    underlying-price PriceTrigger with no pl_anchor (single-leg / equity build
    is out of scope for ALP-601)."""
    equity = EquityInstrument(asset_type="equity", ticker="AAPL", direction="long")
    bracket = _build_pending_bracket(
        bracket_id="BRK-AAPL-abc123",
        position_id="POS-AAPL-abc123",
        ticker="AAPL",
        entry_order_id="ORD-AAPL-entry-abc123",
        target=Target(target_type="absolute_price", price=price(160.0), order_type="limit"),
        target_order_id="ORD-AAPL-target-abc123",
        invalidation_leg_orders=(),
        instrument=equity,
    )
    target_leg = bracket.protective_legs[0]
    assert target_leg.leg_type is BracketLegType.TAKE_PROFIT
    assert target_leg.pl_anchor is None
    assert isinstance(target_leg.trigger, PriceTrigger)
    assert target_leg.trigger.direction == "GTE"
    assert target_leg.trigger.threshold_usd == 160.0
