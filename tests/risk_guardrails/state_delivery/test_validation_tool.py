"""Tests for the guardrail validation tool (story 07)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from types import MappingProxyType

import pytest

from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    RegimeLabel,
    RegimeTransitionState,
    RiskBudgetConsumption,
)
from alphamind.portfolio_state.records.positions import Direction, InstrumentType
from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType,
    DeltaAdjustedExposure,
    EscalationZones,
    FeatureFlagsView,
    FixtureIvProvider,
    Greeks,
    IvQuote,
    IvSource,
    IvSurfaceEntry,
    LibraryConfig,
    LibraryOutput,
    MarketInputs,
    PortfolioStateSnapshot,
    RuleProjection,
    Status,
)
from alphamind.risk_guardrails.state_delivery.validation_tool import (
    ProjectedDelta,
    ValidationAction,
    ValidationInstrument,
    ValidationRequest,
    ValidationResult,
    ValidationSize,
    ValidationStrategyLeg,
    ValidationToolError,
    ValidationToolState,
    validate_guardrail,
)

# ---------------------------------------------------------------------------
# Module-level fixture constants
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
_EXPIRATION_DATE = date(2026, 5, 28)
_EXPIRATION_DT = datetime(2026, 5, 28, 0, 0, tzinfo=UTC)
_RISK_FREE_RATE = 0.045
_SPOT = 100.0
_IV = 0.30
_PORTFOLIO_VALUE = 100_000.0


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _config(
    *,
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
    active_sectors: tuple[str, ...] = ("tech", "semis", "financials", "energy"),
) -> LibraryConfig:
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
    sector_exposure_pct: Mapping[str, float] | None = None,
    net_long_pct: float = 30.0,
    net_short_pct: float = 0.0,
    gross_pct: float = 78.0,
    cash_usd: float = 70_000.0,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {
            "tech": 18.3,
            "semis": 6.0,
            "financials": 3.0,
            "energy": 3.0,
        }
    return PortfolioStateSnapshot(
        portfolio_value_usd=_PORTFOLIO_VALUE,
        cash_usd=cash_usd,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(dict(sector_exposure_pct)),
        net_long_pct=net_long_pct,
        net_short_pct=net_short_pct,
        gross_pct=gross_pct,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
    )


def _atm_provider(underlying: str = "AAPL") -> FixtureIvProvider:
    return FixtureIvProvider(
        surface={
            underlying: IvSurfaceEntry(
                underlying=underlying,
                quotes=(
                    IvQuote(
                        strike=100.0,
                        expiration=_EXPIRATION_DATE,
                        contract_type=ContractType.CALL,
                        implied_volatility=_IV,
                    ),
                    IvQuote(
                        strike=100.0,
                        expiration=_EXPIRATION_DATE,
                        contract_type=ContractType.PUT,
                        implied_volatility=_IV,
                    ),
                ),
            ),
        },
        realized_vol={},
    )


def _market(underlyings: Sequence[str] = ("AAPL", "NVDA", "ABC")) -> MarketInputs:
    return MarketInputs(
        underlying_prices=MappingProxyType({u: _SPOT for u in underlyings}),
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=_atm_provider("AAPL"),
        as_of=_AS_OF,
    )


def _risk_budget() -> RiskBudgetConsumption:
    """Empty RiskBudgetConsumption — the validation tool does not read it; the
    renderer-side header consumes it in story 04a's tests, not here."""
    return RiskBudgetConsumption(entries=())


def _active_risk_parameters() -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(),
        active_overlays=(),
    )


def _sector_resolver(ticker: str) -> str:
    """Test-fixture sector resolver — every fixture ticker is in tech."""
    return {"AAPL": "tech", "NVDA": "tech", "ABC": "tech"}.get(ticker, "tech")


def _state(
    *,
    snapshot: PortfolioStateSnapshot | None = None,
    config: LibraryConfig | None = None,
    market: MarketInputs | None = None,
    accumulated_deltas: tuple[ProjectedDelta, ...] = (),
    invocation_id: str = "INV-001",
    sector_resolver: Callable[[str], str] | None = None,
    feature_flags: FeatureFlagsView | None = None,
    borrow_cost_resolver: Callable[[str], float] | None = None,
) -> ValidationToolState:
    library_config = config or _config()
    return ValidationToolState(
        invocation_id=invocation_id,
        starting_snapshot=snapshot or _snapshot(),
        starting_risk_budget=_risk_budget(),
        starting_active_risk_parameters=_active_risk_parameters(),
        profile_feature_flags=feature_flags or library_config.feature_flags,
        library_config=library_config,
        library_market=market or _market(),
        sector_resolver=sector_resolver or _sector_resolver,
        borrow_cost_resolver=borrow_cost_resolver,
        accumulated_deltas=accumulated_deltas,
    )


def _equity_request(
    *,
    ticker: str = "AAPL",
    direction: Direction = Direction.LONG,
    quantity: int = 50,
    dollar_value: float = 5_000.0,
    action: ValidationAction = ValidationAction.OPEN,
) -> ValidationRequest:
    return ValidationRequest(
        instrument=ValidationInstrument(
            ticker=ticker,
            asset_type=InstrumentType.EQUITY,
            direction=direction,
        ),
        size=ValidationSize(quantity=quantity, dollar_value=dollar_value),
        action=action,
    )


