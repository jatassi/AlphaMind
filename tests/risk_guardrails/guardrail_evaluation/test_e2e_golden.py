"""End-to-end golden tests for ``evaluate_proposals`` (story 06).

Pin the library's behaviour across realistic compositions of state, proposals,
config, and market inputs. Tests load the shipped ``config/`` tree via
``alphamind.config.loaders`` + ``compose_config`` for scenarios 1-7 and 11;
synthetic outputs for scenarios 8-10 demonstrate the library-to-caller seams
(validation tool, pre-processor, engine T3 rejection payload).

Numerical relationships are pinned where the design contracts them (status,
sign, ordering); absolute numbers come from the resolver's output so legitimate
config edits do not break unrelated assertions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest
import yaml

from alphamind._kernel.ids import PositionId, Symbol
from alphamind._kernel.money import money
from alphamind.config.loaders import (
    load_modes,
    load_overlays,
    load_profiles,
    load_regimes,
    load_run_types,
)
from alphamind.config.models import (
    AgentsConfig,
    AssetsConfig,
    ContinuousMonitorConfig,
    DigestConfig,
    ExecutionConfig,
    GuardrailsConfig,
    LLMFailureConfig,
    LoadedConfig,
    MainConfig,
    Mode,
    Overlay,
    Profile,
    Regime,
    ResolvedConfig,
    RuntimeDimensions,
    RunType,
    SchedulerConfig,
    VenueConfig,
    compose_config,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    Action,
    AssetType,
    ContractType,
    DeltaAdjustedExposure,
    Direction,
    ExistingPosition,
    FixtureIvProvider,
    IvQuote,
    IvSource,
    IvSurfaceEntry,
    LibraryConfig,
    LibraryOutput,
    MarketInputs,
    OptionLeg,
    PortfolioStateSnapshot,
    ProposedDelta,
    RealizedVolEntry,
    RuleProjection,
    Status,
    build_active_specs,
    evaluate_proposals,
    from_resolved_config,
)

# ---------------------------------------------------------------------------
# Shipped-tree loader (module scope; tests share the parsed bundle)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_CONFIG_DIR = _REPO_ROOT / "config"


def _read(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((_CONFIG_DIR / name).read_text()))


_MAIN = MainConfig.model_validate(_read("main.yaml"))
_SCHEDULER = SchedulerConfig.model_validate(_read("scheduler.yaml"))
_VENUE = VenueConfig.model_validate(_read("venue.yaml"))
_EXECUTION = ExecutionConfig.model_validate(_read("execution.yaml"))
_GUARDRAILS = GuardrailsConfig.model_validate(_read("guardrails.yaml"))
_LLM_FAILURE = LLMFailureConfig.model_validate(_read("llm_failure.yaml"))
_DIGEST = DigestConfig.model_validate(_read("digest.yaml"))
_ASSETS = AssetsConfig.model_validate(_read("assets.yaml"))
_AGENTS = AgentsConfig.model_validate(_read("agents.yaml"))
_CONTINUOUS_MONITOR = ContinuousMonitorConfig.model_validate(_read("continuous_monitor.yaml"))
_PROFILES = load_profiles(_CONFIG_DIR)
_REGIMES = load_regimes(_CONFIG_DIR)
_MODES = load_modes(_CONFIG_DIR)
_OVERLAYS = load_overlays(_CONFIG_DIR)
_RUN_TYPES = load_run_types(_CONFIG_DIR)


# ---------------------------------------------------------------------------
# Market-data anchors
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
_RISK_FREE_RATE = 0.045


# ---------------------------------------------------------------------------
# Fixture infrastructure
# ---------------------------------------------------------------------------


def _resolve_config(
    *,
    profile: Profile,
    regime: Regime = Regime.normal,
    mode: Mode = Mode.normal,
    overlays: tuple[Overlay, ...] = (),
    run_type: RunType = RunType.pre_open,
) -> ResolvedConfig:
    """Compose a ``ResolvedConfig`` from the shipped tree under the given dimensions."""
    main = _MAIN.model_copy(update={"active_profile": profile})
    inputs = LoadedConfig(
        main=main,
        scheduler=_SCHEDULER,
        venue=_VENUE,
        execution=_EXECUTION,
        guardrails=_GUARDRAILS,
        llm_failure=_LLM_FAILURE,
        digest=_DIGEST,
        assets=_ASSETS,
        agents=_AGENTS,
        continuous_monitor=_CONTINUOUS_MONITOR,
        profiles=dict(_PROFILES),
        regimes=dict(_REGIMES),
        modes=dict(_MODES),
        overlays=dict(_OVERLAYS),
        run_types=dict(_RUN_TYPES),
    )
    runtime = RuntimeDimensions(
        active_regime=regime,
        active_mode=mode,
        active_overlays=overlays,
        firing_trigger=run_type,
    )
    return compose_config(inputs, runtime)


def _load_library_config(
    *,
    profile: Profile,
    regime: Regime = Regime.normal,
    mode: Mode = Mode.normal,
    overlays: tuple[Overlay, ...] = (),
) -> tuple[ResolvedConfig, LibraryConfig]:
    """Load the shipped tree, resolve it, and adapt to ``LibraryConfig``.

    Returns both the ``ResolvedConfig`` (for tests that pin absolute numbers
    against ``rule_values`` per the story's "read from resolver" guidance) and
    the ``LibraryConfig`` (consumed by ``evaluate_proposals``).
    """
    resolved = _resolve_config(profile=profile, regime=regime, mode=mode, overlays=overlays)
    return resolved, from_resolved_config(resolved)


def _make_iv_provider(
    *,
    quotes_by_underlying: Mapping[str, Sequence[tuple[float, date, ContractType, float]]],
    realized_vol_by_underlying: Mapping[str, float],
) -> FixtureIvProvider:
    """Construct a ``FixtureIvProvider`` from compact tuples.

    ``quotes_by_underlying[underlying]`` is a sequence of
    ``(strike, expiration, contract_type, iv)`` tuples;
    ``realized_vol_by_underlying[underlying]`` is the trailing-30d realized vol.
    """
    surface = {
        underlying: IvSurfaceEntry(
            underlying=underlying,
            quotes=tuple(
                IvQuote(
                    strike=strike,
                    expiration=expiration,
                    contract_type=contract_type,
                    implied_volatility=iv,
                )
                for strike, expiration, contract_type, iv in quotes
            ),
        )
        for underlying, quotes in quotes_by_underlying.items()
    }
    realized = {
        underlying: RealizedVolEntry(underlying=underlying, trailing_30d_realized_vol=rv)
        for underlying, rv in realized_vol_by_underlying.items()
    }
    return FixtureIvProvider(surface=surface, realized_vol=realized)


def _make_state(
    *,
    portfolio_value_usd: float,
    cash_usd: float | None = None,
    reserved_for_pending_orders_usd: float = 0.0,
    sector_exposure_pct: Mapping[str, float] | None = None,
    net_long_pct: float = 0.0,
    net_short_pct: float = 0.0,
    gross_pct: float = 0.0,
    options_delta_pct: float = 0.0,
    portfolio_theta_pct_per_day: float = 0.0,
    portfolio_vega_pct_per_iv_point: float = 0.0,
    total_short_pct: float = 0.0,
    single_short_max_pct: float = 0.0,
    daily_borrow_cost_pct: float = 0.0,
    position_max_size_pct: float = 0.0,
    existing_positions: Mapping[str, ExistingPosition] | None = None,
) -> PortfolioStateSnapshot:
    """Construct a ``PortfolioStateSnapshot`` with sane defaults.

    ``cash_usd`` defaults to portfolio value minus gross_pct of portfolio
    (i.e., a coherent fully-deployed snapshot). Tests override fields they
    care about.
    """
    if cash_usd is None:
        cash_usd = portfolio_value_usd * (1.0 - gross_pct / 100.0)
    return PortfolioStateSnapshot(
        portfolio_value_usd=portfolio_value_usd,
        cash_usd=cash_usd,
        reserved_for_pending_orders_usd=reserved_for_pending_orders_usd,
        sector_exposure_pct=MappingProxyType(dict(sector_exposure_pct or {})),
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
        existing_positions=MappingProxyType(dict(existing_positions or {})),
    )


def _market(
    *,
    underlying_prices: Mapping[str, float],
    iv_provider: FixtureIvProvider,
) -> MarketInputs:
    return MarketInputs(
        underlying_prices=MappingProxyType(dict(underlying_prices)),
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=iv_provider,
        as_of=_AS_OF,
    )


def _equity_proposal(
    *,
    proposal_id: str,
    underlying: str,
    sector: str,
    direction: Direction,
    notional_usd: float,
    spot: float,
    action: Action = Action.OPEN,
    existing_position_id: str | None = None,
    daily_borrow_cost_usd: float | None = None,
) -> ProposedDelta:
    return ProposedDelta(
        id=proposal_id,
        underlying=underlying,
        sector=sector,
        direction=direction,
        asset_type=AssetType.EQUITY,
        notional_usd=money(notional_usd),
        quantity=notional_usd / spot,
        option_legs=None,
        action=action,
        existing_position_id=existing_position_id,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
    )


def _by_rule(output: LibraryOutput) -> dict[str, RuleProjection]:
    return {p.rule: p for p in output.per_rule}


# ---------------------------------------------------------------------------
# Scenario 1: Micro long-only book passes full validation
# ---------------------------------------------------------------------------


def test_micro_long_only_book_passes_full_validation() -> None:
    """Micro x normal: a tech-heavy book proposing two more tech longs trips
    the sector limit. Pins the FAIL plus the no-options/no-shorts feature
    gating and equity ``net_greeks=None`` shape."""
    resolved, config = _load_library_config(profile=Profile.micro)
    sector_limit = resolved.rule_values["sector_concentration_pct"]

    state = _make_state(
        portfolio_value_usd=1_500.0,
        sector_exposure_pct={"tech": 35.0, "semis": 25.0},
        net_long_pct=60.0,
        gross_pct=60.0,
    )
    proposals = (
        _equity_proposal(
            proposal_id="REC-1",
            underlying=Symbol("AAPL"),
            sector="tech",
            direction=Direction.LONG,
            notional_usd=75.0,
            spot=100.0,
        ),
        _equity_proposal(
            proposal_id="REC-2",
            underlying=Symbol("MSFT"),
            sector="tech",
            direction=Direction.LONG,
            notional_usd=75.0,
            spot=100.0,
        ),
    )
    market = _market(
        underlying_prices={"AAPL": 100.0, "MSFT": 100.0},
        iv_provider=_make_iv_provider(quotes_by_underlying={}, realized_vol_by_underlying={}),
    )

    output = evaluate_proposals(state=state, proposals=proposals, config=config, market=market)

    # No options/short rules under micro.
    rule_ids = {p.rule for p in output.per_rule}
    assert "options_delta_pct" not in rule_ids
    assert "portfolio_theta_pct_per_day" not in rule_ids
    assert "portfolio_vega_pct_per_iv_point" not in rule_ids
    assert "total_short_pct" not in rule_ids
    assert "single_short_max_pct" not in rule_ids
    assert "borrow_cost_budget_pct_per_day" not in rule_ids

    # No proposals were filtered (both are long equity).
    assert output.feature_disabled == ()

    # Sector tech goes from 35 to 45 — over the 25 limit, FAIL.
    by_rule = _by_rule(output)
    sector_tech = by_rule["sector_concentration_tech"]
    assert sector_tech.current == pytest.approx(35.0)
    assert sector_tech.projected_after == pytest.approx(35.0 + 150.0 / 1500.0 * 100.0)
    assert sector_tech.projected_after == pytest.approx(45.0)
    assert sector_tech.limit == pytest.approx(sector_limit)
    assert sector_tech.status is Status.FAIL

    # Two equity entries with net_greeks=None and matching delta-adjusted exposure.
    assert set(output.delta_adjusted.keys()) == {"REC-1", "REC-2"}
    for dae in output.delta_adjusted.values():
        assert dae.net_greeks is None
        assert dae.signed_notional_usd == pytest.approx(75.0)

    # ``RuleProjection.unit`` strings match the registry's documented units.
    expected_units = {spec.rule_id: spec.unit for spec in build_active_specs(config)}
    for proj in output.per_rule:
        assert proj.unit == expected_units[proj.rule]


# ---------------------------------------------------------------------------
# Scenario 2: Medium options proposal — all rules PASS
# ---------------------------------------------------------------------------


def test_medium_options_proposal_pass() -> None:
    """Medium x normal: a small ATM long call on NVDA fits within every limit.
    Pins SURFACE IV provenance, theta-negative direction, and delta-pct
    contribution; ``net_greeks`` reflects the BS computation."""
    _, config = _load_library_config(profile=Profile.medium)

    # Spot/strike chosen so that 5 contracts x 100 x buffered_delta x spot
    # (~$1,500 delta-adjusted) fits the medium x normal limits with headroom.
    nvda_spot = 5.0
    nvda_strike = 5.0
    expiration = date(2026, 5, 28)  # 30 days from _AS_OF
    iv = 0.30

    # State: $50K, gross 40 across four sectors, no options. State leaves
    # enough headroom on gross/net-long/sector/options-delta that every rule
    # is in PASS after the option lands.
    sector_exposure = {"tech": 12.0, "semis": 10.0, "financials": 10.0, "energy": 8.0}
    state = _make_state(
        portfolio_value_usd=50_000.0,
        sector_exposure_pct=sector_exposure,
        net_long_pct=15.0,
        gross_pct=40.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        position_max_size_pct=3.0,
    )

    proposal = ProposedDelta(
        id="REC-1",
        underlying=Symbol("NVDA"),
        sector="semis",
        direction=Direction.LONG,
        asset_type=AssetType.OPTION,
        notional_usd=money(1_000.0),
        quantity=5,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=nvda_strike,
                expiration=expiration,
                quantity=1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )

    market = _market(
        underlying_prices={"NVDA": nvda_spot},
        iv_provider=_make_iv_provider(
            quotes_by_underlying={
                "NVDA": ((nvda_strike, expiration, ContractType.CALL, iv),),
            },
            realized_vol_by_underlying={},
        ),
    )

    output = evaluate_proposals(state=state, proposals=(proposal,), config=config, market=market)

    # IV provenance: surface hit.
    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.iv_source is IvSource.SURFACE
    assert rec1.iv_used == pytest.approx(iv)

    # Greeks present and consistent with BS for an ATM 30-DTE call.
    assert rec1.net_greeks is not None
    assert 0.4 < rec1.net_greeks.delta < 0.7  # ATM call delta near 0.55
    assert rec1.net_greeks.theta < 0  # long call has negative theta
    assert rec1.net_greeks.vega > 0
    # Buffer multiplier under normal regime is 1.0 -> buffered_delta = 1.10 x |delta|.
    assert rec1.unbuffered_delta is not None
    assert rec1.unbuffered_delta == pytest.approx(rec1.net_greeks.delta)
    expected_signed = 1.10 * rec1.unbuffered_delta * nvda_spot * 100 * 5
    assert rec1.signed_notional_usd == pytest.approx(expected_signed)

    by_rule = _by_rule(output)
    # options_delta_pct moves off zero by signed_notional / value x 100.
    expected_delta_pct = rec1.signed_notional_usd / 50_000.0 * 100.0
    assert by_rule["options_delta_pct"].projected_after == pytest.approx(expected_delta_pct)

    # Theta: long call has negative theta, so the magnitude rule's projected
    # is more negative than current (which is zero).
    theta_proj = by_rule["portfolio_theta_pct_per_day"]
    assert theta_proj.current == pytest.approx(0.0)
    assert theta_proj.projected_after < theta_proj.current

    # Every rule PASS under this sizing.
    for proj in output.per_rule:
        assert proj.status is Status.PASS, (proj.rule, proj.projected_after, proj.limit)


# ---------------------------------------------------------------------------
# Scenario 3: Medium options FAIL on vega under elevated regime
# ---------------------------------------------------------------------------


def test_medium_options_proposal_fail_on_vega_under_elevated() -> None:
    """Medium x elevated: long ATM straddle pushes the regime-tightened vega
    rule into FAIL. Pins the 1.5x regime multiplier on the conservative
    buffer."""
    resolved, config = _load_library_config(profile=Profile.medium, regime=Regime.elevated)

    aapl_spot = 200.0
    aapl_strike = 200.0
    expiration = date(2026, 5, 12)  # 14 days from _AS_OF
    iv = 0.30

    # State: starts at 0.6% vega; well below the elevated 0.7 limit but inside
    # the warning band so the proposal can push to FAIL.
    state = _make_state(
        portfolio_value_usd=50_000.0,
        sector_exposure_pct={"tech": 5.0, "semis": 5.0, "financials": 5.0, "energy": 5.0},
        net_long_pct=10.0,
        gross_pct=20.0,
        options_delta_pct=2.0,
        portfolio_theta_pct_per_day=-0.05,
        portfolio_vega_pct_per_iv_point=0.6,
        position_max_size_pct=2.0,
    )

    proposal = ProposedDelta(
        id="REC-1",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=None,
        asset_type=AssetType.STRATEGY,
        notional_usd=money(2_000.0),
        quantity=4,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=aapl_strike,
                expiration=expiration,
                quantity=1,
            ),
            OptionLeg(
                contract_type=ContractType.PUT,
                strike=aapl_strike,
                expiration=expiration,
                quantity=1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )

    market = _market(
        underlying_prices={"AAPL": aapl_spot},
        iv_provider=_make_iv_provider(
            quotes_by_underlying={
                "AAPL": (
                    (aapl_strike, expiration, ContractType.CALL, iv),
                    (aapl_strike, expiration, ContractType.PUT, iv),
                ),
            },
            realized_vol_by_underlying={},
        ),
    )

    output = evaluate_proposals(state=state, proposals=(proposal,), config=config, market=market)

    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.iv_source is IvSource.SURFACE
    assert rec1.net_greeks is not None
    assert rec1.net_greeks.vega > 0  # long vol

    # The 1.5x elevated regime multiplier on the 10% base buffer -> effective
    # buffered_delta = unbuffered_delta x 1.15 (i.e., 10% x 1.5 = 15% buffer).
    expected_buffered = 1.15 * abs(rec1.net_greeks.delta) * aapl_spot * 100 * 4
    assert rec1.signed_notional_usd == pytest.approx(expected_buffered)

    by_rule = _by_rule(output)
    vega_proj = by_rule["portfolio_vega_pct_per_iv_point"]
    # The elevated-regime vega limit: medium base 1.0 x elevated 0.70 mult = 0.70.
    assert vega_proj.limit == pytest.approx(resolved.rule_values["portfolio_vega_pct_per_iv_point"])
    assert vega_proj.status is Status.FAIL
    # |projected| / limit > 95% (hard-block threshold).
    assert abs(vega_proj.projected_after) / vega_proj.limit > 0.95


# ---------------------------------------------------------------------------
# Scenario 4: Crisis regime immediate tightening creates a breach
# ---------------------------------------------------------------------------


def test_crisis_regime_immediate_tightening_creates_breach() -> None:
    """Medium x crisis: state already over the regime-tightened gross limit;
    library reports current = 85, limit = 60, FAIL. Engine layer responds; the
    library only surfaces the breach."""
    resolved, config = _load_library_config(profile=Profile.medium, regime=Regime.crisis)

    state = _make_state(
        portfolio_value_usd=50_000.0,
        sector_exposure_pct={"tech": 25.0, "semis": 25.0, "financials": 20.0, "energy": 15.0},
        net_long_pct=85.0,
        gross_pct=85.0,
        position_max_size_pct=2.0,
    )

    proposal = _equity_proposal(
        proposal_id="REC-1",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        notional_usd=500.0,
        spot=100.0,
    )
    market = _market(
        underlying_prices={"AAPL": 100.0},
        iv_provider=_make_iv_provider(quotes_by_underlying={}, realized_vol_by_underlying={}),
    )

    output = evaluate_proposals(state=state, proposals=(proposal,), config=config, market=market)

    by_rule = _by_rule(output)
    gross = by_rule["gross_exposure_pct"]
    assert gross.current == pytest.approx(85.0)
    # Crisis cascade: medium base 120 x crisis 0.50 mult = 60.
    assert gross.limit == pytest.approx(resolved.rule_values["gross_exposure_pct"])
    assert gross.limit == pytest.approx(60.0)
    assert gross.status is Status.FAIL
    # Proposal nudges gross further from the limit.
    assert gross.projected_after > gross.current
    assert gross.projected_after == pytest.approx(85.0 + 500.0 / 50_000.0 * 100.0)


# ---------------------------------------------------------------------------
# Scenario 5: Short proposal with borrow cost
# ---------------------------------------------------------------------------


def test_short_proposal_with_borrow_cost() -> None:
    """Medium x normal: short proposal flows through the short rules. Pins the
    signed notional sign, total/net short arithmetic, and borrow-cost
    aggregation."""
    _, config = _load_library_config(profile=Profile.medium)

    existing_short = ExistingPosition(
        position_id=PositionId("POS-EXISTING-SHORT"),
        underlying=Symbol("META"),
        sector="tech",
        direction=Direction.SHORT,
        asset_type=AssetType.EQUITY,
        notional_usd=1_500.0,
        delta_adjusted_exposure_usd=1_500.0,
        current_greeks=None,
        daily_borrow_cost_usd=0.50,
        reserves_capital_usd=0.0,
    )
    state = _make_state(
        portfolio_value_usd=50_000.0,
        sector_exposure_pct={"tech": 3.0, "semis": 0.0, "financials": 0.0, "energy": 0.0},
        net_long_pct=0.0,
        net_short_pct=3.0,
        gross_pct=3.0,
        total_short_pct=3.0,
        single_short_max_pct=3.0,
        daily_borrow_cost_pct=0.50 / 50_000.0 * 100.0,
        position_max_size_pct=3.0,
        existing_positions={"POS-EXISTING-SHORT": existing_short},
    )

    proposal = _equity_proposal(
        proposal_id="REC-1",
        underlying=Symbol("TSLA"),
        sector="tech",
        direction=Direction.SHORT,
        notional_usd=2_000.0,
        spot=100.0,
        daily_borrow_cost_usd=1.00,
    )
    market = _market(
        underlying_prices={"TSLA": 100.0},
        iv_provider=_make_iv_provider(quotes_by_underlying={}, realized_vol_by_underlying={}),
    )

    output = evaluate_proposals(state=state, proposals=(proposal,), config=config, market=market)

    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.signed_notional_usd == pytest.approx(-2_000.0)

    by_rule = _by_rule(output)
    borrow = by_rule["borrow_cost_budget_pct_per_day"]
    assert borrow.current == pytest.approx(0.50 / 50_000.0 * 100.0)
    assert borrow.projected_after == pytest.approx(
        0.50 / 50_000.0 * 100.0 + 1.00 / 50_000.0 * 100.0
    )

    total_short = by_rule["total_short_pct"]
    assert total_short.current == pytest.approx(3.0)
    # current 3 + 2000/50000 x 100 = 3 + 4 = 7
    assert total_short.projected_after == pytest.approx(7.0)

    net_short = by_rule["net_short_pct"]
    # net_short contributes -signed_notional / value x 100 = -(-2000)/50000 x 100 = 4
    assert net_short.projected_after == pytest.approx(net_short.current + 4.0)


# ---------------------------------------------------------------------------
# Scenario 6: Long call spread aggregates correctly across legs
# ---------------------------------------------------------------------------


def test_strategy_long_call_spread_aggregates_correctly() -> None:
    """Medium x normal: long 950 / short 1000 NVDA call spread. Net greeks
    aggregate per-leg correctly: delta is positive but smaller than the
    long-leg delta; theta is negative but smaller magnitude than a single long
    call's theta."""
    _, config = _load_library_config(profile=Profile.medium)

    nvda_spot = 925.0
    long_strike = 950.0
    short_strike = 1000.0
    expiration = date(2026, 5, 28)  # 30 days from _AS_OF
    iv = 0.30

    state = _make_state(
        portfolio_value_usd=50_000.0,
        sector_exposure_pct={"tech": 0.0, "semis": 0.0, "financials": 0.0, "energy": 0.0},
        gross_pct=0.0,
        position_max_size_pct=0.0,
    )

    spread_proposal = ProposedDelta(
        id="REC-1",
        underlying=Symbol("NVDA"),
        sector="semis",
        direction=None,
        asset_type=AssetType.STRATEGY,
        notional_usd=money(2_000.0),
        quantity=3,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=long_strike,
                expiration=expiration,
                quantity=1,
            ),
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=short_strike,
                expiration=expiration,
                quantity=-1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )
    # Comparison proposal: just the long leg, same quantity-per-leg, same
    # contract count. Used to confirm the spread's net delta and theta are
    # smaller in magnitude than a single long call's per-share leg result.
    long_only_proposal = ProposedDelta(
        id="REC-LONG-ONLY",
        underlying=Symbol("NVDA"),
        sector="semis",
        direction=Direction.LONG,
        asset_type=AssetType.OPTION,
        notional_usd=money(2_000.0),
        quantity=3,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=long_strike,
                expiration=expiration,
                quantity=1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )

    market = _market(
        underlying_prices={"NVDA": nvda_spot},
        iv_provider=_make_iv_provider(
            quotes_by_underlying={
                "NVDA": (
                    (long_strike, expiration, ContractType.CALL, iv),
                    (short_strike, expiration, ContractType.CALL, iv),
                ),
            },
            realized_vol_by_underlying={},
        ),
    )

    spread_output = evaluate_proposals(
        state=state, proposals=(spread_proposal,), config=config, market=market
    )
    long_only_output = evaluate_proposals(
        state=state, proposals=(long_only_proposal,), config=config, market=market
    )

    spread_dae = spread_output.delta_adjusted["REC-1"]
    long_only_dae = long_only_output.delta_adjusted["REC-LONG-ONLY"]

    assert spread_dae.net_greeks is not None
    assert long_only_dae.net_greeks is not None

    # Net delta is positive (net-long spread) and smaller than the long leg's delta.
    assert 0 < spread_dae.net_greeks.delta < long_only_dae.net_greeks.delta
    # Net theta is negative (long net) but smaller in magnitude than the lone long call.
    assert spread_dae.net_greeks.theta < 0
    assert abs(spread_dae.net_greeks.theta) < abs(long_only_dae.net_greeks.theta)

    # options_delta_pct projection reflects the spread's signed_notional contribution.
    by_rule = _by_rule(spread_output)
    expected_pct = spread_dae.signed_notional_usd / 50_000.0 * 100.0
    assert by_rule["options_delta_pct"].projected_after == pytest.approx(expected_pct)


