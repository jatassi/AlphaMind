"""Tests for the ``evaluate_proposals`` entry point (story 05).

Composes the library's primitives — feature gate, delta-adjusted exposure,
batch projection — behind one public function. Tests assert the orchestration
order, cross-field input validation, deterministic output, and the public
return shape every caller depends on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from types import MappingProxyType

import pytest

from alphamind._kernel.ids import PositionId, Symbol
from alphamind._kernel.money import money
from alphamind.risk_guardrails.guardrail_evaluation import (
    Action,
    AssetType,
    ContractType,
    Direction,
    EscalationZones,
    ExistingPosition,
    FeatureFlagsView,
    FixtureIvProvider,
    IvQuote,
    IvSource,
    IvSurfaceEntry,
    LibraryConfig,
    LibraryInputError,
    LibraryOutput,
    MarketInputs,
    OptionLeg,
    PortfolioStateSnapshot,
    ProposedDelta,
    RealizedVolEntry,
    Status,
    evaluate_proposals,
)

# ---------------------------------------------------------------------------
# Module-level fixture constants
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
_EXPIRATION = date(2026, 5, 28)  # 30 days from _AS_OF
_RISK_FREE_RATE = 0.045
_SPOT = 100.0
_IV = 0.30


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _full_config(
    *,
    active_sectors: tuple[str, ...] = ("tech", "semis", "financials", "energy"),
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
) -> LibraryConfig:
    """All 13 in-scope rules enabled (mirrors test_registry_and_project_all)."""
    effective_limits = {
        "position_max_size_pct": 10.0,
        "sector_concentration_pct": 25.0,
        "net_long_pct": 60.0,
        "net_short_pct": 40.0,
        "gross_exposure_pct": 100.0,
        "options_delta_pct": 30.0,
        "portfolio_theta_pct_per_day": 0.5,
        "portfolio_vega_pct_per_iv_point": 1.0,
        "total_short_pct": 30.0,
        "single_short_max_pct": 5.0,
        "borrow_cost_budget_pct_per_day": 0.05,
        "min_cash_reserve_pct": 10.0,
        "pending_order_capital_pct": 20.0,
    }
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: _zones() for k in effective_limits}),
        feature_flags=FeatureFlagsView(
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
        ),
        active_sectors=active_sectors,
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _snapshot(
    *,
    portfolio_value_usd: float = 100_000.0,
    sector_exposure_pct: Mapping[str, float] | None = None,
    cash_usd: float = 70_000.0,
    reserved_for_pending_orders_usd: float = 0.0,
    net_long_pct: float = 30.0,
    net_short_pct: float = 0.0,
    gross_pct: float = 78.0,
    options_delta_pct: float = 0.0,
    portfolio_theta_pct_per_day: float = 0.0,
    portfolio_vega_pct_per_iv_point: float = 0.0,
    total_short_pct: float = 0.0,
    single_short_max_pct: float = 0.0,
    daily_borrow_cost_pct: float = 0.0,
    position_max_size_pct: float = 5.0,
    existing_positions: Mapping[str, ExistingPosition] | None = None,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {
            "tech": 18.3,
            "semis": 6.0,
            "financials": 3.0,
            "energy": 3.0,
        }
    if existing_positions is None:
        # ALP-621: ``position_max_size_pct`` projection now simulates the
        # post-batch position book. A snapshot with a non-zero
        # ``position_max_size_pct`` but empty ``existing_positions`` is
        # internally inconsistent; synthesize one position sized to that pct
        # so the holistic projector finds the same max the scalar field
        # reports. Tests that need a specific book pass ``existing_positions``
        # explicitly.
        if position_max_size_pct > 0.0 and portfolio_value_usd > 0.0:
            synth_notional = position_max_size_pct / 100.0 * portfolio_value_usd
            existing_positions = {
                "POS-SYNTH": ExistingPosition(
                    position_id=PositionId("POS-SYNTH"),
                    underlying=Symbol("AAPL"),
                    sector="tech",
                    direction=Direction.LONG,
                    asset_type=AssetType.EQUITY,
                    notional_usd=synth_notional,
                    delta_adjusted_exposure_usd=synth_notional,
                    current_greeks=None,
                    daily_borrow_cost_usd=None,
                    reserves_capital_usd=0.0,
                )
            }
        else:
            existing_positions = {}
    return PortfolioStateSnapshot(
        portfolio_value_usd=portfolio_value_usd,
        cash_usd=cash_usd,
        reserved_for_pending_orders_usd=reserved_for_pending_orders_usd,
        sector_exposure_pct=MappingProxyType(dict(sector_exposure_pct)),
        net_long_pct=net_long_pct,
        net_short_pct=net_short_pct,
        gross_pct=gross_pct,
        options_delta_pct=options_delta_pct,
        portfolio_theta_pct_per_day=portfolio_theta_pct_per_day,
        portfolio_vega_pct_per_iv_point=portfolio_vega_pct_per_iv_point,
        total_short_pct=total_short_pct,
        single_short_max_pct=single_short_max_pct,
        daily_borrow_cost_pct=daily_borrow_cost_pct,
        position_max_size_pct=position_max_size_pct,
        existing_positions=MappingProxyType(dict(existing_positions)),
    )


def _atm_provider(underlying: str) -> FixtureIvProvider:
    return FixtureIvProvider(
        surface={
            underlying: IvSurfaceEntry(
                underlying=underlying,
                quotes=(
                    IvQuote(
                        strike=100.0,
                        expiration=_EXPIRATION,
                        contract_type=ContractType.CALL,
                        implied_volatility=_IV,
                    ),
                    IvQuote(
                        strike=110.0,
                        expiration=_EXPIRATION,
                        contract_type=ContractType.CALL,
                        implied_volatility=_IV,
                    ),
                    IvQuote(
                        strike=100.0,
                        expiration=_EXPIRATION,
                        contract_type=ContractType.PUT,
                        implied_volatility=_IV,
                    ),
                ),
            ),
        },
        realized_vol={},
    )


def _market(
    *,
    iv_provider: FixtureIvProvider | None = None,
    underlying_prices: Mapping[str, float] | None = None,
) -> MarketInputs:
    if iv_provider is None:
        iv_provider = _atm_provider("AAPL")
    if underlying_prices is None:
        underlying_prices = {"AAPL": _SPOT, "NVDA": _SPOT, "ABC": _SPOT}
    return MarketInputs(
        underlying_prices=underlying_prices,
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=iv_provider,
        as_of=_AS_OF,
    )


def _equity(
    *,
    proposal_id: str = "REC-1",
    underlying: str = "AAPL",
    sector: str = "tech",
    direction: Direction = Direction.LONG,
    notional_usd: float = 5_000.0,
    action: Action = Action.OPEN,
    existing_position_id: str | None = None,
    daily_borrow_cost_usd: float | None = None,
    reserves_capital: bool = False,
) -> ProposedDelta:
    return ProposedDelta(
        id=proposal_id,
        underlying=underlying,
        sector=sector,
        direction=direction,
        asset_type=AssetType.EQUITY,
        notional_usd=money(notional_usd),
        quantity=notional_usd / _SPOT,
        option_legs=None,
        action=action,
        existing_position_id=existing_position_id,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
        reserves_capital=reserves_capital,
    )


def _option(
    *,
    proposal_id: str = "REC-1",
    underlying: str = "AAPL",
    sector: str = "tech",
    direction: Direction = Direction.LONG,
    quantity: float = 5.0,
    action: Action = Action.OPEN,
    existing_position_id: str | None = None,
    asset_type: AssetType = AssetType.OPTION,
    legs: Sequence[OptionLeg] | None = None,
) -> ProposedDelta:
    if legs is None:
        legs = (
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=100.0,
                expiration=_EXPIRATION,
                quantity=1,
            ),
        )
    return ProposedDelta(
        id=proposal_id,
        underlying=underlying,
        sector=sector,
        direction=None if asset_type is AssetType.STRATEGY else direction,
        asset_type=asset_type,
        notional_usd=money(1_000.0),
        quantity=quantity,
        option_legs=tuple(legs),
        action=action,
        existing_position_id=existing_position_id,
    )


# ---------------------------------------------------------------------------
# Tracer bullet: single equity OPEN
# ---------------------------------------------------------------------------


def test_single_equity_long_open_returns_documented_per_rule_and_dae() -> None:
    """Tracer bullet: a single $5K equity LONG OPEN in tech projects through
    the full library pipeline and reports the documented shape."""
    proposal = _equity(
        proposal_id="REC-1",
        sector="tech",
        direction=Direction.LONG,
        notional_usd=5_000.0,
    )
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        sector_exposure_pct={
            "tech": 18.3,
            "semis": 6.0,
            "financials": 3.0,
            "energy": 3.0,
        },
        gross_pct=78.0,
    )
    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
    )

    assert isinstance(output, LibraryOutput)

    rule_ids = {p.rule for p in output.per_rule}
    assert "position_max_size_pct" in rule_ids
    assert "sector_concentration_tech" in rule_ids
    assert "net_long_pct" in rule_ids
    assert "gross_exposure_pct" in rule_ids
    assert "min_cash_reserve_pct" in rule_ids

    by_rule = {p.rule: p for p in output.per_rule}
    sector_tech = by_rule["sector_concentration_tech"]
    # current 18.3 + 5_000/100_000 = 23.3
    assert sector_tech.projected_after == pytest.approx(23.3)
    # consumption 23.3/25 = 93.2% → above warning (70), below hard_block (95) → WARNING.
    assert sector_tech.status is Status.WARNING

    assert set(output.delta_adjusted.keys()) == {"REC-1"}
    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.signed_notional_usd == pytest.approx(5_000.0)
    assert rec1.net_greeks is None

    assert output.feature_disabled == ()


# ---------------------------------------------------------------------------
# Multi-proposal batch with sector breach
# ---------------------------------------------------------------------------


def test_multi_proposal_batch_with_sector_breach_reports_fail_with_negative_headroom() -> None:
    """Three tech longs each $3K, sector at 23%. Combined contributions push to
    32% — over the 25% sector limit (FAIL with negative headroom)."""
    proposals = tuple(
        _equity(
            proposal_id=f"REC-{i}",
            sector="tech",
            direction=Direction.LONG,
            notional_usd=3_000.0,
        )
        for i in range(1, 4)
    )
    state = _snapshot(
        sector_exposure_pct={
            "tech": 23.0,
            "semis": 6.0,
            "financials": 3.0,
            "energy": 3.0,
        },
    )
    output = evaluate_proposals(
        state=state,
        proposals=proposals,
        config=_full_config(),
        market=_market(),
    )

    by_rule = {p.rule: p for p in output.per_rule}
    sector_tech = by_rule["sector_concentration_tech"]
    # current 23 + 3 * 3 = 32
    assert sector_tech.projected_after == pytest.approx(32.0)
    # 32 / 25 = 128% consumption ≥ hard_block (95%), so FAIL.
    assert sector_tech.status is Status.FAIL
    # headroom_remaining = limit - projected_after = 25 - 32 = -7
    assert sector_tech.headroom_remaining == pytest.approx(-7.0)


# ---------------------------------------------------------------------------
# Feature-disabled short on a no-shorts profile
# ---------------------------------------------------------------------------


def test_feature_disabled_short_appears_in_rejections_and_skips_dae() -> None:
    """Library config with ``short_selling_enabled=False``. A SHORT equity
    proposal lands in ``feature_disabled``, not in ``delta_adjusted``; per-rule
    projections compute zero contribution from the rejected proposal."""
    proposal = _equity(
        proposal_id="REC-1",
        direction=Direction.SHORT,
        notional_usd=200.0,
        daily_borrow_cost_usd=0.10,
    )
    config = _full_config(short_selling_enabled=False)
    state = _snapshot()

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=config,
        market=_market(),
    )

    assert len(output.feature_disabled) == 1
    rejection = output.feature_disabled[0]
    assert rejection.proposal_id == "REC-1"
    assert rejection.reason == "shorts_disabled"
    assert "REC-1" not in output.delta_adjusted

    # Short rules are filtered out under short_selling_enabled=False; the
    # remaining rules have current==projected_after.
    by_rule = {p.rule: p for p in output.per_rule}
    short_rules = {
        "total_short_pct",
        "single_short_max_pct",
        "borrow_cost_budget_pct_per_day",
    }
    assert short_rules.isdisjoint(by_rule.keys())
    for rule in by_rule.values():
        assert rule.projected_after == pytest.approx(rule.current)


# ---------------------------------------------------------------------------
# Options proposal under medium profile (surface IV)
# ---------------------------------------------------------------------------


def test_options_open_with_surface_iv_records_surface_source_and_greeks() -> None:
    """Single ATM call, 30 days to expiry, 5 contracts. Surface populated for
    the strike. ``delta_adjusted`` records ``iv_source=SURFACE`` with non-None
    greeks; per-rule includes the options-greek rules with non-zero
    contributions."""
    proposal = _option(
        proposal_id="REC-1",
        direction=Direction.LONG,
        quantity=5.0,
    )
    output = evaluate_proposals(
        state=_snapshot(),
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
    )

    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.net_greeks is not None
    assert rec1.iv_source is IvSource.SURFACE
    assert rec1.iv_used == pytest.approx(_IV)
    assert rec1.signed_notional_usd > 0

    by_rule = {p.rule: p for p in output.per_rule}
    assert "options_delta_pct" in by_rule
    assert "portfolio_theta_pct_per_day" in by_rule
    assert "portfolio_vega_pct_per_iv_point" in by_rule
    # The proposal contributed: options_delta moved off of zero.
    assert by_rule["options_delta_pct"].projected_after != pytest.approx(0.0)
    # Theta is signed-negative for long calls; magnitude rule classifies on |.|.
    assert by_rule["portfolio_theta_pct_per_day"].projected_after != pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Options proposal with realized-vol fallback
# ---------------------------------------------------------------------------


def test_options_open_with_realized_vol_fallback_records_fallback_source() -> None:
    """Same proposal; surface empty for the underlying; realized-vol provides
    0.30. Aggregate ``iv_source=REALIZED_VOL_FALLBACK`` and projection still
    completes."""
    provider = FixtureIvProvider(
        surface={},
        realized_vol={
            "AAPL": RealizedVolEntry(underlying=Symbol("AAPL"), trailing_30d_realized_vol=0.30),
        },
    )
    proposal = _option(
        proposal_id="REC-1",
        direction=Direction.LONG,
        quantity=5.0,
    )

    output = evaluate_proposals(
        state=_snapshot(),
        proposals=(proposal,),
        config=_full_config(),
        market=_market(iv_provider=provider),
    )

    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.iv_source is IvSource.REALIZED_VOL_FALLBACK
    assert rec1.iv_used == pytest.approx(0.30)
    # Projection still completed: every active rule appears in per_rule.
    by_rule = {p.rule: p for p in output.per_rule}
    assert "options_delta_pct" in by_rule


# ---------------------------------------------------------------------------
# Strategy proposal — long call spread
# ---------------------------------------------------------------------------


def test_strategy_long_call_spread_projects_net_delta() -> None:
    """Long call spread on NVDA: long 100C + short 110C. Net delta is positive
    and < long leg's |delta|. The options_delta_pct projection reflects the
    net (not the sum of leg |delta|s)."""
    legs = (
        OptionLeg(
            contract_type=ContractType.CALL,
            strike=100.0,
            expiration=_EXPIRATION,
            quantity=1,
        ),
        OptionLeg(
            contract_type=ContractType.CALL,
            strike=110.0,
            expiration=_EXPIRATION,
            quantity=-1,
        ),
    )
    proposal = _option(
        proposal_id="REC-1",
        underlying=Symbol("NVDA"),
        direction=Direction.LONG,
        quantity=1.0,
        asset_type=AssetType.STRATEGY,
        legs=legs,
    )
    spread_provider = FixtureIvProvider(
        surface={
            "NVDA": IvSurfaceEntry(
                underlying=Symbol("NVDA"),
                quotes=(
                    IvQuote(
                        strike=100.0,
                        expiration=_EXPIRATION,
                        contract_type=ContractType.CALL,
                        implied_volatility=_IV,
                    ),
                    IvQuote(
                        strike=110.0,
                        expiration=_EXPIRATION,
                        contract_type=ContractType.CALL,
                        implied_volatility=_IV,
                    ),
                ),
            ),
        },
        realized_vol={},
    )

    output = evaluate_proposals(
        state=_snapshot(),
        proposals=(proposal,),
        config=_full_config(),
        market=_market(iv_provider=spread_provider),
    )

    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.net_greeks is not None
    # Net delta of long call spread is positive but smaller than the front-leg delta.
    assert 0 < rec1.net_greeks.delta < 0.7
    by_rule = {p.rule: p for p in output.per_rule}
    # options_delta_pct projection reflects the net-delta-driven signed_notional.
    expected_pct = rec1.signed_notional_usd / 100_000.0 * 100.0
    assert by_rule["options_delta_pct"].projected_after == pytest.approx(expected_pct, rel=1e-9)


# ---------------------------------------------------------------------------
# CLOSE proposal
# ---------------------------------------------------------------------------


def _existing_long_equity(
    *,
    position_id: str = "POS-1",
    underlying: str = "AAPL",
    sector: str = "tech",
    notional_usd: float = 5_000.0,
) -> ExistingPosition:
    return ExistingPosition(
        position_id=position_id,
        underlying=underlying,
        sector=sector,
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=notional_usd,
        delta_adjusted_exposure_usd=notional_usd,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=0.0,
    )


def test_close_long_equity_decreases_gross_below_state_gross() -> None:
    """An existing long position is closed: ``gross_exposure_pct.projected_after
    < state.gross_pct`` (gross decreases by the position's notional in % terms)."""
    existing = _existing_long_equity(position_id=PositionId("POS-1"), notional_usd=5_000.0)
    state = _snapshot(
        gross_pct=78.0,
        sector_exposure_pct={
            "tech": 18.3,
            "semis": 6.0,
            "financials": 3.0,
            "energy": 3.0,
        },
        existing_positions={"POS-1": existing},
    )
    proposal = _equity(
        proposal_id="REC-1",
        sector="tech",
        direction=Direction.LONG,
        notional_usd=5_000.0,
        action=Action.CLOSE,
        existing_position_id="POS-1",
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
    )

    by_rule = {p.rule: p for p in output.per_rule}
    gross = by_rule["gross_exposure_pct"]
    assert gross.projected_after < state.gross_pct
    # Closing a 5K position out of 100K reduces gross by exactly 5pp.
    assert gross.projected_after == pytest.approx(73.0)
    assert by_rule["sector_concentration_tech"].projected_after < state.sector_exposure_pct["tech"]


# ---------------------------------------------------------------------------
# ADJUST proposal
# ---------------------------------------------------------------------------


def test_adjust_proposal_leaves_every_rule_unchanged() -> None:
    """ADJUST is exposure-neutral: every rule's projected_after equals current,
    and the proposal's delta-adjusted entry has signed_notional_usd=0."""
    existing = _existing_long_equity(position_id=PositionId("POS-1"), notional_usd=5_000.0)
    state = _snapshot(existing_positions={"POS-1": existing})
    proposal = _equity(
        proposal_id="REC-1",
        sector="tech",
        notional_usd=5_000.0,
        action=Action.ADJUST,
        existing_position_id="POS-1",
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
    )

    for rule in output.per_rule:
        assert rule.projected_after == pytest.approx(rule.current), rule.rule
    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.signed_notional_usd == 0.0


# ---------------------------------------------------------------------------
# CANCEL proposal
# ---------------------------------------------------------------------------


def test_cancel_pending_order_decreases_pending_order_capital_pct() -> None:
    """A CANCEL on an existing pending order with reserved capital reduces
    ``pending_order_capital_pct`` below current; other rules unchanged."""
    existing = ExistingPosition(
        position_id=PositionId("POS-1"),
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=4_000.0,
        delta_adjusted_exposure_usd=4_000.0,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=4_000.0,
    )
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        reserved_for_pending_orders_usd=4_000.0,
        existing_positions={"POS-1": existing},
    )
    proposal = _equity(
        proposal_id="REC-1",
        sector="tech",
        notional_usd=4_000.0,
        action=Action.CANCEL,
        existing_position_id="POS-1",
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
    )

    by_rule = {p.rule: p for p in output.per_rule}
    pending = by_rule["pending_order_capital_pct"]
    # current = 4_000 / 100_000 * 100 = 4.0; CANCEL releases 4.0 → projected 0.0
    assert pending.current == pytest.approx(4.0)
    assert pending.projected_after == pytest.approx(0.0)
    # Gross is unchanged by CANCEL.
    assert by_rule["gross_exposure_pct"].projected_after == pytest.approx(state.gross_pct)