def _option_request(
    *,
    ticker: str = "AAPL",
    direction: Direction = Direction.LONG,
    quantity: int = 5,
    dollar_value: float = 1_000.0,
    premium_at_risk_usd: float = 1_000.0,
    action: ValidationAction = ValidationAction.OPEN,
    strike: float = 100.0,
) -> ValidationRequest:
    return ValidationRequest(
        instrument=ValidationInstrument(
            ticker=ticker,
            asset_type=InstrumentType.OPTIONS,
            direction=direction,
            strike=strike,
            expiration=_EXPIRATION_DT,
            contract_type="call",
        ),
        size=ValidationSize(
            quantity=quantity,
            dollar_value=dollar_value,
            premium_at_risk_usd=premium_at_risk_usd,
        ),
        action=action,
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_validation_instrument_options_missing_strike_raises() -> None:
    with pytest.raises(ValueError, match="OPTIONS asset_type requires"):
        ValidationInstrument(
            ticker="AAPL",
            asset_type=InstrumentType.OPTIONS,
            direction=Direction.LONG,
            expiration=_EXPIRATION_DT,
            contract_type="call",
        )


def test_validation_instrument_options_missing_expiration_raises() -> None:
    with pytest.raises(ValueError, match="OPTIONS asset_type requires"):
        ValidationInstrument(
            ticker="AAPL",
            asset_type=InstrumentType.OPTIONS,
            direction=Direction.LONG,
            strike=100.0,
            contract_type="call",
        )


def test_validation_instrument_options_missing_contract_type_raises() -> None:
    with pytest.raises(ValueError, match="OPTIONS asset_type requires"):
        ValidationInstrument(
            ticker="AAPL",
            asset_type=InstrumentType.OPTIONS,
            direction=Direction.LONG,
            strike=100.0,
            expiration=_EXPIRATION_DT,
        )


def test_validation_instrument_strategy_missing_legs_raises() -> None:
    with pytest.raises(ValueError, match="STRATEGY asset_type requires non-empty legs"):
        ValidationInstrument(
            ticker="AAPL",
            asset_type=InstrumentType.STRATEGY,
            direction=Direction.LONG,
            legs=None,
        )


def test_validation_instrument_strategy_empty_legs_raises() -> None:
    with pytest.raises(ValueError, match="STRATEGY asset_type requires non-empty legs"):
        ValidationInstrument(
            ticker="AAPL",
            asset_type=InstrumentType.STRATEGY,
            direction=Direction.LONG,
            legs=(),
        )


def test_validation_size_negative_dollar_value_raises() -> None:
    with pytest.raises(ValueError, match="dollar_value must be non-negative"):
        ValidationSize(quantity=10, dollar_value=-100.0)


def test_validation_size_negative_premium_raises() -> None:
    with pytest.raises(ValueError, match="premium_at_risk_usd must be non-negative"):
        ValidationSize(quantity=10, dollar_value=100.0, premium_at_risk_usd=-50.0)


def test_validation_action_members_are_subset_excluding_cancel() -> None:
    members = {m.name for m in ValidationAction}
    assert members == {"OPEN", "ADD", "CLOSE", "ADJUST"}


# ---------------------------------------------------------------------------
# Feature-flag early-exit
# ---------------------------------------------------------------------------


def test_equity_open_does_not_trigger_options_early_exit() -> None:
    """Equity OPEN on options_enabled=False does NOT short-circuit the library
    call — only option-typed instruments do."""
    state = _state(config=_config(options_enabled=False))
    result = validate_guardrail(request=_equity_request(), state=state)
    # Some per-rule entries are produced — we composed the library, not the early-exit.
    assert result.per_rule  # non-empty


def test_option_open_on_options_disabled_returns_static_failure() -> None:
    """Option OPEN on options_enabled=False returns FAIL with empty per_rule."""
    state = _state(config=_config(options_enabled=False))
    result = validate_guardrail(request=_option_request(), state=state)
    assert result.overall == "FAIL"
    assert result.per_rule == ()
    assert result.greeks is None
    assert result.delta_adjusted_exposure == 0.0
    assert result.failure_guidance == "Options trading is disabled for this portfolio profile."
    assert result.proposal_index_in_invocation == 1


def test_short_open_on_shorts_disabled_returns_static_failure() -> None:
    """Short OPEN on short_selling_enabled=False returns FAIL with empty per_rule."""
    state = _state(config=_config(short_selling_enabled=False))
    result = validate_guardrail(
        request=_equity_request(direction=Direction.SHORT),
        state=state,
    )
    assert result.overall == "FAIL"
    assert result.per_rule == ()
    assert result.failure_guidance == "Short selling is disabled for this portfolio profile."


# ---------------------------------------------------------------------------
# Cumulative tracking
# ---------------------------------------------------------------------------


def test_first_call_on_empty_state_no_prior_proposals_text() -> None:
    state = _state()
    result = validate_guardrail(request=_equity_request(), state=state)
    assert result.proposal_index_in_invocation == 1
    assert (
        result.cumulative_impact_note == "This is proposal #1 in this invocation. "
        "No prior proposals affect headroom calculations."
    )


def test_second_call_after_acceptance_increments_index_and_notes_prior() -> None:
    state = _state()
    first = validate_guardrail(request=_equity_request(), state=state)
    assert first.overall == "PASS"
    delta = ProjectedDelta(
        instrument=_equity_request().instrument,
        size=_equity_request().size,
        action=ValidationAction.OPEN,
        sector="tech",
        delta_adjusted_exposure=first.delta_adjusted_exposure,
        greeks=first.greeks,
        proposal_index=1,
    )
    state2 = state.with_accepted_proposal(delta)
    second = validate_guardrail(
        request=_equity_request(ticker="NVDA"),
        state=state2,
    )
    assert second.proposal_index_in_invocation == 2
    assert (
        second.cumulative_impact_note == "This is proposal #2 in this invocation. "
        "Cumulative impact of proposals #1-1 is included in headroom calculations."
    )


def test_two_individually_passing_proposals_breach_cumulatively() -> None:
    """Two equities each contributing 12% to net-long: current 40%, limit 60%.
    First passes (projected 52%), second fails (projected 64%).

    Per-position_max_size is raised to 15% so the per-proposal size doesn't
    trip that rule and the test isolates the cumulative net-long behavior.
    """
    snapshot = _snapshot(
        sector_exposure_pct={"tech": 5.0, "semis": 5.0, "financials": 5.0, "energy": 25.0},
        net_long_pct=40.0,
        gross_pct=40.0,
    )
    config = _config()
    raised_limits = dict(config.effective_limits)
    raised_limits["position_max_size_pct"] = 15.0
    raised_limits["sector_concentration_pct"] = 50.0
    config = LibraryConfig(
        effective_limits=MappingProxyType(raised_limits),
        escalation_zones=config.escalation_zones,
        feature_flags=config.feature_flags,
        active_sectors=config.active_sectors,
        active_regime=config.active_regime,
        active_profile=config.active_profile,
        conservative_buffer_pct=config.conservative_buffer_pct,
    )
    state = _state(snapshot=snapshot, config=config)
    request = _equity_request(dollar_value=12_000.0)  # 12% of 100K

    first = validate_guardrail(request=request, state=state)
    assert first.overall == "PASS"

    delta = ProjectedDelta(
        instrument=request.instrument,
        size=request.size,
        action=ValidationAction.OPEN,
        sector="tech",
        delta_adjusted_exposure=first.delta_adjusted_exposure,
        greeks=first.greeks,
        proposal_index=1,
    )
    state2 = state.with_accepted_proposal(delta)

    # Use a different ticker to avoid duplicate proposal id
    request2 = _equity_request(ticker="NVDA", dollar_value=12_000.0)
    second = validate_guardrail(request=request2, state=state2)
    assert second.overall == "FAIL"
    by_rule = {p.rule: p for p in second.per_rule}
    # net_long_pct should reflect cumulative 40 + 12 + 12 = 64
    assert by_rule["net_long_pct"].projected_after == pytest.approx(64.0)
    assert by_rule["net_long_pct"].status is Status.FAIL


def test_failed_proposal_not_added_to_accumulated_deltas() -> None:
    """The caller (not the tool) decides whether to call with_accepted_proposal."""
    state = _state()
    state2 = state.with_accepted_proposal(
        ProjectedDelta(
            instrument=_equity_request().instrument,
            size=_equity_request().size,
            action=ValidationAction.OPEN,
            sector="tech",
            delta_adjusted_exposure=5_000.0,
            greeks=None,
            proposal_index=1,
        )
    )
    assert len(state2.accumulated_deltas) == 1
    assert len(state.accumulated_deltas) == 0  # original unchanged


def test_with_accepted_proposal_returns_new_immutable_state() -> None:
    state = _state()
    delta = ProjectedDelta(
        instrument=_equity_request().instrument,
        size=_equity_request().size,
        action=ValidationAction.OPEN,
        sector="tech",
        delta_adjusted_exposure=5_000.0,
        greeks=None,
        proposal_index=1,
    )
    state2 = state.with_accepted_proposal(delta)
    assert state.accumulated_deltas == ()
    assert state2.accumulated_deltas == (delta,)
    assert state2 is not state


def test_state_reset_across_agent_boundary_starts_fresh_index() -> None:
    """Fresh ValidationToolState models the analyst → strategist boundary."""
    state = _state()
    state = state.with_accepted_proposal(
        ProjectedDelta(
            instrument=_equity_request().instrument,
            size=_equity_request().size,
            action=ValidationAction.OPEN,
            sector="tech",
            delta_adjusted_exposure=5_000.0,
            greeks=None,
            proposal_index=1,
        )
    )
    assert len(state.accumulated_deltas) == 1

    fresh_state = _state(invocation_id="INV-002")
    result = validate_guardrail(request=_equity_request(), state=fresh_state)
    assert result.proposal_index_in_invocation == 1
    assert "No prior proposals" in result.cumulative_impact_note


# ---------------------------------------------------------------------------
# Library composition (overall classification)
# ---------------------------------------------------------------------------


def test_pass_when_all_per_rule_pass_or_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    pass_proj = RuleProjection(
        rule="net_long_pct",
        status=Status.PASS,
        current=30.0,
        limit=60.0,
        projected_after=35.0,
        headroom_remaining=25.0,
        unit="% of portfolio",
    )
    warning_proj = RuleProjection(
        rule="sector_concentration_tech",
        status=Status.WARNING,
        current=18.0,
        limit=25.0,
        projected_after=22.0,
        headroom_remaining=3.0,
        unit="% of portfolio (delta-adjusted)",
    )
    fake_output = LibraryOutput(
        per_rule=(pass_proj, warning_proj),
        delta_adjusted=MappingProxyType(
            {
                "validation_request": DeltaAdjustedExposure(
                    proposal_id="validation_request",
                    signed_notional_usd=5_000.0,
                    net_greeks=None,
                    iv_used=None,
                    iv_source=None,
                    unbuffered_delta=None,
                ),
            }
        ),
    )

    def fake_eval(**_kwargs: object) -> LibraryOutput:
        return fake_output

    monkeypatch.setattr(
        "alphamind.risk_guardrails.state_delivery.validation_tool.evaluate_proposals",
        fake_eval,
    )
    state = _state()
    result = validate_guardrail(request=_equity_request(), state=state)
    assert result.overall == "PASS"
    assert result.per_rule == (pass_proj, warning_proj)
    assert result.failure_guidance is None


def test_fail_when_any_per_rule_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    fail_proj = RuleProjection(
        rule="sector_concentration_tech",
        status=Status.FAIL,
        current=23.0,
        limit=25.0,
        projected_after=29.0,
        headroom_remaining=-4.0,
        unit="% of portfolio (delta-adjusted)",
    )
    fake_output = LibraryOutput(
        per_rule=(fail_proj,),
        delta_adjusted=MappingProxyType(
            {
                "validation_request": DeltaAdjustedExposure(
                    proposal_id="validation_request",
                    signed_notional_usd=6_000.0,
                    net_greeks=None,
                    iv_used=None,
                    iv_source=None,
                    unbuffered_delta=None,
                ),
            }
        ),
    )

    def fake_eval(**_kwargs: object) -> LibraryOutput:
        return fake_output

    monkeypatch.setattr(
        "alphamind.risk_guardrails.state_delivery.validation_tool.evaluate_proposals",
        fake_eval,
    )
    state = _state()
    result = validate_guardrail(request=_equity_request(), state=state)
    assert result.overall == "FAIL"
    assert result.failure_guidance is not None


def test_library_not_called_on_options_disabled_early_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"count": 0}

    def fake_eval(**_kwargs: object) -> LibraryOutput:
        called["count"] += 1
        raise AssertionError("evaluate_proposals must not be called on early-exit")

    monkeypatch.setattr(
        "alphamind.risk_guardrails.state_delivery.validation_tool.evaluate_proposals",
        fake_eval,
    )
    state = _state(config=_config(options_enabled=False))
    result = validate_guardrail(request=_option_request(), state=state)
    assert result.overall == "FAIL"
    assert called["count"] == 0


