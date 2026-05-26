"""Tests for exposure rule specs (story 04).

Each exposure rule's ``read_current`` and ``contribute`` is unit-tested
against synthetic state and proposals, asserting the documented arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pytest

from alphamind._kernel.ids import Symbol
from alphamind._kernel.money import money
from alphamind.risk_guardrails.guardrail_evaluation import (
    Action,
    AssetType,
    DeltaAdjustedExposure,
    Direction,
    EscalationZones,
    ExistingPosition,
    FeatureFlagsView,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
    RuleSpec,
    build_active_specs,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _config(
    *,
    effective_limits: Mapping[str, float] | None = None,
    active_sectors: tuple[str, ...] = ("tech", "semis"),
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
) -> LibraryConfig:
    if effective_limits is None:
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
    escalation_zones = MappingProxyType({key: _zones() for key in effective_limits})
    return LibraryConfig(
        effective_limits=MappingProxyType(dict(effective_limits)),
        escalation_zones=escalation_zones,
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
    net_long_pct: float = 40.0,
    net_short_pct: float = 0.0,
    gross_pct: float = 50.0,
    position_max_size_pct: float = 5.0,
    single_short_max_pct: float = 0.0,
    total_short_pct: float = 0.0,
    daily_borrow_cost_pct: float = 0.0,
    options_delta_pct: float = 0.0,
    portfolio_theta_pct_per_day: float = 0.0,
    portfolio_vega_pct_per_iv_point: float = 0.0,
    cash_usd: float = 20_000.0,
    reserved_for_pending_orders_usd: float = 0.0,
    existing_positions: Mapping[str, ExistingPosition] | None = None,
) -> PortfolioStateSnapshot:
    if sector_exposure_pct is None:
        sector_exposure_pct = {"tech": 18.0, "semis": 12.0}
    if existing_positions is None:
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


def _proposal(
    *,
    proposal_id: str = "P-1",
    sector: str = "tech",
    direction: Direction = Direction.LONG,
    asset_type: AssetType = AssetType.EQUITY,
    notional_usd: float = 5_000.0,
    quantity: float = 50.0,
    action: Action = Action.OPEN,
    existing_position_id: str | None = None,
    daily_borrow_cost_usd: float | None = None,
    reserves_capital: bool = False,
) -> ProposedDelta:
    return ProposedDelta(
        id=proposal_id,
        underlying=Symbol("ABC"),
        sector=sector,
        direction=direction,
        asset_type=asset_type,
        notional_usd=money(notional_usd),
        quantity=quantity,
        option_legs=None,
        action=action,
        existing_position_id=existing_position_id,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
        reserves_capital=reserves_capital,
    )


def _dae(*, signed_notional_usd: float = 5_000.0) -> DeltaAdjustedExposure:
    return DeltaAdjustedExposure(
        proposal_id="P-1",
        signed_notional_usd=signed_notional_usd,
        net_greeks=None,
        iv_used=None,
        iv_source=None,
        unbuffered_delta=None,
    )


def _existing(
    *,
    position_id: str = "POS-1",
    direction: Direction = Direction.LONG,
    asset_type: AssetType = AssetType.EQUITY,
    notional_usd: float = 8_000.0,
    delta_adjusted_exposure_usd: float | None = None,
    daily_borrow_cost_usd: float | None = None,
    reserves_capital_usd: float = 0.0,
) -> ExistingPosition:
    return ExistingPosition(
        position_id=position_id,
        underlying=Symbol("ABC"),
        sector="tech",
        direction=direction,
        asset_type=asset_type,
        notional_usd=notional_usd,
        delta_adjusted_exposure_usd=(
            notional_usd if delta_adjusted_exposure_usd is None else delta_adjusted_exposure_usd
        ),
        current_greeks=None,
        daily_borrow_cost_usd=daily_borrow_cost_usd,
        reserves_capital_usd=reserves_capital_usd,
    )


def _spec_by_id(specs: tuple[RuleSpec, ...], rule_id: str) -> RuleSpec:
    for spec in specs:
        if spec.rule_id == rule_id:
            return spec
    raise KeyError(rule_id)


# ---------------------------------------------------------------------------
# position_max_size_pct
#
# ALP-621: the rule uses a holistic ``project_after_batch`` projector that
# simulates the post-batch position book to compute the new max. The per-
# proposal ``contribute`` is a no-op marker; the projection engine bypasses
# it whenever ``project_after_batch`` is set. Tests below target the holistic
# projector directly (the field on ``RuleSpec``).
# ---------------------------------------------------------------------------


def test_position_max_size_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(position_max_size_pct=7.5)
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.read_current(state, config) == 7.5


def test_position_max_size_no_op_contribute_is_zero() -> None:
    """ALP-621: ``contribute`` is a no-op marker for ``position_max_size_pct``.
    The projection engine bypasses it whenever ``project_after_batch`` is
    set on the RuleSpec; the callable exists only to keep ``contribute``
    required on every spec."""
    config = _config()
    state = _snapshot(position_max_size_pct=8.0, portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    proposal = _proposal(action=Action.OPEN, notional_usd=5_000.0)
    dae = _dae(signed_notional_usd=5_000.0)
    assert spec.contribute(proposal, dae, state, config) == 0.0


def test_position_max_size_project_after_batch_is_wired() -> None:
    """ALP-621: the rule's RuleSpec wires ``project_after_batch`` so the
    engine takes the holistic-projection path."""
    config = _config()
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None


def _five_position_existings() -> dict[str, ExistingPosition]:
    """Five-position book at [18.1, 17.7, 14.2, 13.3, 7.1]% of $100k."""
    return {
        f"POS-{i}": _existing(
            position_id=f"POS-{i}",
            direction=Direction.LONG,
            notional_usd=notional,
        )
        for i, notional in enumerate([18_100.0, 17_700.0, 14_200.0, 13_300.0, 7_100.0], start=1)
    }


def test_position_max_size_project_after_batch_close_only_oversized_cures() -> None:
    """ALP-621 cure scenario: 5 positions [18.1, 17.7, 14.2, 13.3, 7.1]%
    with cap 10%; batch CLOSE the 4 oversized positions → projected_after
    drops to the surviving 7.1% position.
    """
    config = _config()  # position_max_size_pct limit = 10.0%
    state = _snapshot(
        position_max_size_pct=18.1,
        portfolio_value_usd=100_000.0,
        existing_positions=_five_position_existings(),
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(
                proposal_id=f"P-{i}",
                action=Action.CLOSE,
                notional_usd=notional,
                existing_position_id=f"POS-{i}",
            ),
            _dae(signed_notional_usd=-notional),
        )
        for i, notional in enumerate([18_100.0, 17_700.0, 14_200.0, 13_300.0], start=1)
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(7.1)
    assert projected_after < config.effective_limits["position_max_size_pct"]


def test_position_max_size_project_after_batch_close_single_max_drops_to_next_largest() -> None:
    """ALP-621: CLOSE the single 18% position only; second is 8% (below cap).
    Post-batch projected_after = 8.0 → PASS."""
    config = _config()  # cap 10%
    existings = {
        "POS-1": _existing(position_id="POS-1", direction=Direction.LONG, notional_usd=18_000.0),
        "POS-2": _existing(position_id="POS-2", direction=Direction.LONG, notional_usd=8_000.0),
        "POS-3": _existing(position_id="POS-3", direction=Direction.LONG, notional_usd=5_000.0),
    }
    state = _snapshot(
        position_max_size_pct=18.0,
        portfolio_value_usd=100_000.0,
        existing_positions=existings,
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(action=Action.CLOSE, notional_usd=18_000.0, existing_position_id="POS-1"),
            _dae(signed_notional_usd=-18_000.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(8.0)


def test_position_max_size_project_after_batch_close_single_still_fails() -> None:
    """ALP-621: CLOSE the 18% position only; second is 12% (still above cap).
    Post-batch projected_after = 12.0 → still FAIL (12 > 10)."""
    config = _config()  # cap 10%
    existings = {
        "POS-1": _existing(position_id="POS-1", direction=Direction.LONG, notional_usd=18_000.0),
        "POS-2": _existing(position_id="POS-2", direction=Direction.LONG, notional_usd=12_000.0),
        "POS-3": _existing(position_id="POS-3", direction=Direction.LONG, notional_usd=5_000.0),
    }
    state = _snapshot(
        position_max_size_pct=18.0,
        portfolio_value_usd=100_000.0,
        existing_positions=existings,
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(action=Action.CLOSE, notional_usd=18_000.0, existing_position_id="POS-1"),
            _dae(signed_notional_usd=-18_000.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(12.0)
    assert projected_after > config.effective_limits["position_max_size_pct"]


def test_position_max_size_project_after_batch_partial_close_uses_close_size() -> None:
    """ALP-621 Finding 4: a partial CLOSE (proposal.notional_usd < position
    notional) reduces the position by the close size, not by the full
    position. Starting 18% + 8% positions; close 9k of the 18% one →
    9k remains; max = 9.0% > 8% other position."""
    config = _config()
    existings = {
        "POS-1": _existing(position_id="POS-1", direction=Direction.LONG, notional_usd=18_000.0),
        "POS-2": _existing(position_id="POS-2", direction=Direction.LONG, notional_usd=8_000.0),
    }
    state = _snapshot(
        position_max_size_pct=18.0,
        portfolio_value_usd=100_000.0,
        existing_positions=existings,
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(action=Action.CLOSE, notional_usd=9_000.0, existing_position_id="POS-1"),
            _dae(signed_notional_usd=-9_000.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(9.0)


def test_position_max_size_project_after_batch_add_on_existing_max() -> None:
    """ALP-621 Finding 5: ADD on an existing max position increases its
    notional by the proposal's notional. 18% + 8% positions; ADD 2k to
    POS-1 → POS-1 grows to 20k = 20%."""
    config = _config()
    existings = {
        "POS-1": _existing(position_id="POS-1", direction=Direction.LONG, notional_usd=18_000.0),
        "POS-2": _existing(position_id="POS-2", direction=Direction.LONG, notional_usd=8_000.0),
    }
    state = _snapshot(
        position_max_size_pct=18.0,
        portfolio_value_usd=100_000.0,
        existing_positions=existings,
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(action=Action.ADD, notional_usd=2_000.0, existing_position_id="POS-1"),
            _dae(signed_notional_usd=2_000.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(20.0)


def test_position_max_size_project_after_batch_close_only_position_yields_zero() -> None:
    """ALP-621: CLOSE the only existing position → empty post-batch book →
    projected_after = 0."""
    config = _config()
    existings = {
        "POS-ONLY": _existing(
            position_id="POS-ONLY",
            direction=Direction.LONG,
            notional_usd=18_100.0,
        ),
    }
    state = _snapshot(
        position_max_size_pct=18.1,
        portfolio_value_usd=100_000.0,
        existing_positions=existings,
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(action=Action.CLOSE, notional_usd=18_100.0, existing_position_id="POS-ONLY"),
            _dae(signed_notional_usd=-18_100.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == 0.0


def test_position_max_size_project_after_batch_zero_portfolio_value_returns_current() -> None:
    """ALP-621 Finding 3: zero portfolio value guards against division-by-zero
    — the projector returns ``current`` (the snapshot's value) instead of
    raising."""
    config = _config()
    state = _snapshot(
        position_max_size_pct=0.0,
        portfolio_value_usd=0.0,
        cash_usd=0.0,
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    # An OPEN proposal regardless — the projector must not divide by zero.
    proposals = [
        (
            _proposal(action=Action.OPEN, notional_usd=5_000.0),
            _dae(signed_notional_usd=5_000.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == 0.0  # state.position_max_size_pct (current)


def test_position_max_size_project_after_batch_close_unresolved_id_is_no_op() -> None:
    """ALP-621: a CLOSE with an unresolved existing_position_id is a defensive
    no-op (validation should have caught it upstream). Projected_after equals
    the unchanged book's max."""
    config = _config()
    existings = {
        "POS-1": _existing(position_id="POS-1", direction=Direction.LONG, notional_usd=8_000.0),
    }
    state = _snapshot(
        position_max_size_pct=8.0,
        portfolio_value_usd=100_000.0,
        existing_positions=existings,
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(
                action=Action.CLOSE, notional_usd=5_000.0, existing_position_id="POS-UNKNOWN"
            ),
            _dae(signed_notional_usd=-5_000.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(8.0)


def test_apply_proposal_to_book_logs_warning_on_unresolved_position(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ALP-621 follow-up: the defensive no-op for an unresolved
    existing_position_id emits a WARNING so the silent skip becomes
    observable. The early-return behavior itself is unchanged — covered by
    ``test_position_max_size_project_after_batch_close_unresolved_id_is_no_op``.
    """
    config = _config()
    existings = {
        "POS-1": _existing(position_id="POS-1", direction=Direction.LONG, notional_usd=8_000.0),
    }
    state = _snapshot(
        position_max_size_pct=8.0,
        portfolio_value_usd=100_000.0,
        existing_positions=existings,
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(
                action=Action.CLOSE, notional_usd=5_000.0, existing_position_id="POS-UNKNOWN"
            ),
            _dae(signed_notional_usd=-5_000.0),
        )
    ]
    with caplog.at_level(
        "WARNING", logger="alphamind.risk_guardrails.guardrail_evaluation.rules.exposure"
    ):
        spec.project_after_batch(proposals, state, config)
    matches = [r for r in caplog.records if "POS-UNKNOWN" in r.getMessage()]
    assert matches, f"expected a warning naming POS-UNKNOWN; got {caplog.records!r}"
    record = matches[0]
    assert record.levelname == "WARNING"
    message = record.getMessage()
    assert "CLOSE" in message
    assert "unresolved" in message.lower()


def test_position_max_size_project_after_batch_cancel_does_not_change_book() -> None:
    """ALP-621: CANCEL releases reserved capital but doesn't change
    open-position notional. Post-batch max equals existing max."""
    config = _config()
    existings = {
        "POS-1": _existing(position_id="POS-1", direction=Direction.LONG, notional_usd=8_000.0),
    }
    state = _snapshot(
        position_max_size_pct=8.0,
        portfolio_value_usd=100_000.0,
        existing_positions=existings,
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(action=Action.CANCEL, existing_position_id="POS-1"),
            _dae(signed_notional_usd=0.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(8.0)


def test_position_max_size_project_after_batch_adjust_shrink_current_max() -> None:
    """ALP-621: ADJUST that shrinks the current-max position; next-largest
    becomes the new max. Position dropped from 18.1% to 12.0%; next-largest
    is 17.7% → new max = 17.7%."""
    config = _config()
    state = _snapshot(
        position_max_size_pct=18.1,
        portfolio_value_usd=100_000.0,
        existing_positions=_five_position_existings(),
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(
                action=Action.ADJUST,
                notional_usd=12_000.0,
                existing_position_id="POS-1",
            ),
            _dae(signed_notional_usd=0.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(17.7)


def test_position_max_size_project_after_batch_adjust_grow_current_max() -> None:
    """ALP-621: ADJUST that grows the current-max from 18.1% to 22.0%
    → new max = 22.0%."""
    config = _config()
    state = _snapshot(
        position_max_size_pct=18.1,
        portfolio_value_usd=100_000.0,
        existing_positions=_five_position_existings(),
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(
                action=Action.ADJUST,
                notional_usd=22_000.0,
                existing_position_id="POS-1",
            ),
            _dae(signed_notional_usd=0.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(22.0)


def test_position_max_size_project_after_batch_adjust_bracket_on_max_unchanged() -> None:
    """ALP-698: a translator-shape adjust-bracket proposal on the current-max
    position carries ``notional_usd == existing.notional_usd``, so the
    simulator's "set new total" branch is a no-op and the projected max
    equals the current max."""
    config = _config()
    state = _snapshot(
        position_max_size_pct=18.1,
        portfolio_value_usd=100_000.0,
        existing_positions=_five_position_existings(),
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(
                action=Action.ADJUST,
                notional_usd=18_100.0,  # = existing POS-1 notional (translator shape)
                existing_position_id="POS-1",
            ),
            _dae(signed_notional_usd=0.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(18.1)


def test_position_max_size_project_after_batch_open_adds_synthetic_position() -> None:
    """ALP-621: an OPEN adds a synthetic post-batch position. Book starts
    empty; OPEN 5k → projected_after = 5.0%."""
    config = _config()
    state = _snapshot(
        position_max_size_pct=0.0,
        portfolio_value_usd=100_000.0,
        existing_positions={},
    )
    spec = _spec_by_id(build_active_specs(config), "position_max_size_pct")
    assert spec.project_after_batch is not None
    proposals = [
        (
            _proposal(action=Action.OPEN, notional_usd=5_000.0),
            _dae(signed_notional_usd=5_000.0),
        )
    ]
    projected_after = spec.project_after_batch(proposals, state, config)
    assert projected_after == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# sector_concentration_{sector}
# ---------------------------------------------------------------------------


def test_sector_concentration_read_current_uses_sector_key() -> None:
    config = _config(active_sectors=("tech", "semis"))
    state = _snapshot(sector_exposure_pct={"tech": 20.0, "semis": 10.0})
    specs = build_active_specs(config)
    tech_spec = _spec_by_id(specs, "sector_concentration_tech")
    semis_spec = _spec_by_id(specs, "sector_concentration_semis")
    assert tech_spec.read_current(state, config) == 20.0
    assert semis_spec.read_current(state, config) == 10.0


def test_sector_concentration_contribute_signed_for_matching_sector() -> None:
    config = _config(active_sectors=("tech",))
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "sector_concentration_tech")
    proposal = _proposal(sector="tech", action=Action.OPEN, notional_usd=5_000.0)
    dae = _dae(signed_notional_usd=5_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(5.0)


def test_sector_concentration_contribute_zero_for_other_sector() -> None:
    config = _config(active_sectors=("tech",))
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "sector_concentration_tech")
    proposal = _proposal(sector="energy", action=Action.OPEN, notional_usd=5_000.0)
    dae = _dae(signed_notional_usd=5_000.0)
    assert spec.contribute(proposal, dae, state, config) == 0.0


def test_sector_concentration_contribute_negative_for_close() -> None:
    """CLOSE in the sector reduces concentration via signed_notional_usd<0."""
    config = _config(active_sectors=("tech",))
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "sector_concentration_tech")
    proposal = _proposal(
        sector="tech",
        action=Action.CLOSE,
        notional_usd=5_000.0,
        existing_position_id="POS-1",
    )
    dae = _dae(signed_notional_usd=-5_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-5.0)


def test_sector_concentration_contribute_partial_equity_close_uses_proposal_dae() -> None:
    """Partial EQUITY CLOSE must scale with the close size (proposal DAE),
    not the full existing position's stored DAE. Regression coverage for the
    asset-type gating on ``signed_notional_for_contribution`` — a $3k close
    of a $10k LONG position contributes -3.0%, not -10.0%."""
    config = _config(active_sectors=("tech",))
    existing = _existing(direction=Direction.LONG, notional_usd=10_000.0)
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-1": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "sector_concentration_tech")
    proposal = _proposal(
        sector="tech",
        action=Action.CLOSE,
        notional_usd=3_000.0,
        existing_position_id="POS-1",
    )
    dae = _dae(signed_notional_usd=-3_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-3.0)


def test_sector_concentration_contribute_close_on_strategy_uses_existing_dae() -> None:
    """CLOSE on a STRATEGY (or OPTION) position with empty proposal DAE
    falls back to ``existing.delta_adjusted_exposure_usd`` — without this, the
    rule reports zero impact."""
    config = _config(active_sectors=("tech",))
    existing = _existing(
        asset_type=AssetType.STRATEGY,
        notional_usd=4_000.0,
        delta_adjusted_exposure_usd=4_500.0,
    )
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-1": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "sector_concentration_tech")
    proposal = _proposal(
        sector="tech",
        asset_type=AssetType.STRATEGY,
        action=Action.CLOSE,
        notional_usd=4_000.0,
        existing_position_id="POS-1",
    )
    # Empty DAE simulates the option_legs=None path.
    dae = _dae(signed_notional_usd=0.0)
    # Contribution = -existing.dae / pv * 100 = -4.5%
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-4.5)


# ---------------------------------------------------------------------------
# net_long_pct
# ---------------------------------------------------------------------------


def test_net_long_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(net_long_pct=42.5)
    spec = _spec_by_id(build_active_specs(config), "net_long_pct")
    assert spec.read_current(state, config) == 42.5


def test_net_long_contribute_signed() -> None:
    """Long OPEN contributes positive; short OPEN contributes negative."""
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "net_long_pct")

    long_open = _proposal(direction=Direction.LONG, action=Action.OPEN)
    long_dae = _dae(signed_notional_usd=5_000.0)
    assert spec.contribute(long_open, long_dae, state, config) == pytest.approx(5.0)

    short_open = _proposal(direction=Direction.SHORT, action=Action.OPEN)
    short_dae = _dae(signed_notional_usd=-5_000.0)
    assert spec.contribute(short_open, short_dae, state, config) == pytest.approx(-5.0)


def test_net_long_contribute_close_on_strategy_uses_existing_dae() -> None:
    """CLOSE on a STRATEGY position with empty proposal DAE: net_long contribution
    must come from ``existing.delta_adjusted_exposure_usd``."""
    config = _config()
    existing = _existing(
        position_id="POS-STRAT",
        direction=Direction.LONG,
        asset_type=AssetType.STRATEGY,
        notional_usd=4_000.0,
        delta_adjusted_exposure_usd=4_500.0,
    )
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-STRAT": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "net_long_pct")
    proposal = _proposal(
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.STRATEGY,
        action=Action.CLOSE,
        notional_usd=4_000.0,
        existing_position_id="POS-STRAT",
    )
    dae = _dae(signed_notional_usd=0.0)
    # Closing a +4_500 DAE position: net_long contribution = -4_500/100_000*100 = -4.5
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-4.5)


def test_net_long_contribute_close_on_short_strategy_increases_net_long() -> None:
    """CLOSE on a SHORT-direction STRATEGY position: closing bearish exposure
    raises net_long. ``existing.delta_adjusted_exposure_usd`` is negative for
    a bearish position, so the helper returns ``-existing.dae`` = +X."""
    config = _config()
    existing = _existing(
        position_id="POS-STRAT-BEAR",
        direction=Direction.SHORT,
        asset_type=AssetType.STRATEGY,
        notional_usd=3_000.0,
        delta_adjusted_exposure_usd=-3_500.0,
    )
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-STRAT-BEAR": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "net_long_pct")
    proposal = _proposal(
        sector="tech",
        direction=Direction.SHORT,
        asset_type=AssetType.STRATEGY,
        action=Action.CLOSE,
        notional_usd=3_000.0,
        existing_position_id="POS-STRAT-BEAR",
    )
    dae = _dae(signed_notional_usd=0.0)
    # Helper returns -existing.dae = -(-3_500) = +3_500 → contribution = +3.5
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(3.5)


def test_net_long_contribute_partial_equity_close_uses_proposal_dae() -> None:
    """Partial EQUITY CLOSE scales with the close size — net_long contribution
    is proposal DAE / pv, not existing position DAE / pv."""
    config = _config()
    existing = _existing(direction=Direction.LONG, notional_usd=10_000.0)
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-1": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "net_long_pct")
    proposal = _proposal(
        sector="tech",
        direction=Direction.LONG,
        action=Action.CLOSE,
        notional_usd=3_000.0,
        existing_position_id="POS-1",
    )
    dae = _dae(signed_notional_usd=-3_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-3.0)


# ---------------------------------------------------------------------------
# net_short_pct
# ---------------------------------------------------------------------------


def test_net_short_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(net_short_pct=15.0)
    spec = _spec_by_id(build_active_specs(config), "net_short_pct")
    assert spec.read_current(state, config) == 15.0


def test_net_short_contribute_flipped_sign() -> None:
    """Short OPEN (signed_notional<0) contributes positively to net short."""
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "net_short_pct")

    short_open = _proposal(direction=Direction.SHORT, action=Action.OPEN)
    short_dae = _dae(signed_notional_usd=-5_000.0)
    # -(-5_000) / 100_000 * 100 = +5
    assert spec.contribute(short_open, short_dae, state, config) == pytest.approx(5.0)


def test_net_short_contribute_close_on_short_strategy_decreases_net_short() -> None:
    """CLOSE on a SHORT STRATEGY position with empty proposal DAE: net_short
    contribution comes from ``existing.delta_adjusted_exposure_usd``.
    Helper returns -existing.dae = +3_500; net_short flips that to -3.5."""
    config = _config()
    existing = _existing(
        position_id="POS-STRAT-BEAR",
        direction=Direction.SHORT,
        asset_type=AssetType.STRATEGY,
        notional_usd=3_000.0,
        delta_adjusted_exposure_usd=-3_500.0,
    )
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-STRAT-BEAR": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "net_short_pct")
    proposal = _proposal(
        sector="tech",
        direction=Direction.SHORT,
        asset_type=AssetType.STRATEGY,
        action=Action.CLOSE,
        notional_usd=3_000.0,
        existing_position_id="POS-STRAT-BEAR",
    )
    dae = _dae(signed_notional_usd=0.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-3.5)


# ---------------------------------------------------------------------------
# gross_exposure_pct
# ---------------------------------------------------------------------------


def test_gross_read_current_returns_state_value() -> None:
    config = _config()
    state = _snapshot(gross_pct=68.0)
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    assert spec.read_current(state, config) == 68.0


def test_gross_contribute_open_uses_absolute_signed_notional() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    proposal = _proposal(action=Action.OPEN, notional_usd=5_000.0)
    dae = _dae(signed_notional_usd=5_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(5.0)


def test_gross_contribute_close_long_decreases_gross() -> None:
    """CLOSE-LONG reduces gross by the existing position's notional."""
    config = _config()
    existing = _existing(direction=Direction.LONG, notional_usd=8_000.0)
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-1": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    proposal = _proposal(
        direction=Direction.LONG,
        action=Action.CLOSE,
        notional_usd=8_000.0,
        existing_position_id="POS-1",
    )
    dae = _dae(signed_notional_usd=-8_000.0)
    # Contribution = -existing.notional / portfolio * 100 = -8
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-8.0)


def test_gross_contribute_close_short_decreases_gross() -> None:
    """CLOSE-SHORT also reduces gross."""
    config = _config()
    existing = _existing(direction=Direction.SHORT, notional_usd=4_000.0)
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-2": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    proposal = _proposal(
        direction=Direction.SHORT,
        action=Action.CLOSE,
        notional_usd=4_000.0,
        existing_position_id="POS-2",
    )
    dae = _dae(signed_notional_usd=4_000.0)  # short close → +signed_notional
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(-4.0)


def test_gross_contribute_add_increases_gross_by_signed_magnitude() -> None:
    config = _config()
    existing = _existing(direction=Direction.LONG, notional_usd=5_000.0)
    state = _snapshot(
        portfolio_value_usd=100_000.0,
        existing_positions={"POS-1": existing},
    )
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    proposal = _proposal(action=Action.ADD, existing_position_id="POS-1")
    dae = _dae(signed_notional_usd=2_000.0)
    assert spec.contribute(proposal, dae, state, config) == pytest.approx(2.0)


def test_gross_contribute_zero_for_adjust_and_cancel() -> None:
    config = _config()
    state = _snapshot(portfolio_value_usd=100_000.0)
    spec = _spec_by_id(build_active_specs(config), "gross_exposure_pct")
    for action in (Action.ADJUST, Action.CANCEL):
        proposal = _proposal(action=action)
        dae = _dae(signed_notional_usd=0.0)
        assert spec.contribute(proposal, dae, state, config) == 0.0


# ---------------------------------------------------------------------------
# ALP-636: holistic-rule pairing invariant on RuleSpec
# ---------------------------------------------------------------------------


def test_rulespec_holistic_pairing_requires_contributors_from_batch() -> None:
    """A RuleSpec with ``project_after_batch`` set but no ``contributors_from_batch``
    must raise at construction — otherwise ``_build_breach_entry`` falls back to
    the no-op ``contribute`` walk and silently emits empty contributors (the
    exact bug ALP-636 fixed; the invariant prevents future regressions).
    """

    def _noop_read(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
        return 0.0

    def _noop_contribute(
        proposal: ProposedDelta,
        dae: DeltaAdjustedExposure,
        state: PortfolioStateSnapshot,
        config: LibraryConfig,
    ) -> float:
        return 0.0

    def _project(
        proposals: object,
        state: PortfolioStateSnapshot,
        config: LibraryConfig,
    ) -> float:
        return 0.0

    with pytest.raises(ValueError, match="contributors_from_batch"):
        RuleSpec(
            rule_id="bogus_holistic",
            unit="% of portfolio",
            read_current=_noop_read,
            contribute=_noop_contribute,
            project_after_batch=_project,
            effective_limit_key="bogus_holistic",
        )