# ---------------------------------------------------------------------------
# Exposure-neutral actions with zero quantity / notional
# ---------------------------------------------------------------------------


def test_adjust_with_zero_quantity_and_zero_notional_validates() -> None:
    """ADJUST is exposure-neutral by design; the validator's documented
    contract is that ``quantity > 0`` applies only to exposure-changing
    actions (OPEN/ADD/CLOSE), so producers MAY emit ``quantity=0,
    notional_usd=0`` for ADJUST and ``evaluate_proposals`` must accept it.

    Today's translator emits the existing position's totals for adjust-bracket
    (ALP-698) so the ``position_max_size_pct`` simulator's "set new total"
    branch is a no-op — but the validator gate is by-contract, not
    by-producer, so the zero/zero shape stays valid for any future producer
    (or for hand-built CANCELs).
    """
    existing = _existing_long_equity(position_id=PositionId("POS-1"), notional_usd=5_000.0)
    state = _snapshot(existing_positions={"POS-1": existing})
    proposal = ProposedDelta(
        id="REC-1",
        underlying="AAPL",
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=money(0),
        quantity=0.0,
        option_legs=None,
        action=Action.ADJUST,
        existing_position_id="POS-1",
        daily_borrow_cost_usd=None,
        reserves_capital=False,
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
    )

    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.signed_notional_usd == 0.0