# ---------------------------------------------------------------------------
# Failure guidance templates
# ---------------------------------------------------------------------------


def _fail_proj(
    *,
    rule: str,
    current: float,
    limit: float,
    projected_after: float,
    unit: str,
    inverse: bool = False,
) -> RuleProjection:
    return RuleProjection(
        rule=rule,
        status=Status.FAIL,
        current=current,
        limit=limit,
        projected_after=projected_after,
        headroom_remaining=limit - projected_after,
        unit=unit,
        inverse=inverse,
    )


def _patch_library(
    monkeypatch: pytest.MonkeyPatch,
    per_rule: tuple[RuleProjection, ...],
    *,
    signed_notional_usd: float = 5_000.0,
    greeks: Greeks | None = None,
    iv_source: IvSource | None = None,
    iv_used: float | None = None,
) -> None:
    fake_output = LibraryOutput(
        per_rule=per_rule,
        delta_adjusted=MappingProxyType(
            {
                "validation_request": DeltaAdjustedExposure(
                    proposal_id="validation_request",
                    signed_notional_usd=signed_notional_usd,
                    net_greeks=greeks,
                    iv_used=iv_used,
                    iv_source=iv_source,
                    unbuffered_delta=None,
                ),
            }
        ),
    )

    def fake_eval(**_kwargs: object) -> LibraryOutput:
        return fake_output

    monkeypatch.setattr(
        "alphamind.risk_guardrails.state_delivery.validation_tool.evaluate_proposals",
        fake_eval,
    )