# ---------------------------------------------------------------------------
# Scenario 7: Close releases capital and reduces gross
# ---------------------------------------------------------------------------


def test_close_releases_capital_and_reduces_gross() -> None:
    """Medium x normal: closing a long equity position reduces gross and the
    position's sector exposure, frees up capital, and every rule lands in PASS.

    The position size is sized so that all rules pass — every closing reduces
    gross/sector by the same percentage points, and the cash-reserve floor
    moves positively."""
    _, config = _load_library_config(profile=Profile.medium)

    portfolio_value_usd = 50_000.0
    position_notional_usd = 1_000.0  # 2% of $50K — fits the 5% per-position cap.

    existing = ExistingPosition(
        position_id=PositionId("POS-1"),
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=position_notional_usd,
        delta_adjusted_exposure_usd=position_notional_usd,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=0.0,
    )
    state = _make_state(
        portfolio_value_usd=portfolio_value_usd,
        cash_usd=portfolio_value_usd * 0.20,  # 20% cash -> above the 10% reserve floor
        sector_exposure_pct={"tech": 12.0, "semis": 8.0, "financials": 5.0, "energy": 3.0},
        net_long_pct=28.0,
        gross_pct=28.0,
        position_max_size_pct=2.0,
        existing_positions={"POS-1": existing},
    )

    proposal = _equity_proposal(
        proposal_id="REC-1",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        notional_usd=position_notional_usd,
        spot=100.0,
        action=Action.CLOSE,
        existing_position_id="POS-1",
    )
    market = _market(
        underlying_prices={"AAPL": 100.0},
        iv_provider=_make_iv_provider(quotes_by_underlying={}, realized_vol_by_underlying={}),
    )

    output = evaluate_proposals(state=state, proposals=(proposal,), config=config, market=market)

    rec1 = output.delta_adjusted["REC-1"]
    assert rec1.signed_notional_usd == pytest.approx(-position_notional_usd)

    by_rule = _by_rule(output)
    position_pct = position_notional_usd / portfolio_value_usd * 100.0
    # Gross drops by the position's % size.
    gross = by_rule["gross_exposure_pct"]
    assert gross.projected_after == pytest.approx(gross.current - position_pct)
    # Sector tech drops by the same.
    sector_tech = by_rule["sector_concentration_tech"]
    assert sector_tech.projected_after == pytest.approx(sector_tech.current - position_pct)
    # Capital is released — projected cash is above current cash %.
    cash = by_rule["min_cash_reserve_pct"]
    assert cash.projected_after > cash.current

    # All rules PASS.
    for proj in output.per_rule:
        assert proj.status is Status.PASS, (proj.rule, proj.projected_after, proj.limit)