def test_cancel_with_zero_quantity_and_zero_notional_validates() -> None:
    """CANCEL is exposure-neutral on the rule-projection side; the validator's
    documented contract permits ``quantity=0.0, notional_usd=0`` (parallel to
    ADJUST). The production translator does not emit CANCEL today (CANCEL is
    OMS-side), but the validator must accept the shape — the ``Action`` enum
    docstring and ``evaluate_proposals`` step 2 both treat CANCEL as a
    pass-through.

    The validator's ``quantity > 0`` rule applies only to exposure-changing
    actions (OPEN/ADD/CLOSE). CANCEL's effect on ``pending_order_capital_pct``
    comes from the existing position's ``reserves_capital_usd``, not from
    the proposal's own notional.
    """
    existing = ExistingPosition(
        position_id=PositionId("POS-1"),
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=4_000.0,
        delta_adjusted_exposure_usd=4_000.0,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=4_000.0,
    )
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        reserved_for_pending_orders_usd=4_000.0,
        existing_positions={"POS-1": existing},
    )
    proposal = ProposedDelta(
        id="REC-1",
        underlying="AAPL",
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=money(0),
        quantity=0.0,
        option_legs=None,
        action=Action.CANCEL,
        existing_position_id="POS-1",
        daily_borrow_cost_usd=None,
        reserves_capital=False,
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
    )

    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.signed_notional_usd == 0.0


