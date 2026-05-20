"""Tests for the regime-transition breach detector (story 07).

The breach detector is a pure function that scans held positions and the
risk-budget snapshot against the new effective limits resolved for the
current invocation, and emits one ``RegimeTransitionBreach`` per breaching
(rule, position) pair (per-position rules) or one record per breaching rule
(aggregate rules). The records are the strategist's deferred-remedy input.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    RegimeTransitionState,
    RiskZone,
)
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.regime_adaptation import (
    RegimeTransitionBreach,
    RuleMetadata,
)
from alphamind.risk_guardrails.regime_adaptation.breach_detector import (
    _DEFERRED_RULE_IDS,
    _RULE_EMISSION_KIND,
    detect_regime_transition_breaches,
)

# ---------------------------------------------------------------------------
# Fixture builders — minimal portfolio shapes for breach-detector tests.
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _fill() -> PositionFill:
    return PositionFill(
        fill_timestamp=_NOW,
        fill_price=price(100.0),
        fill_quantity=10.0,
        slippage=signed_money(0.01),
        fees=money(0.5),
    )


def _equity_position(
    *,
    position_id: str,
    position_weight_pct: float,
    direction: Direction = Direction.LONG,
) -> PositionView:
    is_short = direction == Direction.SHORT
    equity_details = EquityPositionDetails(
        ticker=Symbol("AAPL"),
        share_count=10.0,
        average_cost_basis_per_share=100.0,
        borrow_rate_pct=0.5 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=500.0 if is_short else None,
    )
    delta_adjusted = 1000.0 if direction == Direction.LONG else -1000.0
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=_NOW,
        details=equity_details,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(1000.0),
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=position_weight_pct,
        position_age_hours=0.0,
        notional_exposure_usd=money(1000.0),
        delta_adjusted_exposure_usd=signed_money(delta_adjusted),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _strategy_position(
    *,
    position_id: str,
    position_weight_pct: float,
) -> PositionView:
    """Build an OPEN multi-leg STRATEGY ``PositionView``.

    A strategy record carries ``direction = None`` (ALP-610);
    ``position_direction()`` yields ``None`` for it, so the short filter must
    exclude this position.
    """
    leg = StrategyLeg(
        leg_id="leg-0",
        direction=Direction.LONG,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("AAPL"),
            strike_price=100.0,
            expiration_date=_NOW.date(),
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=5.0,
            greeks=OptionGreeks(delta=0.5, gamma=0.02, theta=-0.1, vega=0.3, as_of_timestamp=_NOW),
        ),
    )
    details = StrategyPositionDetails(
        strategy_type_label="bull_spread",
        legs=(leg,),
        net_premium_usd=-200.0,
        max_profit_usd=800.0,
        max_loss_usd=-200.0,
        breakeven_levels=(102.0,),
        strategy_greeks=OptionGreeks(
            delta=0.3, gamma=0.01, theta=-0.05, vega=0.2, as_of_timestamp=_NOW
        ),
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=_NOW,
        details=details,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(1000.0),
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=position_weight_pct,
        position_age_hours=0.0,
        notional_exposure_usd=money(1000.0),
        delta_adjusted_exposure_usd=signed_money(1000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _budget_entry(
    *,
    rule_id: str,
    current_value: float,
    limit_value: float = 100.0,
    rule_label: str = "label",
    unit: str = "pct",
) -> RiskBudgetEntry:
    headroom = limit_value - current_value
    headroom_pct = max(0.0, min(100.0, 100.0 * headroom / limit_value)) if limit_value else 0.0
    return RiskBudgetEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        current_value=current_value,
        limit_value=limit_value,
        headroom=headroom,
        headroom_pct_of_limit=headroom_pct,
        zone=RiskZone.NORMAL,
        unit=unit,
        cumulative_invocation_impact_value=0.0,
    )


def _metadata(rule_id: str, *, label: str = "Label", unit: str = "pct") -> RuleMetadata:
    return RuleMetadata(rule_id=rule_id, label=label, unit=unit)


def _full_metadata() -> dict[str, RuleMetadata]:
    """Metadata covering every rule in ``_DEFERRED_RULE_IDS`` plus a couple of
    sector-prefixed ids the tests use."""
    base = {rule_id: _metadata(rule_id) for rule_id in _DEFERRED_RULE_IDS}
    base["sector_concentration_tech"] = _metadata("sector_concentration_tech", label="Tech")
    base["sector_concentration_semis"] = _metadata("sector_concentration_semis", label="Semis")
    return base


def _empty_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _full_limits() -> dict[str, float]:
    """Effective-limits map covering every rule in ``_DEFERRED_RULE_IDS`` plus
    sector-prefixed ids the tests use."""
    base: dict[str, float] = {rule_id: 100.0 for rule_id in _DEFERRED_RULE_IDS}
    base["sector_concentration_tech"] = 100.0
    base["sector_concentration_semis"] = 100.0
    return base


def _call(**overrides: Any) -> tuple[RegimeTransitionBreach, ...]:
    """Invoke the detector with sensible defaults overridable by keyword."""
    kwargs: dict[str, Any] = {
        "held_positions": (),
        "risk_budget": _empty_budget(),
        "new_effective_limits": _full_limits(),
        "transition_state": RegimeTransitionState.TIGHTENING,
        "rule_metadata": _full_metadata(),
    }
    kwargs.update(overrides)
    return detect_regime_transition_breaches(**kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tightening_with_no_positions_and_empty_budget_returns_empty() -> None:
    """Smallest happy path: no positions, no budget entries, TIGHTENING — empty."""
    assert _call() == ()


def test_stable_transition_returns_empty_even_with_breaching_positions() -> None:
    """STABLE means no change; no regime-transition breaches can arise."""
    breaching_position = _equity_position(position_id=PositionId("POS-1"), position_weight_pct=4.5)
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    assert (
        _call(
            held_positions=(breaching_position,),
            new_effective_limits=limits,
            transition_state=RegimeTransitionState.STABLE,
        )
        == ()
    )


def test_loosening_transition_returns_empty_even_with_breaching_positions() -> None:
    """LOOSENING relaxes limits gradually; cannot create new breaches."""
    breaching_position = _equity_position(position_id=PositionId("POS-1"), position_weight_pct=4.5)
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    assert (
        _call(
            held_positions=(breaching_position,),
            new_effective_limits=limits,
            transition_state=RegimeTransitionState.LOOSENING,
        )
        == ()
    )


def test_per_position_no_breaches_when_all_under_limit() -> None:
    """Three positions all under the new effective limit → empty tuple."""
    positions = (
        _equity_position(position_id=PositionId("POS-A"), position_weight_pct=2.0),
        _equity_position(position_id=PositionId("POS-B"), position_weight_pct=3.0),
        _equity_position(position_id=PositionId("POS-C"), position_weight_pct=3.4),
    )
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    assert (
        _call(
            held_positions=positions,
            new_effective_limits=limits,
        )
        == ()
    )


def test_per_position_breach_across_multiple_positions_only_breachers_emit() -> None:
    """Three positions at 4.0%, 5.0%, 3.0% with limit 3.5% → two breach records."""
    positions = (
        _equity_position(position_id=PositionId("POS-A"), position_weight_pct=4.0),
        _equity_position(position_id=PositionId("POS-B"), position_weight_pct=5.0),
        _equity_position(position_id=PositionId("POS-C"), position_weight_pct=3.0),
    )
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    breaches = _call(
        held_positions=positions,
        new_effective_limits=limits,
    )
    assert len(breaches) == 2
    breach_position_ids = {b.position_id for b in breaches}
    assert breach_position_ids == {"POS-A", "POS-B"}
    for b in breaches:
        assert b.rule_id == "position_max_size_pct"
        assert b.new_limit_value == 3.5


def test_per_position_breach_emits_one_record_with_position_id() -> None:
    """One position at 4.5% with new effective limit 3.5% → one breach record."""
    breaching = _equity_position(position_id=PositionId("POS-NVDA"), position_weight_pct=4.5)
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    metadata = _full_metadata()
    metadata["position_max_size_pct"] = _metadata(
        "position_max_size_pct", label="Per-position max size", unit="pct"
    )
    breaches = _call(
        held_positions=(breaching,),
        new_effective_limits=limits,
        rule_metadata=metadata,
    )
    assert breaches == (
        RegimeTransitionBreach(
            position_id=PositionId("POS-NVDA"),
            rule_id="position_max_size_pct",
            rule_label="Per-position max size",
            current_value=4.5,
            new_limit_value=3.5,
            overage=1.0,
            unit="pct",
        ),
    )


def test_aggregate_sector_concentration_no_breach_when_under_limit() -> None:
    """Sector tech total at 18% with new limit 20% → no breach."""
    budget = RiskBudgetConsumption(
        entries=(_budget_entry(rule_id="sector_concentration_tech", current_value=18.0),)
    )
    limits = _full_limits()
    limits["sector_concentration_tech"] = 20.0
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=2.0),)
    assert (
        _call(
            held_positions=held,
            risk_budget=budget,
            new_effective_limits=limits,
        )
        == ()
    )


def test_net_long_aggregate_breach_emits_one_record() -> None:
    """net_long aggregate at 65% with new limit 45% → one breach, position_id=None."""
    budget = RiskBudgetConsumption(
        entries=(_budget_entry(rule_id="net_long_pct", current_value=65.0),)
    )
    limits = _full_limits()
    limits["net_long_pct"] = 45.0
    metadata = _full_metadata()
    metadata["net_long_pct"] = _metadata("net_long_pct", label="Net long", unit="pct")
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=2.0),)
    breaches = _call(
        held_positions=held,
        risk_budget=budget,
        new_effective_limits=limits,
        rule_metadata=metadata,
    )
    assert breaches == (
        RegimeTransitionBreach(
            position_id=None,
            rule_id="net_long_pct",
            rule_label="Net long",
            current_value=65.0,
            new_limit_value=45.0,
            overage=20.0,
            unit="pct",
        ),
    )


def test_gross_exposure_aggregate_breach_emits_one_record() -> None:
    budget = RiskBudgetConsumption(
        entries=(_budget_entry(rule_id="gross_exposure_pct", current_value=180.0),)
    )
    limits = _full_limits()
    limits["gross_exposure_pct"] = 150.0
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=2.0),)
    breaches = _call(
        held_positions=held,
        risk_budget=budget,
        new_effective_limits=limits,
    )
    assert len(breaches) == 1
    assert breaches[0].rule_id == "gross_exposure_pct"
    assert breaches[0].position_id is None
    assert breaches[0].overage == pytest.approx(30.0)


def test_single_short_max_breach_emits_per_position_record_only_for_shorts() -> None:
    """Only short positions are evaluated against ``single_short_max_pct``.

    A long position sized 5% is NOT flagged by this rule; a short position
    sized 5% IS flagged.
    """
    long_at_five = _equity_position(
        position_id=PositionId("POS-LONG"), position_weight_pct=5.0, direction=Direction.LONG
    )
    short_at_five = _equity_position(
        position_id=PositionId("POS-SHORT"), position_weight_pct=5.0, direction=Direction.SHORT
    )
    limits = _full_limits()
    # Set position_max_size_pct above 5 so the LONG position doesn't trip on
    # *that* rule and confuse this test.
    limits["position_max_size_pct"] = 10.0
    limits["single_short_max_pct"] = 3.0
    breaches = _call(
        held_positions=(long_at_five, short_at_five),
        new_effective_limits=limits,
    )
    assert len(breaches) == 1
    assert breaches[0].rule_id == "single_short_max_pct"
    assert breaches[0].position_id == "POS-SHORT"
    assert breaches[0].current_value == pytest.approx(5.0)
    assert breaches[0].overage == pytest.approx(2.0)


def test_single_short_max_no_breach_when_short_under_limit() -> None:
    short_compliant = _equity_position(
        position_id=PositionId("POS-SHORT"), position_weight_pct=2.0, direction=Direction.SHORT
    )
    limits = _full_limits()
    limits["single_short_max_pct"] = 3.0
    assert (
        _call(
            held_positions=(short_compliant,),
            new_effective_limits=limits,
        )
        == ()
    )


def test_single_short_max_excludes_strategy_position() -> None:
    """AC: a strategy position is never counted in the short-position set.

    The held-position short filter reads direction via ``position_direction()``,
    which returns ``None`` for a strategy — so a strategy is excluded from the
    ``single_short_max_pct`` scan even when its inert position-level placeholder
    is ``SHORT``. Reading the raw ``record.direction`` would mis-flag it.
    """
    strategy = _strategy_position(
        position_id=PositionId("POS-STRAT"),
        position_weight_pct=5.0,
    )
    limits = _full_limits()
    limits["position_max_size_pct"] = 10.0  # keep position_max_size out of the way
    limits["single_short_max_pct"] = 3.0
    breaches = _call(
        held_positions=(strategy,),
        new_effective_limits=limits,
    )
    assert all(b.rule_id != "single_short_max_pct" for b in breaches)


def test_output_sorted_by_rule_id_then_position_id_with_aggregates_first() -> None:
    """Output tuple is sorted ascending by ``(rule_id, position_id_or_empty)``.

    Aggregate records (``position_id=None``) sort before per-position records
    sharing the same ``rule_id`` because ``None`` collapses to ``""`` in the
    sort key, which sorts before any non-empty string.
    """
    # Build a portfolio that triggers four breaches across three rules:
    #  - position_max_size_pct (per-position): two positions breach
    #  - net_long_pct (aggregate): one breach
    #  - sector_concentration_tech (aggregate): one breach
    # Note: position_max_size_pct alphabetizes after net_long_pct and after
    # sector_concentration_tech.
    pos_b = _equity_position(position_id=PositionId("POS-B"), position_weight_pct=4.0)
    pos_a = _equity_position(position_id=PositionId("POS-A"), position_weight_pct=5.0)
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="net_long_pct", current_value=65.0),
            _budget_entry(rule_id="sector_concentration_tech", current_value=28.0),
        )
    )
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    limits["net_long_pct"] = 45.0
    limits["sector_concentration_tech"] = 20.0
    breaches = _call(
        held_positions=(pos_b, pos_a),
        risk_budget=budget,
        new_effective_limits=limits,
    )
    assert [(b.rule_id, b.position_id) for b in breaches] == [
        ("net_long_pct", None),
        ("position_max_size_pct", "POS-A"),
        ("position_max_size_pct", "POS-B"),
        ("sector_concentration_tech", None),
    ]


def test_excluded_rules_not_in_deferred_set() -> None:
    """Drawdown, position-loss, total-short, correlation, thesis-dependency,
    and the headroom-only rules are excluded from the regime-transition
    deferred surface."""
    excluded = {
        "daily_drawdown_pct",
        "cumulative_drawdown_pct",
        "position_max_loss_equity_pct",
        "position_max_loss_options_pct",
        "total_short_pct",
        "correlation_max",
        "thesis_dependency_flag_pct",
        "borrow_cost_budget_pct_per_day",
        "min_cash_reserve_pct",
        "pending_order_capital_pct",
        "portfolio_theta_pct",
        "portfolio_vega_pct",
    }
    assert excluded.isdisjoint(_DEFERRED_RULE_IDS)


def test_missing_rule_in_new_effective_limits_for_per_position_raises() -> None:
    """If ``new_effective_limits`` is missing a per-position deferred rule, the
    detector raises ValueError mentioning the rule id."""
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=4.0),)
    limits = _full_limits()
    del limits["position_max_size_pct"]
    with pytest.raises(ValueError, match="position_max_size_pct"):
        _call(held_positions=held, new_effective_limits=limits)


def test_missing_rule_in_new_effective_limits_for_aggregate_raises() -> None:
    budget = RiskBudgetConsumption(
        entries=(_budget_entry(rule_id="net_long_pct", current_value=65.0),)
    )
    limits = _full_limits()
    del limits["net_long_pct"]
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=2.0),)
    with pytest.raises(ValueError, match="net_long_pct"):
        _call(held_positions=held, risk_budget=budget, new_effective_limits=limits)


def test_missing_rule_in_rule_metadata_raises() -> None:
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=4.0),)
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    metadata = _full_metadata()
    del metadata["position_max_size_pct"]
    with pytest.raises(ValueError, match="position_max_size_pct"):
        _call(held_positions=held, new_effective_limits=limits, rule_metadata=metadata)


def test_missing_rule_in_risk_budget_is_silently_skipped() -> None:
    """Feature-flag closure: a deferred rule simply absent from
    ``risk_budget.entries`` (e.g., ``options_delta_pct`` under
    ``options_enabled: false``) is not an error."""
    # Empty budget; aggregate scan iterates entries — there are none, so no
    # breaches and no errors. Per-position scan still applies for
    # position_max_size_pct.
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=2.0),)
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    # Verify silent skip works even when limits/metadata are present for the
    # absent rule — the detector does not insist that every deferred rule
    # appear in the budget.
    assert (
        _call(
            held_positions=held,
            risk_budget=_empty_budget(),
            new_effective_limits=limits,
        )
        == ()
    )


def test_detect_function_is_re_exported_from_package() -> None:
    """``detect_regime_transition_breaches`` is reachable via the package
    namespace per acceptance criterion 2."""
    from alphamind.risk_guardrails.regime_adaptation import (
        detect_regime_transition_breaches as exported,
    )

    assert exported is detect_regime_transition_breaches


def test_invariants_hold_for_every_emitted_record() -> None:
    """Every emitted ``RegimeTransitionBreach`` satisfies the typed-record
    invariants: ``overage > 0`` and ``overage ≈ current_value -
    new_limit_value``. The dataclass's ``__post_init__`` raises if violated;
    the test verifies that valid emissions are produced and invariants hold."""
    pos = _equity_position(position_id=PositionId("POS-A"), position_weight_pct=4.0)
    budget = RiskBudgetConsumption(
        entries=(_budget_entry(rule_id="net_long_pct", current_value=65.0),)
    )
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    limits["net_long_pct"] = 45.0
    breaches = _call(
        held_positions=(pos,),
        risk_budget=budget,
        new_effective_limits=limits,
    )
    assert len(breaches) == 2
    for b in breaches:
        assert b.overage > 0
        assert b.overage == pytest.approx(b.current_value - b.new_limit_value, abs=1e-9)


def test_aggregate_kind_records_have_none_position_id_per_position_kind_have_string() -> None:
    """Aggregate emissions carry ``position_id=None``; per-position emissions
    carry a non-empty ``position_id``."""
    pos = _equity_position(position_id=PositionId("POS-A"), position_weight_pct=4.0)
    budget = RiskBudgetConsumption(
        entries=(_budget_entry(rule_id="net_long_pct", current_value=65.0),)
    )
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    limits["net_long_pct"] = 45.0
    breaches = _call(
        held_positions=(pos,),
        risk_budget=budget,
        new_effective_limits=limits,
    )
    by_rule = {b.rule_id: b for b in breaches}
    assert by_rule["net_long_pct"].position_id is None
    assert by_rule["position_max_size_pct"].position_id == "POS-A"


def test_purity_equal_inputs_produce_equal_outputs_and_inputs_unmutated() -> None:
    """Calling the detector twice with equal inputs produces equal outputs;
    no input collection is mutated."""
    pos = _equity_position(position_id=PositionId("POS-A"), position_weight_pct=4.0)
    held = (pos,)
    budget = RiskBudgetConsumption(
        entries=(_budget_entry(rule_id="net_long_pct", current_value=65.0),)
    )
    limits = _full_limits()
    limits["position_max_size_pct"] = 3.5
    limits["net_long_pct"] = 45.0
    metadata = _full_metadata()

    held_snapshot = held
    budget_snapshot_entries = budget.entries
    limits_snapshot = dict(limits)
    metadata_snapshot = dict(metadata)

    first = _call(
        held_positions=held,
        risk_budget=budget,
        new_effective_limits=limits,
        rule_metadata=metadata,
    )
    second = _call(
        held_positions=held,
        risk_budget=budget,
        new_effective_limits=limits,
        rule_metadata=metadata,
    )
    assert first == second
    assert held is held_snapshot
    assert budget.entries is budget_snapshot_entries
    assert limits == limits_snapshot
    assert metadata == metadata_snapshot


def test_excluded_rule_in_risk_budget_does_not_emit_breach() -> None:
    """A budget entry for an excluded rule (e.g., ``daily_drawdown_pct``) at a
    breaching value does NOT produce a breach record."""
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="daily_drawdown_pct", current_value=5.0),
            _budget_entry(rule_id="cumulative_drawdown_pct", current_value=12.0),
            _budget_entry(rule_id="position_max_loss_equity_pct", current_value=35.0),
            _budget_entry(rule_id="total_short_pct", current_value=50.0),
            _budget_entry(rule_id="correlation_max", current_value=0.95),
        )
    )
    limits = _full_limits()
    # Set tight limits on excluded rules so they would *appear* to breach if
    # the detector mistakenly evaluated them.
    limits["daily_drawdown_pct"] = 2.5
    limits["cumulative_drawdown_pct"] = 8.0
    limits["position_max_loss_equity_pct"] = 30.0
    limits["total_short_pct"] = 40.0
    limits["correlation_max"] = 0.85
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=2.0),)
    assert (
        _call(
            held_positions=held,
            risk_budget=budget,
            new_effective_limits=limits,
        )
        == ()
    )


def test_net_short_aggregate_breach_emits_one_record() -> None:
    budget = RiskBudgetConsumption(
        entries=(_budget_entry(rule_id="net_short_pct", current_value=42.0),)
    )
    limits = _full_limits()
    limits["net_short_pct"] = 30.0
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=2.0),)
    breaches = _call(
        held_positions=held,
        risk_budget=budget,
        new_effective_limits=limits,
    )
    assert len(breaches) == 1
    assert breaches[0].rule_id == "net_short_pct"
    assert breaches[0].position_id is None


def test_emission_kind_classifies_each_deferred_rule_id() -> None:
    """Every rule in ``_DEFERRED_RULE_IDS`` is classified in
    ``_RULE_EMISSION_KIND``; classification is one of the two valid kinds."""
    assert set(_DEFERRED_RULE_IDS) <= set(_RULE_EMISSION_KIND.keys())
    valid_kinds = {"per_position", "aggregate"}
    for rule_id in _DEFERRED_RULE_IDS:
        assert _RULE_EMISSION_KIND[rule_id] in valid_kinds


def test_options_delta_aggregate_breach_emits_one_record() -> None:
    budget = RiskBudgetConsumption(
        entries=(_budget_entry(rule_id="options_delta_pct", current_value=42.0),)
    )
    limits = _full_limits()
    limits["options_delta_pct"] = 30.0
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=2.0),)
    breaches = _call(
        held_positions=held,
        risk_budget=budget,
        new_effective_limits=limits,
    )
    assert len(breaches) == 1
    assert breaches[0].rule_id == "options_delta_pct"
    assert breaches[0].position_id is None
    assert breaches[0].overage == pytest.approx(12.0)


def test_multiple_sectors_each_emit_one_aggregate_record() -> None:
    """Sector tech at 28%/20% AND sector semis at 18%/15% → two aggregate records."""
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="sector_concentration_tech", current_value=28.0),
            _budget_entry(rule_id="sector_concentration_semis", current_value=18.0),
        )
    )
    limits = _full_limits()
    limits["sector_concentration_tech"] = 20.0
    limits["sector_concentration_semis"] = 15.0
    held = (_equity_position(position_id=PositionId("POS-A"), position_weight_pct=2.0),)
    breaches = _call(
        held_positions=held,
        risk_budget=budget,
        new_effective_limits=limits,
    )
    rule_ids = {b.rule_id for b in breaches}
    assert rule_ids == {"sector_concentration_tech", "sector_concentration_semis"}
    for b in breaches:
        assert b.position_id is None


def test_aggregate_sector_concentration_breach_emits_one_record_with_no_position_id() -> None:
    """Sector tech total at 28% with new limit 20%; multiple contributing tech
    positions; emits exactly one record with ``position_id=None``."""
    tech_a = _equity_position(position_id=PositionId("POS-NVDA"), position_weight_pct=12.0)
    tech_b = _equity_position(position_id=PositionId("POS-AMD"), position_weight_pct=10.0)
    tech_c = _equity_position(position_id=PositionId("POS-MSFT"), position_weight_pct=6.0)
    budget = RiskBudgetConsumption(
        entries=(_budget_entry(rule_id="sector_concentration_tech", current_value=28.0),)
    )
    limits = _full_limits()
    limits["sector_concentration_tech"] = 20.0
    metadata = _full_metadata()
    metadata["sector_concentration_tech"] = _metadata(
        "sector_concentration_tech", label="Tech concentration", unit="pct"
    )
    breaches = _call(
        held_positions=(tech_a, tech_b, tech_c),
        risk_budget=budget,
        new_effective_limits=limits,
        rule_metadata=metadata,
    )
    assert breaches == (
        RegimeTransitionBreach(
            position_id=None,
            rule_id="sector_concentration_tech",
            rule_label="Tech concentration",
            current_value=28.0,
            new_limit_value=20.0,
            overage=8.0,
            unit="pct",
        ),
    )
