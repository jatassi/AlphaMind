"""Tests for the Phase 2 OPEN write path's position builder (ALP-594).

Story 01c extends ``_build_pending_position`` so a :class:`StrategyInstrument`
produces a PENDING strategy :class:`PositionRecord` carrying a
:class:`StrategyPositionDetails` skeleton — replacing the prior
``NotImplementedError``. Payoff metrics are zeroed here; story 02 recomputes
them from the filled legs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.money import price
from alphamind.commands.command_models import (
    ComponentType,
    EquityInstrument,
    StrategyInstrument,
    Target,
    Thesis,
)
from alphamind.commands.command_models import (
    StrategyLeg as WireStrategyLeg,
)
from alphamind.commands.command_models import (
    ThesisComponent as OMSThesisComponent,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.write_paths.phase2.open import (
    _build_active_thesis,
    _build_pending_bracket,
    _build_pending_position,
    _direction_from_instrument,
)
from alphamind.portfolio_state.records.orders import (
    BracketLegType,
    BracketRecord,
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
from alphamind.portfolio_state.records.theses import (
    ThesisComponentType,
    ThesisRecordStatus,
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
        direction=None,
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
        direction=None,
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


def test_direction_from_strategy_instrument_is_none() -> None:
    """_direction_from_instrument returns None for a strategy — a multi-leg
    strategy has no position-level direction (ALP-610)."""
    assert _direction_from_instrument(_vertical_spread()) is None


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


def _build_strategy_bracket(instrument: StrategyInstrument | None = None) -> BracketRecord:
    return _build_pending_bracket(
        bracket_id="BRK-NVDA-abc123",
        position_id="POS-NVDA-abc123",
        entry_order_id="ORD-NVDA-entry-abc123",
        target=_pl_percentage_target(),
        target_order_id="ORD-NVDA-target-abc123",
        invalidation_leg_orders=(),
        instrument=instrument if instrument is not None else _vertical_spread(),
        entry_window_deadline=None,
    )


def test_strategy_bracket_take_profit_leg_carries_pl_anchor() -> None:
    """A strategy TAKE_PROFIT leg carries a PLAnchorSpec — the representation
    the strategy net-P/L evaluator consumes — with pct = pl_percentage / 100."""
    bracket = _build_strategy_bracket()
    target_leg = bracket.protective_legs[0]

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
    target_leg = bracket.protective_legs[0]

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
        entry_order_id="ORD-AAPL-entry-abc123",
        target=Target(target_type="absolute_price", price=price(160.0), order_type="limit"),
        target_order_id="ORD-AAPL-target-abc123",
        invalidation_leg_orders=(),
        instrument=equity,
        entry_window_deadline=None,
    )
    target_leg = bracket.protective_legs[0]
    assert target_leg.leg_type is BracketLegType.TAKE_PROFIT
    assert target_leg.pl_anchor is None
    assert isinstance(target_leg.trigger, PriceTrigger)
    assert target_leg.trigger.direction == "GTE"
    assert target_leg.trigger.threshold_usd == 160.0


def test_build_pending_order_without_broker_id_carries_no_alpaca_id() -> None:
    """ALP-847 — a protective leg with no broker order carries NO broker id.

    The synthetic ``alp-{order_id}`` mint is deleted (invariant 5): with no
    ``alpaca_order_id_override`` the built order's ``alpaca_order_id`` is None
    and its chain is empty — never a placeholder. This is what renders the
    ALP-837 cancel-of-a-non-existent-order path unrepresentable.
    """
    from alphamind.execution.write_paths.phase2._shared import _build_pending_order
    from alphamind.portfolio_state.records.orders import (
        OrderClass,
        OrderDirection,
        OrderRole,
        OrderType,
        PriceParameters,
    )

    order = _build_pending_order(
        order_id="ORD-AAPL-inv0-abc123",
        position_id="POS-AAPL-abc123",
        bracket_id="BRK-AAPL-abc123",
        role=OrderRole.PRICE_STOP,
        order_class=OrderClass.OTO,
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        price_parameters=PriceParameters(stop_trigger_price=150.0),
        ticker="AAPL",
        pm_command_id="inv-1.env.0.0",
        thesis_id="THE-AAPL-abc123",
        timestamp=datetime(2026, 5, 29, 17, 30, tzinfo=UTC),
        quantity=10.0,
    )
    assert order.alpaca_order_id is None
    assert order.alpaca_order_id_chain == ()


def test_build_pending_order_with_override_carries_real_broker_id() -> None:
    """ALP-847 — a leg WITH a real broker order carries the broker's id.

    The override (the broker's real Alpaca id captured at submission) stamps
    both ``alpaca_order_id`` and the single-element chain — a leg backed by a
    Broker-Owned Fact, the broker-enforced case.
    """
    from alphamind.execution.write_paths.phase2._shared import _build_pending_order
    from alphamind.portfolio_state.records.orders import (
        OrderClass,
        OrderDirection,
        OrderRole,
        OrderType,
        PriceParameters,
    )

    order = _build_pending_order(
        order_id="ORD-AAPL-inv0-abc123",
        position_id="POS-AAPL-abc123",
        bracket_id="BRK-AAPL-abc123",
        role=OrderRole.PRICE_STOP,
        order_class=OrderClass.OTO,
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        price_parameters=PriceParameters(stop_trigger_price=150.0),
        ticker="AAPL",
        pm_command_id="inv-1.env.0.0",
        thesis_id="THE-AAPL-abc123",
        timestamp=datetime(2026, 5, 29, 17, 30, tzinfo=UTC),
        quantity=10.0,
        alpaca_order_id_override="real-broker-uuid-1234",
    )
    assert order.alpaca_order_id == "real-broker-uuid-1234"
    assert order.alpaca_order_id_chain == ("real-broker-uuid-1234",)


def test_build_pending_bracket_carries_entry_window_deadline() -> None:
    """``_build_pending_bracket`` stamps the supplied ``entry_window_deadline``
    onto the bracket (ALP-737) — the seam the OPEN writeback feeds from
    ``command.entry_window.deadline``."""
    from datetime import UTC, datetime

    equity = EquityInstrument(asset_type="equity", ticker="AAPL", direction="long")
    deadline = datetime(2026, 5, 29, 17, 30, tzinfo=UTC)
    bracket = _build_pending_bracket(
        bracket_id="BRK-AAPL-abc123",
        position_id="POS-AAPL-abc123",
        entry_order_id="ORD-AAPL-entry-abc123",
        target=Target(target_type="absolute_price", price=price(160.0), order_type="limit"),
        target_order_id="ORD-AAPL-target-abc123",
        invalidation_leg_orders=(),
        instrument=equity,
        entry_window_deadline=deadline,
    )
    assert bracket.entry_window_deadline == deadline


# ---------------------------------------------------------------------------
# Multi-component-per-type thesis (ALP-699)
# ---------------------------------------------------------------------------


def _wire_component(
    component_type: ComponentType,
    *,
    narrative: str,
    linked_leg: str = "leg-1",
    instrument_reference: str = "MRVL",
) -> OMSThesisComponent:
    return OMSThesisComponent(
        component_type=component_type,
        linked_leg=linked_leg,
        instrument_reference=instrument_reference,
        narrative=narrative,
        key_assumptions=("assumption",),
    )


def test_build_active_thesis_yields_unique_component_ids_for_multiple_same_type() -> None:
    """ALP-699 regression — a wire ``Thesis`` carrying ≥2 components of the
    same type (here, 3 ``invalidation_rationale`` components — the MRVL
    reproducer's shape) must produce ``ThesisComponent``s with distinct
    ``component_id``s so the ``thesis_components.component_id`` PRIMARY KEY
    is not violated at INSERT time."""
    thesis_id = "THE-MRVL-abc123"
    wire = Thesis(
        summary="MRVL pre-gap consolidation long",
        components=(
            _wire_component("entry_rationale", narrative="pre-gap floor break"),
            _wire_component("target_rationale", narrative="resistance retest"),
            _wire_component("invalidation_rationale", narrative="pre-gap floor"),
            _wire_component("invalidation_rationale", narrative="48h catalyst window"),
            _wire_component("invalidation_rationale", narrative="MU cross-name bearish"),
        ),
    )

    record = _build_active_thesis(
        thesis_id=thesis_id,
        position_id="POS-MRVL-abc123",
        thesis=wire,
        timestamp=datetime(2026, 5, 26, 16, 24, 54, tzinfo=UTC),
    )

    ids = [c.component_id for c in record.components]
    assert len(ids) == len(set(ids)), f"duplicate component_ids: {ids}"
    # All 5 wire components persisted; no backfill needed since each required
    # type has ≥1 wire entry.
    assert len(record.components) == 5
    invalidation_ids = [
        c.component_id
        for c in record.components
        if c.component_type is ThesisComponentType.INVALIDATION_RATIONALE
    ]
    assert len(invalidation_ids) == 3
    assert len(set(invalidation_ids)) == 3
    assert record.status is ThesisRecordStatus.ACTIVE


def test_build_active_thesis_component_id_carries_thesis_id_and_type() -> None:
    """The persisted ``component_id`` is composed from ``thesis_id`` + the
    persisted component type so it stays human-readable and traces back to
    its owning thesis — the index suffix that disambiguates same-type
    duplicates extends, not replaces, the existing scheme."""
    thesis_id = "THE-AAPL-xyz789"
    wire = Thesis(
        summary="AAPL long",
        components=(
            _wire_component("entry_rationale", narrative="strong setup"),
            _wire_component("target_rationale", narrative="resistance"),
            _wire_component("invalidation_rationale", narrative="break of support"),
        ),
    )

    record = _build_active_thesis(
        thesis_id=thesis_id,
        position_id="POS-AAPL-xyz789",
        thesis=wire,
        timestamp=datetime(2026, 5, 26, 16, 24, 54, tzinfo=UTC),
    )

    for component in record.components:
        prefix = f"{thesis_id}-{component.component_type.value.lower()}"
        assert component.component_id.startswith(prefix), (
            f"component_id {component.component_id!r} should start with {prefix!r}"
        )


def test_build_active_thesis_backfill_yields_unique_component_ids() -> None:
    """When a wire ``Thesis`` is missing required component types, the
    placeholder-backfill loop seeds them. The synthesized ``component_id``s
    must not collide with the wire-component ids that precede them."""
    thesis_id = "THE-NVDA-zzz999"
    wire = Thesis(
        summary="NVDA momentum — invalidation-only wire (entry+target backfilled)",
        components=(
            _wire_component("invalidation_rationale", narrative="floor break"),
            _wire_component("invalidation_rationale", narrative="time-stop"),
        ),
    )

    record = _build_active_thesis(
        thesis_id=thesis_id,
        position_id="POS-NVDA-zzz999",
        thesis=wire,
        timestamp=datetime(2026, 5, 26, 16, 24, 54, tzinfo=UTC),
    )

    ids = [c.component_id for c in record.components]
    assert len(ids) == len(set(ids)), f"duplicate component_ids: {ids}"
    # 2 wire invalidation components + 2 backfilled (entry + target) = 4.
    assert len(record.components) == 4