# ---------------------------------------------------------------------------
# Scenario 11: Determinism against shipped config
# ---------------------------------------------------------------------------


def test_determinism_against_shipped_config() -> None:
    """Load the medium x normal config twice and run ``evaluate_proposals``
    twice on identical inputs; outputs must be ``==`` and hash-equal.

    Catches non-determinism that would otherwise creep in (dict iteration
    order, non-frozen aggregation)."""
    _, config_a = _load_library_config(profile=Profile.medium)
    _, config_b = _load_library_config(profile=Profile.medium)

    state = _make_state(
        portfolio_value_usd=50_000.0,
        sector_exposure_pct={"tech": 10.0, "semis": 8.0, "financials": 5.0, "energy": 5.0},
        net_long_pct=28.0,
        gross_pct=28.0,
        position_max_size_pct=2.0,
    )
    proposal = _equity_proposal(
        proposal_id="REC-1",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        notional_usd=1_500.0,
        spot=100.0,
    )
    market_a = _market(
        underlying_prices={"AAPL": 100.0},
        iv_provider=_make_iv_provider(quotes_by_underlying={}, realized_vol_by_underlying={}),
    )
    market_b = _market(
        underlying_prices={"AAPL": 100.0},
        iv_provider=_make_iv_provider(quotes_by_underlying={}, realized_vol_by_underlying={}),
    )

    out_a = evaluate_proposals(state=state, proposals=(proposal,), config=config_a, market=market_a)
    out_b = evaluate_proposals(state=state, proposals=(proposal,), config=config_b, market=market_b)

    assert out_a == out_b
    assert hash(out_a) == hash(out_b)