def test_failure_guidance_single_sector_concentration_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = _fail_proj(
        rule="sector_concentration_tech",
        current=23.0,
        limit=25.0,
        projected_after=29.0,
        unit="% of portfolio (delta-adjusted)",
    )
    _patch_library(monkeypatch, (proj,))
    result = validate_guardrail(request=_equity_request(), state=_state())
    assert result.overall == "FAIL"
    assert result.failure_guidance is not None
    # (29 - 25) / 29 * 100 ≈ 13.79 → 14%
    assert "Reduce size by ~14%" in result.failure_guidance
    assert "sector_concentration_tech" in result.failure_guidance


def test_failure_guidance_single_net_long_includes_substitute_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = _fail_proj(
        rule="net_long_pct",
        current=55.0,
        limit=60.0,
        projected_after=65.0,
        unit="% of portfolio (delta-adjusted)",
    )
    _patch_library(monkeypatch, (proj,))
    result = validate_guardrail(request=_equity_request(), state=_state())
    assert result.failure_guidance is not None
    assert "substitute a lower-delta instrument" in result.failure_guidance


def test_failure_guidance_capital_includes_dollar_amounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverse rule ``min_cash_reserve_pct``: limit 10% (floor), projected 8%
    (cash dropped under reserve). The agent must see the *deployable capital
    above the floor*, not the floor itself.

    Snapshot: portfolio $100K, cash $70K → current cash is 70%. Floor is 10%,
    so deployable capital above floor = 60% of $100K = $60,000.
    The proposal would push cash to 8% (a 2-point shortfall, $2K below floor);
    given a $5K proposal, the suggested smaller size is $5K - $2K = $3K.
    """
    proj = _fail_proj(
        rule="min_cash_reserve_pct",
        current=70.0,  # cash is 70% of portfolio
        limit=10.0,
        projected_after=8.0,
        unit="% of portfolio",
        inverse=True,
    )
    _patch_library(monkeypatch, (proj,), signed_notional_usd=5_000.0)
    result = validate_guardrail(request=_equity_request(), state=_state())
    assert result.failure_guidance is not None
    # The deployable capital available (above the floor) is $60K, NOT the
    # floor's $10K. Pre-fix bug: framed $10K as "available".
    assert "$60,000" in result.failure_guidance
    assert "$3,000" in result.failure_guidance
    assert "Insufficient deployable capital" in result.failure_guidance


def test_failure_guidance_capital_pending_order_non_inverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-inverse capital rule ``pending_order_capital_pct``: limit 20% (cap),
    current 15%, projected 25%. Available below cap = (20-15)*100K/100 = $5K.
    Excess = (25-20)*100K/100 = $5K. Suggested smaller = max(0, |5K| - 5K) = $0.
    """
    proj = _fail_proj(
        rule="pending_order_capital_pct",
        current=15.0,
        limit=20.0,
        projected_after=25.0,
        unit="% of portfolio",
    )
    _patch_library(monkeypatch, (proj,), signed_notional_usd=5_000.0)
    result = validate_guardrail(request=_equity_request(), state=_state())
    assert result.failure_guidance is not None
    assert "$5,000" in result.failure_guidance
    assert "$0" in result.failure_guidance