def test_open_with_zero_quantity_still_raises() -> None:
    """The validator's narrowed ``quantity > 0`` rule still rejects
    exposure-changing actions with ``quantity=0`` — anchors the gating
    boundary opposite to the ADJUST/CANCEL pass-through.
    """
    state = _snapshot()
    proposal = ProposedDelta(
        id="REC-1",
        underlying="AAPL",
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=money(1_000),
        quantity=0.0,
        option_legs=None,
        action=Action.OPEN,
        existing_position_id=None,
        daily_borrow_cost_usd=None,
        reserves_capital=False,
    )
    with pytest.raises(LibraryInputError, match="quantity must be > 0 for action OPEN"):
        evaluate_proposals(
            state=state,
            proposals=(proposal,),
            config=_full_config(),
            market=_market(),
        )


# ---------------------------------------------------------------------------
# Mixed batch
# ---------------------------------------------------------------------------


def test_mixed_batch_projects_each_action_per_documented_arithmetic() -> None:
    """One OPEN-LONG-EQUITY, one OPEN-SHORT-EQUITY, one CLOSE on existing long,
    one ADD on existing short, one ADJUST. Each is reflected in projections."""
    existing_long = _existing_long_equity(
        position_id=PositionId("POS-LONG"), notional_usd=4_000.0, sector="tech"
    )
    existing_short = ExistingPosition(
        position_id=PositionId("POS-SHORT"),
        underlying=Symbol("AAPL"),
        sector="semis",
        direction=Direction.SHORT,
        asset_type=AssetType.EQUITY,
        notional_usd=2_000.0,
        delta_adjusted_exposure_usd=2_000.0,
        current_greeks=None,
        daily_borrow_cost_usd=1.0,
        reserves_capital_usd=0.0,
    )
    existing_adjust = _existing_long_equity(
        position_id=PositionId("POS-ADJUST"), notional_usd=3_000.0, sector="financials"
    )

    state = _snapshot(
        portfolio_value_usd=100_000.0,
        gross_pct=20.0,
        net_long_pct=15.0,
        net_short_pct=2.0,
        total_short_pct=2.0,
        single_short_max_pct=2.0,
        daily_borrow_cost_pct=1.0 / 100_000.0 * 100.0,
        sector_exposure_pct={
            "tech": 4.0,
            "semis": 2.0,
            "financials": 3.0,
            "energy": 0.0,
        },
        existing_positions={
            "POS-LONG": existing_long,
            "POS-SHORT": existing_short,
            "POS-ADJUST": existing_adjust,
        },
    )

    proposals = (
        # OPEN-LONG-EQUITY in energy
        _equity(
            proposal_id="REC-OPEN-LONG",
            sector="energy",
            direction=Direction.LONG,
            notional_usd=2_000.0,
        ),
        # OPEN-SHORT-EQUITY in tech (need borrow cost)
        _equity(
            proposal_id="REC-OPEN-SHORT",
            sector="tech",
            direction=Direction.SHORT,
            notional_usd=1_000.0,
            daily_borrow_cost_usd=0.50,
        ),
        # CLOSE the existing long in tech
        _equity(
            proposal_id="REC-CLOSE",
            sector="tech",
            direction=Direction.LONG,
            notional_usd=4_000.0,
            action=Action.CLOSE,
            existing_position_id="POS-LONG",
        ),
        # ADD to the existing short in semis
        _equity(
            proposal_id="REC-ADD-SHORT",
            sector="semis",
            direction=Direction.SHORT,
            notional_usd=500.0,
            action=Action.ADD,
            existing_position_id="POS-SHORT",
            daily_borrow_cost_usd=0.20,
        ),
        # ADJUST the existing financials long
        _equity(
            proposal_id="REC-ADJUST",
            sector="financials",
            direction=Direction.LONG,
            notional_usd=3_000.0,
            action=Action.ADJUST,
            existing_position_id="POS-ADJUST",
        ),
    )

    output = evaluate_proposals(
        state=state,
        proposals=proposals,
        config=_full_config(),
        market=_market(),
    )

    # Every proposal produces a delta_adjusted entry.
    assert set(output.delta_adjusted.keys()) == {
        "REC-OPEN-LONG",
        "REC-OPEN-SHORT",
        "REC-CLOSE",
        "REC-ADD-SHORT",
        "REC-ADJUST",
    }
    # ADJUST signed_notional is 0
    assert output.delta_adjusted["REC-ADJUST"].signed_notional_usd == 0.0
    # CLOSE-LONG signed_notional is negative (reverses the LONG direction)
    assert output.delta_adjusted["REC-CLOSE"].signed_notional_usd == pytest.approx(-4_000.0)
    # ADD-SHORT signed_notional is negative (SHORT direction)
    assert output.delta_adjusted["REC-ADD-SHORT"].signed_notional_usd == pytest.approx(-500.0)
    # OPEN-SHORT signed_notional is negative
    assert output.delta_adjusted["REC-OPEN-SHORT"].signed_notional_usd == pytest.approx(-1_000.0)

    by_rule = {p.rule: p for p in output.per_rule}
    # Gross: +2000 (open long) + 1000 (open short abs) - 4000 (close long) + 500 (add short abs)
    # +0 (adjust) = -500. As pp of 100K = -0.5; current 20 → projected 19.5
    assert by_rule["gross_exposure_pct"].projected_after == pytest.approx(19.5)
    # Net long: signed sum / 100K * 100 = (+2000 - 1000 - 4000 - 500 + 0) / 100K * 100 = -3.5
    # current 15 → projected 11.5
    assert by_rule["net_long_pct"].projected_after == pytest.approx(11.5)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_evaluate_proposals_is_deterministic_eq_and_hash() -> None:
    """Equal inputs produce equal outputs (``==`` and ``hash`` agree)."""
    proposal = _equity(proposal_id="REC-1", direction=Direction.LONG, notional_usd=5_000.0)
    state = _snapshot()
    config = _full_config()
    market = _market()

    first = evaluate_proposals(state=state, proposals=(proposal,), config=config, market=market)
    second = evaluate_proposals(state=state, proposals=(proposal,), config=config, market=market)

    assert first == second
    assert hash(first) == hash(second)


