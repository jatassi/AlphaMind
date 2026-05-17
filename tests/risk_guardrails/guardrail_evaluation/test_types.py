"""Tests for the boundary-contract dataclasses and enums (story 01).

Story 01 is types-only: every test here verifies a structural property of the
public-API surface (frozen/slots, hashability, enum members, defaults,
re-exports). Library math lands in later stories.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Callable
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any

import pytest

from alphamind._kernel.ids import PositionId, Symbol
from alphamind.risk_guardrails.guardrail_evaluation import (
    Action,
    AssetType,
    ContractType,
    DeltaAdjustedExposure,
    Direction,
    EscalationZones,
    ExistingPosition,
    FeatureDisabledRejection,
    FeatureFlagsView,
    Greeks,
    IvLookupResult,
    IvProvider,
    IvSource,
    LibraryConfig,
    LibraryOutput,
    MarketInputs,
    OptionLeg,
    PortfolioStateSnapshot,
    ProposedDelta,
    RuleProjection,
    Status,
)

# ---------------------------------------------------------------------------
# Enum members and string values
# ---------------------------------------------------------------------------


def test_status_enum_members_and_values() -> None:
    """``Status`` is the three-status projection collapse: PASS / WARNING / FAIL."""
    assert {member.name: member.value for member in Status} == {
        "PASS": "PASS",
        "WARNING": "WARNING",
        "FAIL": "FAIL",
    }


def test_direction_enum_members_and_values() -> None:
    assert {member.name: member.value for member in Direction} == {
        "LONG": "LONG",
        "SHORT": "SHORT",
    }


def test_asset_type_enum_members_and_values() -> None:
    assert {member.name: member.value for member in AssetType} == {
        "EQUITY": "EQUITY",
        "OPTION": "OPTION",
        "STRATEGY": "STRATEGY",
    }


def test_contract_type_enum_members_and_values() -> None:
    assert {member.name: member.value for member in ContractType} == {
        "CALL": "CALL",
        "PUT": "PUT",
    }


def test_action_enum_members_and_values() -> None:
    assert {member.name: member.value for member in Action} == {
        "OPEN": "OPEN",
        "ADD": "ADD",
        "CLOSE": "CLOSE",
        "ADJUST": "ADJUST",
        "CANCEL": "CANCEL",
    }


def test_iv_source_enum_members_and_values() -> None:
    assert {member.name: member.value for member in IvSource} == {
        "SURFACE": "SURFACE",
        "REALIZED_VOL_FALLBACK": "REALIZED_VOL_FALLBACK",
    }


# ---------------------------------------------------------------------------
# Dataclass catalogue + builders
# ---------------------------------------------------------------------------


# One builder per dataclass. The frozen/slots and hashability tests iterate
# this catalogue, so adding a new dataclass automatically extends the
# structural assertions provided a builder is registered here.


def _build_greeks() -> Greeks:
    return Greeks(delta=0.5, gamma=0.01, theta=-0.05, vega=0.1)


def _build_option_leg() -> OptionLeg:
    return OptionLeg(
        contract_type=ContractType.CALL,
        strike=100.0,
        expiration=date(2026, 6, 19),
        quantity=1,
    )


def _build_proposed_delta() -> ProposedDelta:
    return ProposedDelta(
        id="REC-1",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=10_000.0,
        quantity=50.0,
        option_legs=None,
        action=Action.OPEN,
        existing_position_id=None,
    )


def _build_existing_position() -> ExistingPosition:
    return ExistingPosition(
        position_id=PositionId("POS-1"),
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=10_000.0,
        delta_adjusted_exposure_usd=10_000.0,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=0.0,
    )


def _build_portfolio_snapshot() -> PortfolioStateSnapshot:
    return PortfolioStateSnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=20_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType({"tech": 18.3, "semis": 12.4}),
        net_long_pct=60.0,
        net_short_pct=10.0,
        gross_pct=70.0,
        options_delta_pct=5.0,
        portfolio_theta_pct_per_day=-0.1,
        portfolio_vega_pct_per_iv_point=0.2,
        total_short_pct=10.0,
        single_short_max_pct=4.0,
        daily_borrow_cost_pct=0.001,
        position_max_size_pct=8.5,
        existing_positions=MappingProxyType({}),
    )


class _FakeIvProvider:
    """Minimal stand-in conforming to the story-02b ``IvProvider`` Protocol."""

    def lookup_iv(
        self,
        *,
        underlying: str,
        strike: float,
        expiration: date,
        contract_type: ContractType,
        as_of: datetime,
    ) -> IvLookupResult:
        return IvLookupResult(
            implied_volatility=0.30,
            source=IvSource.SURFACE,
            notes=None,
        )


def _build_market_inputs() -> MarketInputs:
    return MarketInputs(
        underlying_prices=MappingProxyType({"AAPL": 195.0}),
        risk_free_rate=0.045,
        iv_provider=_FakeIvProvider(),
        as_of=datetime(2026, 4, 28, 14, 30, tzinfo=UTC),
    )


def _build_escalation_zones() -> EscalationZones:
    return EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _build_feature_flags_view() -> FeatureFlagsView:
    return FeatureFlagsView(options_enabled=True, short_selling_enabled=False)


def _build_library_config() -> LibraryConfig:
    return LibraryConfig(
        effective_limits=MappingProxyType({"sector_concentration_tech": 25.0}),
        escalation_zones=MappingProxyType({"sector_concentration_tech": _build_escalation_zones()}),
        feature_flags=_build_feature_flags_view(),
        active_sectors=("tech", "semis"),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _build_delta_adjusted_exposure() -> DeltaAdjustedExposure:
    return DeltaAdjustedExposure(
        proposal_id="REC-1",
        signed_notional_usd=10_000.0,
        net_greeks=None,
        iv_used=None,
        iv_source=None,
        unbuffered_delta=None,
    )


def _build_feature_disabled_rejection() -> FeatureDisabledRejection:
    return FeatureDisabledRejection(
        proposal_id="REC-2",
        reason="options_disabled_on_micro",
        disabled_feature="options",
    )


def _build_rule_projection() -> RuleProjection:
    return RuleProjection(
        rule="sector_concentration_tech",
        status=Status.PASS,
        current=18.3,
        limit=25.0,
        projected_after=22.1,
        headroom_remaining=2.9,
        unit="% of portfolio (delta-adjusted)",
    )


def _build_library_output() -> LibraryOutput:
    return LibraryOutput(
        per_rule=(_build_rule_projection(),),
        delta_adjusted=MappingProxyType({"REC-1": _build_delta_adjusted_exposure()}),
        feature_disabled=(),
    )


_BUILDERS: dict[str, Callable[[], object]] = {
    "Greeks": _build_greeks,
    "OptionLeg": _build_option_leg,
    "ProposedDelta": _build_proposed_delta,
    "ExistingPosition": _build_existing_position,
    "PortfolioStateSnapshot": _build_portfolio_snapshot,
    "MarketInputs": _build_market_inputs,
    "EscalationZones": _build_escalation_zones,
    "FeatureFlagsView": _build_feature_flags_view,
    "LibraryConfig": _build_library_config,
    "DeltaAdjustedExposure": _build_delta_adjusted_exposure,
    "FeatureDisabledRejection": _build_feature_disabled_rejection,
    "RuleProjection": _build_rule_projection,
    "LibraryOutput": _build_library_output,
}


# ---------------------------------------------------------------------------
# Structural assertions over every dataclass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_BUILDERS))
def test_dataclass_is_frozen_and_slots(name: str) -> None:
    """Every boundary dataclass uses ``frozen=True, slots=True`` so the
    library's inputs and outputs are immutable and hashable."""
    cls = type(_BUILDERS[name]())
    assert dataclasses.is_dataclass(cls), f"{name} is not a dataclass"
    # ``__dataclass_params__`` is a CPython-private attribute mypy doesn't
    # know about; widen to ``Any`` after the dataclass guard so the field
    # access type-checks.
    cls_any: Any = cls
    assert cls_any.__dataclass_params__.frozen is True, f"{name} must be frozen=True"
    # ``slots=True`` is reflected by the absence of __dict__ and presence of
    # __slots__ on the class. Checking the slot attribute directly is the
    # durable behaviour (``__dataclass_params__.slots`` is CPython-dependent).
    assert hasattr(cls, "__slots__"), f"{name} must have __slots__"
    assert "__dict__" not in cls.__dict__, f"{name} must not have __dict__"