def test_failure_guidance_multi_rule_inverse_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an inverse-rule failure is the largest-magnitude breach, it must
    be selected as the primary. Pre-fix bug: ``projected_after - limit`` was
    negative for inverse rules, so the inverse rule was silently deprioritized.

    Setup: sector overage = +1 (small breach); inverse cash overage = -3
    (large breach by absolute magnitude). Primary should be the cash rule.
    """
    sector = _fail_proj(
        rule="sector_concentration_tech",
        current=23.0,
        limit=25.0,
        projected_after=26.0,  # over by 1
        unit="% of portfolio (delta-adjusted)",
    )
    cash = _fail_proj(
        rule="min_cash_reserve_pct",
        current=12.0,
        limit=10.0,
        projected_after=7.0,  # under by 3 (largest by magnitude)
        unit="% of portfolio",
        inverse=True,
    )
    _patch_library(monkeypatch, (sector, cash), signed_notional_usd=5_000.0)
    result = validate_guardrail(request=_equity_request(), state=_state())
    assert result.failure_guidance is not None
    assert "Multiple rules would breach" in result.failure_guidance
    # Primary must be the inverse rule (largest magnitude breach).
    assert "addresses min_cash_reserve_pct" in result.failure_guidance


@pytest.mark.parametrize(
    "projected_after,limit,expected_pct",
    [
        # 13.5 → 14 (Python's round-half-to-even rounds 13.5 up to even 14)
        (100.0, 86.5, 14),
        # 14.5 → 14 (round-half-to-even rounds 14.5 down to even 14)
        (200.0, 171.0, 14),
        # Plain rounding (not on a half-boundary)
        (29.0, 25.0, 14),
    ],
)
def test_reduction_pct_rounds_half_to_even(
    monkeypatch: pytest.MonkeyPatch,
    projected_after: float,
    limit: float,
    expected_pct: int,
) -> None:
    """``_reduction_pct`` uses Python's banker's rounding; both halves of
    round-half-to-even must produce the documented integer."""
    proj = _fail_proj(
        rule="sector_concentration_tech",
        current=limit - 1.0,
        limit=limit,
        projected_after=projected_after,
        unit="% of portfolio (delta-adjusted)",
    )
    _patch_library(monkeypatch, (proj,))
    result = validate_guardrail(request=_equity_request(), state=_state())
    assert result.failure_guidance is not None
    assert f"~{expected_pct}%" in result.failure_guidance


def test_failure_guidance_multi_rule_lists_all_with_primary_reduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sector = _fail_proj(
        rule="sector_concentration_tech",
        current=23.0,
        limit=25.0,
        projected_after=29.0,  # over by 4
        unit="% of portfolio (delta-adjusted)",
    )
    net_long = _fail_proj(
        rule="net_long_pct",
        current=55.0,
        limit=60.0,
        projected_after=65.0,  # over by 5 (primary)
        unit="% of portfolio (delta-adjusted)",
    )
    _patch_library(monkeypatch, (sector, net_long))
    result = validate_guardrail(request=_equity_request(), state=_state())
    assert result.failure_guidance is not None
    assert "Multiple rules would breach" in result.failure_guidance
    assert "sector_concentration_tech" in result.failure_guidance
    assert "net_long_pct" in result.failure_guidance
    # primary is net_long_pct (overage 5 > 4); pct = 5/65*100 ≈ 8%
    assert "~8%" in result.failure_guidance


def test_pass_returns_none_failure_guidance() -> None:
    state = _state()
    result = validate_guardrail(request=_equity_request(), state=state)
    assert result.overall == "PASS"
    assert result.failure_guidance is None


# ---------------------------------------------------------------------------
# Greeks population by asset type
# ---------------------------------------------------------------------------


def test_equity_action_returns_none_greeks() -> None:
    state = _state()
    result = validate_guardrail(request=_equity_request(), state=state)
    assert result.greeks is None
    assert result.implied_volatility is None


def test_option_action_returns_populated_greeks(monkeypatch: pytest.MonkeyPatch) -> None:
    proj = RuleProjection(
        rule="options_delta_pct",
        status=Status.PASS,
        current=0.0,
        limit=30.0,
        projected_after=2.0,
        headroom_remaining=28.0,
        unit="% of portfolio (delta-adjusted)",
    )
    greeks = Greeks(delta=0.55, gamma=0.02, theta=-0.01, vega=0.10)
    _patch_library(
        monkeypatch,
        (proj,),
        signed_notional_usd=2_000.0,
        greeks=greeks,
        iv_source=IvSource.SURFACE,
        iv_used=0.30,
    )
    result = validate_guardrail(request=_option_request(), state=_state())
    assert result.greeks == greeks
    # ALP-399: the IV the library consumed is surfaced on ValidationResult so
    # downstream Acknowledgment / persistence (OptionGreeks.iv_used) can read it.
    assert result.implied_volatility == 0.30


def test_strategy_action_returns_populated_greeks(monkeypatch: pytest.MonkeyPatch) -> None:
    proj = RuleProjection(
        rule="options_delta_pct",
        status=Status.PASS,
        current=0.0,
        limit=30.0,
        projected_after=1.0,
        headroom_remaining=29.0,
        unit="% of portfolio (delta-adjusted)",
    )
    greeks = Greeks(delta=0.20, gamma=0.005, theta=-0.005, vega=0.04)
    _patch_library(
        monkeypatch,
        (proj,),
        signed_notional_usd=1_000.0,
        greeks=greeks,
    )
    request = ValidationRequest(
        instrument=ValidationInstrument(
            ticker="AAPL",
            asset_type=InstrumentType.STRATEGY,
            direction=Direction.LONG,
            legs=(
                ValidationStrategyLeg(
                    direction=Direction.LONG,
                    asset_type=InstrumentType.OPTIONS,
                    strike=100.0,
                    expiration=_EXPIRATION_DT,
                    contract_type="call",
                    quantity=1,
                ),
                ValidationStrategyLeg(
                    direction=Direction.SHORT,
                    asset_type=InstrumentType.OPTIONS,
                    strike=110.0,
                    expiration=_EXPIRATION_DT,
                    contract_type="call",
                    quantity=1,
                ),
            ),
        ),
        size=ValidationSize(quantity=1, dollar_value=500.0, premium_at_risk_usd=500.0),
        action=ValidationAction.OPEN,
    )
    result = validate_guardrail(request=request, state=_state())
    assert result.greeks == greeks


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_non_marketable_limit_reserves_capital_when_flagged() -> None:
    """``pending_order_capital_pct`` rule contributes only when
    ``reserves_capital=True``. A marketable order (default) reserves nothing;
    a non-marketable limit reserves its dollar value."""
    snapshot = _snapshot()
    state = _state(snapshot=snapshot)

    marketable = validate_guardrail(
        request=_equity_request(dollar_value=15_000.0),
        state=state,
    )
    non_marketable_request = _equity_request(dollar_value=15_000.0).model_copy(
        update={"reserves_capital": True}
    )
    non_marketable = validate_guardrail(request=non_marketable_request, state=state)

    by_rule_marketable = {p.rule: p for p in marketable.per_rule}
    by_rule_non_marketable = {p.rule: p for p in non_marketable.per_rule}
    pending_rule = "pending_order_capital_pct"
    # Both proposals see the same starting state for the rule, but only the
    # non-marketable one contributes to projected_after.
    assert by_rule_marketable[pending_rule].projected_after == pytest.approx(0.0)
    # Non-marketable: contributes 15_000 / 100_000 * 100 = 15.0% to the rule.
    assert by_rule_non_marketable[pending_rule].projected_after == pytest.approx(15.0)


def test_repeated_calls_produce_identical_results() -> None:
    state = _state()
    request = _equity_request()
    a = validate_guardrail(request=request, state=state)
    b = validate_guardrail(request=request, state=state)
    assert a == b


# ---------------------------------------------------------------------------
# Re-export surface
# ---------------------------------------------------------------------------


def test_option_open_premium_at_risk_routed_as_library_notional() -> None:
    """When ``premium_at_risk_usd != dollar_value``, the library sees the
    premium as ``notional_usd`` for option proposals (the library's capital
    rules treat options ``notional_usd`` as the premium-at-risk for cash
    accounting per ``rules/capital.py``)."""
    state = _state()
    # Cash starts at 70K (70% of 100K portfolio); min-cash floor is 10%.
    # Premium-at-risk 6_000 → cash drops to 64K (64%); dollar_value 12_000
    # would drop cash to 58K (58%). With premium routed: pending_order
    # contribution 0 (options reserve nothing), cash impact 6K.
    request = _option_request(
        ticker="AAPL",
        dollar_value=12_000.0,
        premium_at_risk_usd=6_000.0,
    )
    result = validate_guardrail(request=request, state=state)
    by_rule = {p.rule: p for p in result.per_rule}
    cash_rule = by_rule["min_cash_reserve_pct"]
    # Cash drops from 70% to 64% (premium 6K used, not dollar_value 12K).
    # Pre-fix: would be 58% (12K used).
    assert cash_rule.projected_after == pytest.approx(64.0)


def test_option_open_without_premium_falls_back_to_dollar_value() -> None:
    """When ``premium_at_risk_usd`` is None, dollar_value plays the
    premium-at-risk role for cash accounting (back-compat fallback)."""
    state = _state()
    # Build an option request where premium_at_risk_usd is unset
    request = ValidationRequest(
        instrument=ValidationInstrument(
            ticker="AAPL",
            asset_type=InstrumentType.OPTIONS,
            direction=Direction.LONG,
            strike=100.0,
            expiration=_EXPIRATION_DT,
            contract_type="call",
        ),
        size=ValidationSize(quantity=5, dollar_value=8_000.0),  # no premium_at_risk_usd
        action=ValidationAction.OPEN,
    )
    result = validate_guardrail(request=request, state=state)
    by_rule = {p.rule: p for p in result.per_rule}
    cash_rule = by_rule["min_cash_reserve_pct"]
    # Cash drops from 70% to 62% (dollar_value 8K).
    assert cash_rule.projected_after == pytest.approx(62.0)


def test_short_equity_open_uses_borrow_cost_resolver() -> None:
    """Short equity OPEN derives ``daily_borrow_cost_usd`` via the state's
    ``borrow_cost_resolver``; the request shape carries no borrow-cost field."""
    state = _state(borrow_cost_resolver=lambda ticker: 0.50)
    request = _equity_request(direction=Direction.SHORT, dollar_value=4_000.0)
    # The library would raise on missing borrow-cost; PASS or FAIL depends on
    # rule outcomes, but the call must succeed (no LibraryInputError).
    result = validate_guardrail(request=request, state=state)
    assert result.overall in {"PASS", "FAIL"}


def test_short_equity_open_without_resolver_raises_validation_error() -> None:
    """Short equity OPEN with ``borrow_cost_resolver=None`` raises
    ``ValidationToolError`` with a clear message."""
    state = _state()  # default fixture: resolver=None
    request = _equity_request(direction=Direction.SHORT)
    with pytest.raises(ValidationToolError, match="borrow_cost_resolver"):
        validate_guardrail(request=request, state=state)


def test_long_equity_open_does_not_call_borrow_cost_resolver() -> None:
    """The resolver is only invoked for short equity OPEN/ADD; other actions
    must succeed even with ``borrow_cost_resolver=None``."""
    state = _state()  # default fixture: resolver=None
    request = _equity_request(direction=Direction.LONG)
    result = validate_guardrail(request=request, state=state)
    assert result.overall in {"PASS", "FAIL"}


def test_borrow_cost_resolver_purity_replay_determinism() -> None:
    """``borrow_cost_resolver`` purity contract: equal ticker inputs must
    produce equal float outputs across all calls within a chain.

    Replays of the same proposal sequence against fresh ``ValidationToolState``
    instances must yield byte-identical ``ValidationResult`` outputs. The
    resolver is invoked once per short equity OPEN/ADD per call (the current
    proposal plus each replayed prior delta in the chain).
    """
    calls: list[str] = []

    def tracking_resolver(ticker: str) -> float:
        calls.append(ticker)
        return {"AAPL": 0.40, "NVDA": 0.55}[ticker]

    def fresh_state() -> ValidationToolState:
        # Snapshot/config sized so the short-only chain stays well inside limits.
        snapshot = _snapshot(net_long_pct=0.0, net_short_pct=0.0, gross_pct=0.0)
        return _state(snapshot=snapshot, borrow_cost_resolver=tracking_resolver)

    state = fresh_state()
    request_a = _equity_request(ticker="AAPL", direction=Direction.SHORT, dollar_value=2_000.0)
    request_b = _equity_request(ticker="NVDA", direction=Direction.SHORT, dollar_value=2_000.0)

    # Call 1: validates request_a → resolver called once for AAPL.
    result_a = validate_guardrail(request=request_a, state=state)
    state = state.with_accepted_proposal(
        ProjectedDelta(
            instrument=request_a.instrument,
            size=request_a.size,
            action=ValidationAction.OPEN,
            sector="tech",
            delta_adjusted_exposure=result_a.delta_adjusted_exposure,
            greeks=result_a.greeks,
            proposal_index=1,
        )
    )
    # Call 2: replays request_a (resolver call) + validates request_b.
    result_b = validate_guardrail(request=request_b, state=state)
    expected_calls = ["AAPL", "AAPL", "NVDA"]
    assert calls == expected_calls

    # Replay the same sequence against a fresh state with a fresh tracking list;
    # the resolver must still be pure (same ticker → same float), so the
    # ValidationResult outputs must be byte-identical to the first run.
    calls.clear()
    state2 = fresh_state()
    replay_a = validate_guardrail(request=request_a, state=state2)
    state2 = state2.with_accepted_proposal(
        ProjectedDelta(
            instrument=request_a.instrument,
            size=request_a.size,
            action=ValidationAction.OPEN,
            sector="tech",
            delta_adjusted_exposure=replay_a.delta_adjusted_exposure,
            greeks=replay_a.greeks,
            proposal_index=1,
        )
    )
    replay_b = validate_guardrail(request=request_b, state=state2)
    assert calls == expected_calls
    assert replay_a == result_a
    assert replay_b == result_b


def test_borrow_cost_resolver_mutating_violates_replay_determinism() -> None:
    """A mutating resolver (closure over a mutable counter) violates the
    purity contract and produces visibly different ``ValidationResult`` outputs
    across replays — making the contract violation tangible.
    """
    counter = {"n": 0}

    def mutating_resolver(_ticker: str) -> float:
        counter["n"] += 1
        # Each call returns a different cost — clearly impure.
        return 0.10 + 0.05 * counter["n"]

    def fresh_state() -> ValidationToolState:
        snapshot = _snapshot(net_long_pct=0.0, net_short_pct=0.0, gross_pct=0.0)
        return _state(snapshot=snapshot, borrow_cost_resolver=mutating_resolver)

    request = _equity_request(ticker="AAPL", direction=Direction.SHORT, dollar_value=2_000.0)

    first = validate_guardrail(request=request, state=fresh_state())
    second = validate_guardrail(request=request, state=fresh_state())
    # Per-rule projections may differ via the borrow-cost rule; the divergence
    # is the visible symptom of the contract violation.
    assert first.per_rule != second.per_rule


def test_validation_tool_state_uses_library_prefixed_field_names() -> None:
    """Library plumbing fields are prefixed ``library_*`` to distinguish them
    from the spec's renderer-shared starting_* fields."""
    state = _state()
    assert isinstance(state.library_config, LibraryConfig)
    assert isinstance(state.library_market, MarketInputs)
    # Spec-shaped renderer-shared inputs remain available as starting_* names
    assert state.starting_snapshot is not None
    assert state.starting_risk_budget is not None
    assert state.starting_active_risk_parameters is not None
    assert state.profile_feature_flags is not None