# ---------------------------------------------------------------------------
# Per-rule output ordering
# ---------------------------------------------------------------------------


def test_per_rule_order_is_stable_and_lexicographic() -> None:
    """``per_rule`` follows ``build_active_specs`` order (lexicographic);
    two calls return the same order."""
    proposal = _equity(proposal_id="REC-1", direction=Direction.LONG, notional_usd=1_000.0)
    state = _snapshot()
    config = _full_config()
    market = _market()

    first = evaluate_proposals(state=state, proposals=(proposal,), config=config, market=market)
    second = evaluate_proposals(state=state, proposals=(proposal,), config=config, market=market)

    rule_ids_first = [p.rule for p in first.per_rule]
    rule_ids_second = [p.rule for p in second.per_rule]
    assert rule_ids_first == rule_ids_second
    assert rule_ids_first == sorted(rule_ids_first)


# ---------------------------------------------------------------------------
# Empty proposals
# ---------------------------------------------------------------------------


def test_empty_proposals_returns_current_equals_projected_for_every_rule() -> None:
    """The validation tool's headroom-only path: ``proposals=[]`` returns
    ``delta_adjusted={}``, ``feature_disabled=()``, and ``per_rule`` with
    ``current==projected_after`` per entry."""
    output = evaluate_proposals(
        state=_snapshot(),
        proposals=(),
        config=_full_config(),
        market=_market(),
    )

    assert dict(output.delta_adjusted) == {}
    assert output.feature_disabled == ()
    assert len(output.per_rule) > 0
    for rule in output.per_rule:
        assert rule.current == pytest.approx(rule.projected_after), rule.rule