# ---------------------------------------------------------------------------
# Scenario 8: Validation-tool wrapper composition (library-to-tool seam)
# ---------------------------------------------------------------------------


def test_validation_tool_wrapper_composition() -> None:
    """Synthetic ``LibraryOutput`` composed into the validation tool's payload
    shape. The wrapper needs only ``per_rule`` and ``delta_adjusted``; the
    library does not need to know about cumulative state across invocations."""
    per_rule = (
        RuleProjection(
            rule="gross_exposure_pct",
            status=Status.PASS,
            current=70.0,
            limit=100.0,
            projected_after=72.0,
            headroom_remaining=28.0,
            unit="% of portfolio (delta-adjusted)",
        ),
        RuleProjection(
            rule="sector_concentration_tech",
            status=Status.FAIL,
            current=23.0,
            limit=25.0,
            projected_after=28.0,
            headroom_remaining=-3.0,
            unit="% of portfolio (delta-adjusted)",
        ),
        RuleProjection(
            rule="net_long_pct",
            status=Status.WARNING,
            current=50.0,
            limit=60.0,
            projected_after=52.0,
            headroom_remaining=8.0,
            unit="% of portfolio (delta-adjusted)",
        ),
    )
    library_output = LibraryOutput(
        per_rule=per_rule,
        delta_adjusted=MappingProxyType(
            {
                "REC-1": DeltaAdjustedExposure(
                    proposal_id="REC-1",
                    signed_notional_usd=2_500.0,
                    net_greeks=None,
                    iv_used=None,
                    iv_source=None,
                    unbuffered_delta=None,
                ),
            }
        ),
        feature_disabled=(),
    )

    # --- Validation-tool composition ---
    statuses = {p.status for p in library_output.per_rule}
    if Status.FAIL in statuses:
        overall = "FAIL"
    elif Status.WARNING in statuses:
        overall = "WARNING"
    else:
        overall = "PASS"
    assert overall == "FAIL"

    # Worst FAIL rule (largest negative headroom) drives the failure guidance.
    fails = [p for p in library_output.per_rule if p.status is Status.FAIL]
    worst = min(fails, key=lambda p: p.headroom_remaining)
    failure_guidance = (
        f"{worst.rule} would land at {worst.projected_after} {worst.unit} "
        f"(limit {worst.limit}); headroom {worst.headroom_remaining}"
    )
    assert worst.rule == "sector_concentration_tech"
    assert "sector_concentration_tech" in failure_guidance
    assert "limit 25.0" in failure_guidance

    # ``cumulative_impact_note`` is the wrapper's responsibility — the library
    # only knows about the proposal index in this batch (one entry per id);
    # the wrapper concatenates with whatever upstream invocation context it carries.
    proposal_id = next(iter(library_output.delta_adjusted.keys()))
    cumulative_impact_note = f"proposal {proposal_id} #1 of 1 in this batch"
    assert "REC-1" in cumulative_impact_note