def test_validation_tool_state_rejects_mismatched_feature_flags() -> None:
    """``profile_feature_flags`` and ``library_config.feature_flags`` must agree
    at construction time. The tool composes the library against the latter and
    returns disabled-feature guidance against the former; silent divergence
    yields a class of bugs where the guidance and the actual gate disagree."""
    library_config = _config(options_enabled=True, short_selling_enabled=True)
    mismatched_flags = FeatureFlagsView(options_enabled=False, short_selling_enabled=True)
    with pytest.raises(ValueError, match="profile_feature_flags must equal"):
        ValidationToolState(
            invocation_id="INV-001",
            starting_snapshot=_snapshot(),
            starting_risk_budget=_risk_budget(),
            starting_active_risk_parameters=_active_risk_parameters(),
            profile_feature_flags=mismatched_flags,
            library_config=library_config,
            library_market=_market(),
            sector_resolver=_sector_resolver,
        )


def test_validation_tool_state_with_accepted_proposal_preserves_all_fields() -> None:
    """``with_accepted_proposal`` returns a new state preserving every other
    field — invocation_id, snapshot, risk budget, active risk params, feature
    flags, library config, library market, sector resolver."""
    state = _state()
    delta = ProjectedDelta(
        instrument=_equity_request().instrument,
        size=_equity_request().size,
        action=ValidationAction.OPEN,
        sector="tech",
        delta_adjusted_exposure=5_000.0,
        greeks=None,
        proposal_index=1,
    )
    state2 = state.with_accepted_proposal(delta)
    assert state2.invocation_id == state.invocation_id
    assert state2.starting_snapshot is state.starting_snapshot
    assert state2.starting_risk_budget is state.starting_risk_budget
    assert state2.starting_active_risk_parameters is state.starting_active_risk_parameters
    assert state2.profile_feature_flags == state.profile_feature_flags
    assert state2.library_config is state.library_config
    assert state2.library_market is state.library_market
    assert state2.sector_resolver is state.sector_resolver
    assert state2.accumulated_deltas == (delta,)