@pytest.mark.parametrize("name", sorted(_BUILDERS))
def test_dataclass_is_hashable(name: str) -> None:
    """Every dataclass instance is hashable — required for ``LibraryOutput``
    determinism tests in story 05."""
    instance = _BUILDERS[name]()
    # Should not raise.
    hash(instance)


# ---------------------------------------------------------------------------
# Cross-field invariants and field defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("warning", "critical", "hard_block"),
    [
        (85.0, 85.0, 95.0),  # warning == critical
        (86.0, 85.0, 95.0),  # warning > critical
        (70.0, 95.0, 95.0),  # critical == hard_block
        (70.0, 96.0, 95.0),  # critical > hard_block
    ],
)
def test_escalation_zones_rejects_unordered_thresholds(
    warning: float, critical: float, hard_block: float
) -> None:
    """``warning < critical < hard_block`` is invariant on construction."""
    with pytest.raises(ValueError):
        EscalationZones(warning=warning, critical=critical, hard_block=hard_block)


def test_proposed_delta_optional_field_defaults() -> None:
    """``daily_borrow_cost_usd`` defaults to None; ``reserves_capital`` to False."""
    delta = _build_proposed_delta()
    assert delta.daily_borrow_cost_usd is None
    assert delta.reserves_capital is False


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_public_api_surface_matches_documented_re_exports() -> None:
    """The package's top-level dir() (excluding private dunders) must equal
    exactly the documented re-export list. Adding a name without re-exporting
    it, or re-exporting a name not in the spec, fails this test."""
    import alphamind.risk_guardrails.guardrail_evaluation as module

    expected = {
        # Status / classification enums
        "Status",
        "Direction",
        "AssetType",
        "ContractType",
        "Action",
        "IvSource",
        # Math primitives' shapes
        "Greeks",
        "OptionLeg",
        # Boundary inputs
        "ProposedDelta",
        "ExistingPosition",
        "PortfolioStateSnapshot",
        "MarketInputs",
        "EscalationZones",
        "FeatureFlagsView",
        "LibraryConfig",
        # Boundary outputs
        "DeltaAdjustedExposure",
        "FeatureDisabledRejection",
        "RuleProjection",
        "LibraryOutput",
        # IV-sourcing surface from story 02b
        "FixtureIvProvider",
        "IvLookupError",
        "IvLookupResult",
        "IvProvider",
        "IvQuote",
        "IvSurfaceEntry",
        "RealizedVolEntry",
        # Black-Scholes core (story 02a; bs_price added by ALP-422)
        "bs_greeks",
        "bs_price",
        # Effective-limit adapter and feature-flag gate (story 02c)
        "EffectiveLimitAdapterError",
        "classify_feature_gate",
        "from_resolved_config",
        # Delta-adjusted exposure (story 03)
        "compute_delta_adjusted_exposure",
        # Projection engine and rule-contribution registry (story 04)
        "ProjectionError",
        "RuleSpec",
        "build_active_specs",
        "project_all",
        "project_rule",
        # Library entry point (story 05)
        "LibraryInputError",
        "evaluate_proposals",
        # Risk-budget projection (ALP-503)
        "build_risk_budget_consumption",
    }

    public = {name for name in dir(module) if not name.startswith("_")}
    assert public == expected, (
        f"unexpected re-exports: {public - expected}; missing: {expected - public}"
    )


def test_iv_provider_protocol_typing() -> None:
    """``MarketInputs.iv_provider`` is typed against the implemented
    ``IvProvider`` Protocol from story 02b. A class with the protocol-required
    ``lookup_iv`` signature is structurally compatible (mypy enforces this; at
    runtime we just confirm the protocol is importable and the field accepts
    the value)."""
    assert typing.get_type_hints(MarketInputs)["iv_provider"] is IvProvider

    market = _build_market_inputs()
    assert isinstance(market, MarketInputs)