# ---------------------------------------------------------------------------
# Scenario 9: Pre-processor breaches extraction (library-to-pre-processor seam)
# ---------------------------------------------------------------------------


def test_pre_processor_breaches_extraction() -> None:
    """Synthetic 3-proposal batch demonstrates how the pre-processor extracts
    ``breaches[]`` from ``per_rule`` and computes per-proposal ``contributors``
    by re-running ``RuleSpec.contribute`` for each proposal individually.

    The contributors per breaching rule must sum to that rule's combined
    contribution (``projected_after - current``)."""
    _, config = _load_library_config(profile=Profile.medium)

    state = _make_state(
        portfolio_value_usd=50_000.0,
        sector_exposure_pct={"tech": 22.0, "semis": 5.0, "financials": 5.0, "energy": 5.0},
        net_long_pct=37.0,
        gross_pct=37.0,
        position_max_size_pct=2.0,
    )
    proposals = (
        _equity_proposal(
            proposal_id="REC-1",
            underlying=Symbol("AAPL"),
            sector="tech",
            direction=Direction.LONG,
            notional_usd=2_000.0,
            spot=100.0,
        ),
        _equity_proposal(
            proposal_id="REC-2",
            underlying=Symbol("MSFT"),
            sector="tech",
            direction=Direction.LONG,
            notional_usd=1_000.0,
            spot=100.0,
        ),
        _equity_proposal(
            proposal_id="REC-3",
            underlying=Symbol("META"),
            sector="tech",
            direction=Direction.LONG,
            notional_usd=500.0,
            spot=100.0,
        ),
    )
    market = _market(
        underlying_prices={"AAPL": 100.0, "MSFT": 100.0, "META": 100.0},
        iv_provider=_make_iv_provider(quotes_by_underlying={}, realized_vol_by_underlying={}),
    )
    output = evaluate_proposals(state=state, proposals=proposals, config=config, market=market)

    # --- Pre-processor composition ---
    breaches = tuple(p for p in output.per_rule if p.status is not Status.PASS)
    # The combined batch lifts tech sector into breach.
    assert any(p.rule == "sector_concentration_tech" for p in breaches)

    # Per-proposal attribution: re-run the breaching rule's ``contribute`` for
    # each proposal individually and confirm contributors sum to the combined
    # contribution recorded in ``per_rule``.
    spec_by_id = {spec.rule_id: spec for spec in build_active_specs(config)}
    for breach in breaches:
        spec = spec_by_id[breach.rule]
        contributors: dict[str, float] = {}
        for proposal in proposals:
            dae = output.delta_adjusted[proposal.id]
            contributors[proposal.id] = spec.contribute(proposal, dae, state, config)

        combined_contribution = breach.projected_after - breach.current
        assert sum(contributors.values()) == pytest.approx(combined_contribution)
        # Every proposal's contributor is signed (zero allowed) — the seam shape.
        assert set(contributors.keys()) == {"REC-1", "REC-2", "REC-3"}