def test_state_delivery_reexports_validation_tool_symbols() -> None:
    from alphamind.risk_guardrails.state_delivery import (
        ProjectedDelta as RexProjectedDelta,
    )
    from alphamind.risk_guardrails.state_delivery import (
        ValidationAction as RexValidationAction,
    )
    from alphamind.risk_guardrails.state_delivery import (
        ValidationInstrument as RexValidationInstrument,
    )
    from alphamind.risk_guardrails.state_delivery import (
        ValidationRequest as RexValidationRequest,
    )
    from alphamind.risk_guardrails.state_delivery import (
        ValidationResult as RexValidationResult,
    )
    from alphamind.risk_guardrails.state_delivery import (
        ValidationSize as RexValidationSize,
    )
    from alphamind.risk_guardrails.state_delivery import (
        ValidationStrategyLeg as RexValidationStrategyLeg,
    )
    from alphamind.risk_guardrails.state_delivery import (
        ValidationToolError as RexValidationToolError,
    )
    from alphamind.risk_guardrails.state_delivery import (
        ValidationToolState as RexValidationToolState,
    )
    from alphamind.risk_guardrails.state_delivery import (
        validate_guardrail as rex_validate_guardrail,
    )

    assert RexProjectedDelta is ProjectedDelta
    assert RexValidationAction is ValidationAction
    assert RexValidationInstrument is ValidationInstrument
    assert RexValidationRequest is ValidationRequest
    assert RexValidationResult is ValidationResult
    assert RexValidationSize is ValidationSize
    assert RexValidationStrategyLeg is ValidationStrategyLeg
    assert RexValidationToolError is ValidationToolError
    assert RexValidationToolState is ValidationToolState
    assert rex_validate_guardrail is validate_guardrail


def test_real_library_composition_smoke() -> None:
    """Sanity: the real evaluate_proposals composes successfully (no monkeypatch)."""
    state = _state()
    result = validate_guardrail(request=_equity_request(), state=state)
    assert result.per_rule  # non-empty per_rule from the real library
    by_rule = {p.rule: p for p in result.per_rule}
    assert "net_long_pct" in by_rule
    assert result.delta_adjusted_exposure == pytest.approx(5_000.0)