# ---------------------------------------------------------------------------
# All-feature-disabled batch
# ---------------------------------------------------------------------------


def test_all_options_proposals_under_micro_profile_only_in_feature_disabled() -> None:
    """Three options proposals on a profile with options_enabled=False: all
    three are in ``feature_disabled``, none in ``delta_adjusted``, and per-rule
    shows zero contribution."""
    proposals = tuple(
        _option(proposal_id=f"REC-{i}", direction=Direction.LONG, quantity=1.0) for i in range(1, 4)
    )
    config = _full_config(options_enabled=False)
    state = _snapshot()

    output = evaluate_proposals(
        state=state,
        proposals=proposals,
        config=config,
        market=_market(),
    )

    assert {r.proposal_id for r in output.feature_disabled} == {"REC-1", "REC-2", "REC-3"}
    assert dict(output.delta_adjusted) == {}
    for rule in output.per_rule:
        assert rule.projected_after == pytest.approx(rule.current), rule.rule


# ---------------------------------------------------------------------------
# Feature-disabled CLOSE / ADJUST / CANCEL carve-out (ALP-647)
#
# The library's feature gate is structural (asset_type + direction); the
# entry point owns the *policy* that operators must be able to unwind
# disabled-class exposure through the same library that's policing them.
# OPEN/ADD on a disabled class is still rejected; CLOSE/ADJUST/CANCEL pass
# through so the regular rules can project the unwind.
# ---------------------------------------------------------------------------


def test_close_on_held_option_under_options_disabled_passes_gate() -> None:
    """``options_enabled=False`` with a CLOSE against a held OPTION position
    must let the proposal through — operators need to unwind. The proposal
    lands in ``delta_adjusted`` and ``feature_disabled`` stays empty."""
    existing = _existing_options_position(
        position_id="POS-OPT",
        asset_type=AssetType.OPTION,
        delta_adjusted_exposure_usd=2_250.0,
    )
    state = _snapshot(existing_positions={"POS-OPT": existing})
    proposal = _close_proposal(
        proposal_id="REC-CLOSE-OPT",
        asset_type=AssetType.OPTION,
        existing_position_id="POS-OPT",
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(options_enabled=False),
        market=_market(),
    )

    assert output.feature_disabled == ()
    assert "REC-CLOSE-OPT" in output.delta_adjusted


def test_open_on_option_under_options_disabled_still_rejected() -> None:
    """The carve-out is action-scoped: an OPEN of an options proposal under
    ``options_enabled=False`` is still rejected — the operator can unwind but
    cannot create new disabled-class exposure."""
    proposal = _option(proposal_id="REC-OPEN-OPT", direction=Direction.LONG, quantity=1.0)

    output = evaluate_proposals(
        state=_snapshot(),
        proposals=(proposal,),
        config=_full_config(options_enabled=False),
        market=_market(),
    )

    assert {r.proposal_id for r in output.feature_disabled} == {"REC-OPEN-OPT"}
    assert "REC-OPEN-OPT" not in output.delta_adjusted


def test_close_on_held_short_under_shorts_disabled_passes_gate() -> None:
    """``short_selling_enabled=False`` with a CLOSE against a held SHORT equity
    position must let the proposal through. Same unwind-policy mirror of the
    options carve-out."""
    existing = ExistingPosition(
        position_id=PositionId("POS-SHORT"),
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.SHORT,
        asset_type=AssetType.EQUITY,
        notional_usd=2_000.0,
        delta_adjusted_exposure_usd=-2_000.0,
        current_greeks=None,
        daily_borrow_cost_usd=0.10,
        reserves_capital_usd=0.0,
    )
    state = _snapshot(existing_positions={"POS-SHORT": existing})
    proposal = ProposedDelta(
        id="REC-CLOSE-SHORT",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.SHORT,
        asset_type=AssetType.EQUITY,
        notional_usd=money(2_000.0),
        quantity=20.0,
        option_legs=None,
        action=Action.CLOSE,
        existing_position_id="POS-SHORT",
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(short_selling_enabled=False),
        market=_market(),
    )

    assert output.feature_disabled == ()
    assert "REC-CLOSE-SHORT" in output.delta_adjusted


def test_open_short_under_shorts_disabled_still_rejected() -> None:
    """OPEN of a SHORT equity under ``short_selling_enabled=False`` is still
    rejected — mirror of the options OPEN-still-rejected test."""
    proposal = _equity(
        proposal_id="REC-OPEN-SHORT",
        direction=Direction.SHORT,
        notional_usd=2_000.0,
        daily_borrow_cost_usd=0.10,
    )

    output = evaluate_proposals(
        state=_snapshot(),
        proposals=(proposal,),
        config=_full_config(short_selling_enabled=False),
        market=_market(),
    )

    assert {r.proposal_id for r in output.feature_disabled} == {"REC-OPEN-SHORT"}
    assert "REC-OPEN-SHORT" not in output.delta_adjusted


def test_adjust_on_held_option_under_options_disabled_passes_gate() -> None:
    """ADJUST on a held OPTION position under ``options_enabled=False`` passes
    the gate. The size/concentration rules still bound the resulting notional
    in absolute terms; the gate carve-out only opens the action seam."""
    existing = _existing_options_position(
        position_id="POS-OPT-ADJ",
        asset_type=AssetType.OPTION,
        delta_adjusted_exposure_usd=2_250.0,
    )
    state = _snapshot(existing_positions={"POS-OPT-ADJ": existing})
    proposal = ProposedDelta(
        id="REC-ADJ-OPT",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.OPTION,
        notional_usd=money(1_800.0),
        quantity=4.0,
        option_legs=None,
        action=Action.ADJUST,
        existing_position_id="POS-OPT-ADJ",
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(options_enabled=False),
        market=_market(),
    )

    assert output.feature_disabled == ()
    assert "REC-ADJ-OPT" in output.delta_adjusted