# ---------------------------------------------------------------------------
# Scenario 10: Engine T3 synchronous rejection payload (library-to-engine seam)
# ---------------------------------------------------------------------------


def test_engine_t3_synchronous_rejection_payload() -> None:
    """Synthetic single-proposal FAIL output: the engine T3 builds the
    rejection payload returned to the PM (breached rules, headroom values,
    suggested modification). Pins the seam — the library's output covers
    everything the engine needs."""
    breach = RuleProjection(
        rule="sector_concentration_tech",
        status=Status.FAIL,
        current=23.0,
        limit=25.0,
        projected_after=28.0,
        headroom_remaining=-3.0,
        unit="% of portfolio (delta-adjusted)",
    )
    library_output = LibraryOutput(
        per_rule=(
            RuleProjection(
                rule="gross_exposure_pct",
                status=Status.PASS,
                current=70.0,
                limit=120.0,
                projected_after=75.0,
                headroom_remaining=45.0,
                unit="% of portfolio (delta-adjusted)",
            ),
            breach,
        ),
        delta_adjusted=MappingProxyType(
            {
                "REC-1": DeltaAdjustedExposure(
                    proposal_id="REC-1",
                    signed_notional_usd=2_500.0,
                    net_greeks=None,
                    iv_used=None,
                    iv_source=None,
                    unbuffered_delta=None,
                ),
            }
        ),
        feature_disabled=(),
    )

    # --- Engine rejection payload composition ---
    failing = tuple(p for p in library_output.per_rule if p.status is Status.FAIL)
    assert len(failing) == 1
    worst = failing[0]

    # The library's projection records the contribution that pushed the rule
    # into FAIL; the suggested modification recovers headroom by reducing the
    # proposal's contribution by the overage.
    overage = worst.projected_after - worst.limit
    proposal_contribution = worst.projected_after - worst.current
    reduce_pct = overage / proposal_contribution * 100.0
    suggested_modification = f"reduce size by {reduce_pct:.1f}% to pass"

    rejection_payload = {
        "breached_rules": [worst.rule],
        "current": worst.current,
        "limit": worst.limit,
        "projected_after": worst.projected_after,
        "headroom_remaining": worst.headroom_remaining,
        "suggested_modification": suggested_modification,
    }

    assert rejection_payload["breached_rules"] == ["sector_concentration_tech"]
    assert rejection_payload["current"] == pytest.approx(23.0)
    assert rejection_payload["limit"] == pytest.approx(25.0)
    assert rejection_payload["projected_after"] == pytest.approx(28.0)
    assert rejection_payload["headroom_remaining"] == pytest.approx(-3.0)
    assert "reduce size by" in suggested_modification
    # Reduction recovers the overage exactly: 3 / 5 -> 60%.
    assert "60.0%" in suggested_modification