def test_cancel_on_pending_short_under_shorts_disabled_passes_gate() -> None:
    """CANCEL against a pending SHORT equity order under
    ``short_selling_enabled=False`` passes the gate. The CANCEL is
    exposure-neutral by definition; the carve-out lets the operator release
    the reserved capital after the flag flip."""
    pending = ExistingPosition(
        position_id=PositionId("POS-SHORT-PEND"),
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.SHORT,
        asset_type=AssetType.EQUITY,
        notional_usd=2_000.0,
        delta_adjusted_exposure_usd=-2_000.0,
        current_greeks=None,
        daily_borrow_cost_usd=0.10,
        reserves_capital_usd=2_000.0,
    )
    state = _snapshot(existing_positions={"POS-SHORT-PEND": pending})
    proposal = ProposedDelta(
        id="REC-CANCEL-SHORT",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.SHORT,
        asset_type=AssetType.EQUITY,
        notional_usd=money(2_000.0),
        quantity=20.0,
        option_legs=None,
        action=Action.CANCEL,
        existing_position_id="POS-SHORT-PEND",
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(short_selling_enabled=False),
        market=_market(),
    )

    assert output.feature_disabled == ()
    assert "REC-CANCEL-SHORT" in output.delta_adjusted


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_equity_with_option_legs_raises_library_input_error() -> None:
    """An EQUITY proposal that carries option_legs raises naming the proposal id."""
    bad = ProposedDelta(
        id="REC-BAD",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=money(1_000.0),
        quantity=10.0,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=100.0,
                expiration=_EXPIRATION,
                quantity=1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )

    with pytest.raises(LibraryInputError, match=r"REC-BAD.*EQUITY"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad,),
            config=_full_config(),
            market=_market(),
        )


def test_option_without_legs_raises() -> None:
    """``OPTION`` with ``option_legs=None`` raises naming the proposal id."""
    bad = ProposedDelta(
        id="REC-BAD",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.OPTION,
        notional_usd=money(1_000.0),
        quantity=1.0,
        option_legs=None,
        action=Action.OPEN,
        existing_position_id=None,
    )

    with pytest.raises(LibraryInputError, match=r"REC-BAD.*OPTION"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad,),
            config=_full_config(),
            market=_market(),
        )


def test_strategy_with_single_leg_raises() -> None:
    """``STRATEGY`` with ``len(option_legs)==1`` raises naming the proposal id."""
    bad = ProposedDelta(
        id="REC-BAD",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=None,
        asset_type=AssetType.STRATEGY,
        notional_usd=money(1_000.0),
        quantity=1.0,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=100.0,
                expiration=_EXPIRATION,
                quantity=1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )

    with pytest.raises(LibraryInputError, match=r"REC-BAD.*STRATEGY"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad,),
            config=_full_config(),
            market=_market(),
        )


def test_strategy_with_position_level_direction_raises() -> None:
    """ALP-603: a STRATEGY proposal must carry ``direction=None`` — a non-None
    position-level direction is a category error and fails the entry point."""
    bad = ProposedDelta(
        id="REC-BAD",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.STRATEGY,
        notional_usd=money(1_000.0),
        quantity=1.0,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=100.0,
                expiration=_EXPIRATION,
                quantity=1,
            ),
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=110.0,
                expiration=_EXPIRATION,
                quantity=-1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )

    with pytest.raises(LibraryInputError, match=r"REC-BAD.*STRATEGY must not carry"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad,),
            config=_full_config(),
            market=_market(),
        )


def test_equity_without_direction_raises() -> None:
    """ALP-603: an EQUITY proposal must carry a non-None ``direction`` — the
    optional shape is reserved for a multi-leg STRATEGY."""
    bad = ProposedDelta(
        id="REC-BAD",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=None,
        asset_type=AssetType.EQUITY,
        notional_usd=money(1_000.0),
        quantity=10.0,
        option_legs=None,
        action=Action.OPEN,
        existing_position_id=None,
    )

    with pytest.raises(LibraryInputError, match=r"REC-BAD.*EQUITY requires a direction"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad,),
            config=_full_config(),
            market=_market(),
        )


def _existing_options_position(
    *,
    position_id: str,
    asset_type: AssetType,
    delta_adjusted_exposure_usd: float,
    notional_usd: float = 2_000.0,
) -> ExistingPosition:
    return ExistingPosition(
        position_id=PositionId(position_id),
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=None if asset_type is AssetType.STRATEGY else Direction.LONG,
        asset_type=asset_type,
        notional_usd=notional_usd,
        delta_adjusted_exposure_usd=delta_adjusted_exposure_usd,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=0.0,
    )


def _close_proposal(
    *,
    proposal_id: str,
    asset_type: AssetType,
    existing_position_id: str,
    notional_usd: float = 2_000.0,
    quantity: float = 5.0,
) -> ProposedDelta:
    return ProposedDelta(
        id=proposal_id,
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=None if asset_type is AssetType.STRATEGY else Direction.LONG,
        asset_type=asset_type,
        notional_usd=money(notional_usd),
        quantity=quantity,
        option_legs=None,
        action=Action.CLOSE,
        existing_position_id=existing_position_id,
    )


def test_close_on_strategy_with_no_legs_is_accepted() -> None:
    """CLOSE on an existing STRATEGY position with ``option_legs=None`` is valid.

    A CLOSE assessment legitimately doesn't carry the leg breakdown — the
    existing position already knows its legs. The validator must accept this
    case; the rule contributions then read ``existing.delta_adjusted_exposure_usd``
    instead of the empty proposal DAE.
    """
    existing = _existing_options_position(
        position_id="POS-STRAT",
        asset_type=AssetType.STRATEGY,
        notional_usd=4_000.0,
        delta_adjusted_exposure_usd=4_500.0,
    )
    state = _snapshot(existing_positions={"POS-STRAT": existing})
    proposal = _close_proposal(
        proposal_id="REC-CLOSE-STRAT",
        asset_type=AssetType.STRATEGY,
        existing_position_id="POS-STRAT",
        notional_usd=4_000.0,
        quantity=4.0,
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
    )

    assert "REC-CLOSE-STRAT" in output.delta_adjusted


def test_close_on_option_with_no_legs_is_accepted() -> None:
    """CLOSE on an existing OPTION position with ``option_legs=None`` is valid."""
    existing = _existing_options_position(
        position_id="POS-OPT",
        asset_type=AssetType.OPTION,
        delta_adjusted_exposure_usd=2_250.0,
    )
    state = _snapshot(existing_positions={"POS-OPT": existing})
    proposal = _close_proposal(
        proposal_id="REC-CLOSE-OPT",
        asset_type=AssetType.OPTION,
        existing_position_id="POS-OPT",
    )

    output = evaluate_proposals(
        state=state,
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
    )

    assert "REC-CLOSE-OPT" in output.delta_adjusted


def test_close_with_no_existing_position_id_raises() -> None:
    """CLOSE without ``existing_position_id`` raises."""
    bad = _equity(
        proposal_id="REC-BAD",
        action=Action.CLOSE,
        existing_position_id=None,
    )
    with pytest.raises(LibraryInputError, match=r"REC-BAD.*CLOSE"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad,),
            config=_full_config(),
            market=_market(),
        )


def test_add_with_unknown_position_id_raises() -> None:
    """ADD with a position id that's not in ``state.existing_positions`` raises."""
    bad = _equity(
        proposal_id="REC-BAD",
        action=Action.ADD,
        existing_position_id="DOES-NOT-EXIST",
    )
    with pytest.raises(LibraryInputError, match=r"REC-BAD.*DOES-NOT-EXIST"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad,),
            config=_full_config(),
            market=_market(),
        )


def test_short_equity_open_without_borrow_cost_raises() -> None:
    """SHORT equity OPEN without ``daily_borrow_cost_usd`` raises."""
    bad = _equity(
        proposal_id="REC-BAD",
        direction=Direction.SHORT,
        notional_usd=1_000.0,
        daily_borrow_cost_usd=None,
    )
    with pytest.raises(LibraryInputError, match=r"REC-BAD.*daily_borrow_cost_usd"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad,),
            config=_full_config(),
            market=_market(),
        )


def test_duplicate_proposal_ids_raise() -> None:
    """Two proposals with the same ``id`` raise."""
    a = _equity(proposal_id="REC-1")
    b = _equity(proposal_id="REC-1", notional_usd=2_000.0)
    with pytest.raises(LibraryInputError, match=r"duplicate.*REC-1"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(a, b),
            config=_full_config(),
            market=_market(),
        )


def test_zero_portfolio_value_raises() -> None:
    """``portfolio_value_usd=0`` breaks all percentage math; raises."""
    state = _snapshot(portfolio_value_usd=0.0, cash_usd=0.0)
    with pytest.raises(LibraryInputError, match=r"portfolio_value_usd"):
        evaluate_proposals(
            state=state,
            proposals=(),
            config=_full_config(),
            market=_market(),
        )


def test_options_proposal_underlying_missing_from_market_raises() -> None:
    """An options proposal whose ``underlying`` isn't in
    ``market.underlying_prices`` raises ``LibraryInputError`` at the entry
    point's aggregated boundary, naming the missing underlying — not a
    mid-computation ``KeyError``."""
    bad = _option(
        proposal_id="REC-MISSING",
        underlying=Symbol("NOT-IN-MARKET"),
    )
    with pytest.raises(LibraryInputError, match=r"REC-MISSING.*NOT-IN-MARKET"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad,),
            config=_full_config(),
            market=_market(),
        )


def test_equity_proposal_underlying_missing_from_market_raises() -> None:
    """Equity proposals also require their ``underlying`` to be in
    ``market.underlying_prices`` — the boundary check is uniform across asset
    types so callers get a consistent ``LibraryInputError`` upfront."""
    bad = _equity(
        proposal_id="REC-MISSING",
        underlying=Symbol("NOT-IN-MARKET"),
        sector="tech",
        direction=Direction.LONG,
        notional_usd=1_000.0,
    )
    with pytest.raises(LibraryInputError, match=r"REC-MISSING.*NOT-IN-MARKET"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad,),
            config=_full_config(),
            market=_market(),
        )


def test_aggregated_errors_lists_every_violation() -> None:
    """A batch with three independent violations raises one
    ``LibraryInputError`` whose message names all three offending proposals."""
    bad1 = ProposedDelta(
        id="REC-EQ-LEGS",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=money(1_000.0),
        quantity=10.0,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=100.0,
                expiration=_EXPIRATION,
                quantity=1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )
    bad2 = _equity(
        proposal_id="REC-NO-BORROW",
        direction=Direction.SHORT,
        notional_usd=1_000.0,
        daily_borrow_cost_usd=None,
    )
    bad3 = _equity(
        proposal_id="REC-CLOSE-NO-POS",
        action=Action.CLOSE,
        existing_position_id=None,
    )

    with pytest.raises(LibraryInputError) as excinfo:
        evaluate_proposals(
            state=_snapshot(),
            proposals=(bad1, bad2, bad3),
            config=_full_config(),
            market=_market(),
        )
    msg = str(excinfo.value)
    assert "REC-EQ-LEGS" in msg
    assert "REC-NO-BORROW" in msg
    assert "REC-CLOSE-NO-POS" in msg


# ---------------------------------------------------------------------------
# delta_buffer_factor validation (M1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_factor", [0.0, -0.5, -1.0])
def test_zero_or_negative_delta_buffer_factor_raises(bad_factor: float) -> None:
    """``delta_buffer_factor`` must be > 0 — zero or negative raises ``LibraryInputError``."""
    with pytest.raises(LibraryInputError, match=r"delta_buffer_factor.*must be > 0"):
        evaluate_proposals(
            state=_snapshot(),
            proposals=(),
            config=_full_config(),
            market=_market(),
            delta_buffer_factor=bad_factor,
        )


def test_delta_buffer_factor_scales_option_signed_notional() -> None:
    """A larger ``delta_buffer_factor`` produces a larger ``signed_notional_usd``
    for option proposals — verifying the scaling is actually applied."""
    proposal = _option(proposal_id="REC-OPT-1", direction=Direction.LONG)

    out_baseline = evaluate_proposals(
        state=_snapshot(),
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
        delta_buffer_factor=1.0,
    )
    out_scaled = evaluate_proposals(
        state=_snapshot(),
        proposals=(proposal,),
        config=_full_config(),
        market=_market(),
        delta_buffer_factor=1.25,
    )

    baseline_dae = out_baseline.delta_adjusted["REC-OPT-1"]
    scaled_dae = out_scaled.delta_adjusted["REC-OPT-1"]
    # Scaled buffer fraction > baseline buffer fraction → larger absolute signed notional.
    assert abs(scaled_dae.signed_notional_usd) > abs(baseline_dae.signed_notional_usd)
