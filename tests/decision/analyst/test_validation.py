"""Tests for AnalystOutput Layer-2/3 validator — ALP-296.

Layer-2 (cross-field structural invariants Pydantic can't see across siblings)
and Layer-3 (referential integrity against the per-invocation retrieval store)
plus the soft conviction-band sizing-deviation WARN check. Mirrors the sibling
shapes of ``alphamind.analysis.qualitative_research.validation`` and
``alphamind.analysis.adaptive_research.validation``.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from alphamind._kernel.ids import (
    InvocationId,
    Symbol,
)
from alphamind._kernel.money import money, price
from alphamind.analysis.synthesizer.models import BriefSource
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.decision.analyst.models import (
    AnalystOutput,
    EntryOrder,
    EntryWindow,
    EventCondition,
    GuardrailValidationResult,
    InstrumentEquity,
    InstrumentOption,
    InstrumentStrategy,
    InvalidationLeg,
    InvalidationRationale,
    OrderParameters,
    PositionSize,
    PriceCondition,
    Recommendation,
    StrategyLeg,
    Target,
    WatchlistEntry,
)
from alphamind.decision.analyst.validation import (
    DEFAULT_CONVICTION_BANDS,
    ValidationError,
    ValidationResult,
    ValidationWarning,
    validate_analyst_output,
)
from alphamind.risk_guardrails.guardrail_evaluation import RuleProjection, Status

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


_DEFAULT_ACTIVE_SECTORS = frozenset({"tech", "semis", "financials", "energy"})


def _retrieval_store(*ref_ids: str) -> RetrievalStore:
    """Build a RetrievalStore with the given prefixed reference IDs."""
    entries = {ref_id: f"section text for {ref_id}" for ref_id in ref_ids}
    freshness = {BriefSource.SA_TECH: datetime(2026, 4, 23, 14, 30, tzinfo=UTC)}
    return RetrievalStore(entries=entries, freshness_by_source=freshness)


def _make_guardrail_result(**overrides: Any) -> GuardrailValidationResult:
    defaults: dict[str, Any] = {
        "overall": "PASS",
        "per_rule": (
            RuleProjection(
                rule="per_position_max_size",
                status=Status.PASS,
                current=0.0,
                limit=5.0,
                projected_after=3.0,
                headroom_remaining=2.0,
                unit="% of portfolio",
            ),
        ),
        "delta_adjusted_exposure": 3000.0,
        "greeks": None,
        "cumulative_impact_note": "Proposal #1 of 1.",
        "checked_at": _ts("2026-04-23T14:31:10Z"),
    }
    return GuardrailValidationResult(**(defaults | overrides))


def _make_recommendation(**overrides: Any) -> Recommendation:
    defaults: dict[str, Any] = {
        "recommendation_id": "REC-1",
        "instrument": InstrumentEquity(
            asset_type="equity", ticker=Symbol("NVDA"), direction="long"
        ),
        "underlying": "NVDA",
        "sector": "semis",
        "conviction_level": 4,
        "entry_order": EntryOrder(type="limit", limit_price=price(842.50)),
        "position_size": PositionSize(
            quantity=4, dollar_value=money(3370.0), pct_of_portfolio=3.37
        ),
        "target": Target(
            target_type="absolute_price", price=price(890.0), dollar_pl_target=money(190.0)
        ),
        "invalidation_legs": (
            InvalidationLeg(
                leg_id="INV-1",
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=Symbol("NVDA"), comparator="<=", trigger_price=price(820.0)
                ),
                order_parameters=OrderParameters(order_type="market"),
            ),
        ),
        "time_expectation_hours": 18.0,
        "guardrail_validation_result": _make_guardrail_result(),
        "thesis_narrative": "Hyperscaler capex signals in [SA-TECH-2] align with [SA-TECH-4].",
        "target_rationale": "$890 is the first level where prior reactions stalled.",
        "invalidation_rationale": (
            InvalidationRationale(leg_id="INV-1", rationale="Break would contradict thesis."),
        ),
        "position_size_rationale": "Conviction 4 maps to 2-4% band; sized at 3.37%.",
        "counterarguments_acknowledged": "Implied-move pricing is in line.",
    }
    return Recommendation(**(defaults | overrides))


def _make_output(**overrides: Any) -> AnalystOutput:
    defaults: dict[str, Any] = {
        "invocation_id": "inv-001",
        "timestamp": _ts("2026-04-23T14:31:22Z"),
        "mode": "normal",
        "recommendations": (_make_recommendation(),),
    }
    return AnalystOutput(**(defaults | overrides))


def _store_with_baseline_refs() -> RetrievalStore:
    """The retrieval store every baseline narrative cites by default."""
    return _retrieval_store("SA-TECH-2", "SA-TECH-4")


# ---------------------------------------------------------------------------
# Tracer — baseline recommendation passes; default bands match the design doc
# ---------------------------------------------------------------------------


class TestTracerBaseline:
    def test_default_conviction_bands_match_design_doc(self) -> None:
        """analyst.md § Conviction scale → DEFAULT_CONVICTION_BANDS."""
        assert DEFAULT_CONVICTION_BANDS == {
            1: (0.25, 0.75),
            2: (0.5, 1.5),
            3: (1.0, 3.0),
            4: (2.0, 4.0),
            5: (3.0, 5.0),
        }

    def test_baseline_recommendation_passes(self) -> None:
        result = validate_analyst_output(
            _make_output(),
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is True
        assert result.errors == ()
        assert result.warnings == ()


# ---------------------------------------------------------------------------
# Layer-2 (a) — bidirectional leg_id pairing between rationale and legs
# ---------------------------------------------------------------------------


class TestLegIdPairing:
    def test_rationale_references_unknown_leg_id_is_error(self) -> None:
        """invalidation_rationale[].leg_id with no matching leg → ERROR."""
        rec = _make_recommendation(
            invalidation_rationale=(
                InvalidationRationale(leg_id="INV-2", rationale="Refers to a leg not present."),
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "invalidation_leg_id_pairing" in rules

    def test_leg_without_rationale_is_error(self) -> None:
        """invalidation_legs[].leg_id with no matching rationale → ERROR."""
        rec = _make_recommendation(
            invalidation_legs=(
                InvalidationLeg(
                    leg_id="INV-1",
                    type="price",
                    is_hard=True,
                    condition=PriceCondition(
                        underlying_trigger=Symbol("NVDA"),
                        comparator="<=",
                        trigger_price=price(820.0),
                    ),
                    order_parameters=OrderParameters(order_type="market"),
                ),
                InvalidationLeg(
                    leg_id="INV-2",
                    type="event",
                    is_hard=False,
                    condition=EventCondition(event_description="Some event."),
                ),
            ),
            invalidation_rationale=(
                InvalidationRationale(leg_id="INV-1", rationale="Only INV-1 has rationale."),
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "invalidation_leg_id_pairing" in rules


# ---------------------------------------------------------------------------
# Layer-2 (b) — underlying matches instrument.ticker / instrument.underlying
# ---------------------------------------------------------------------------


class TestUnderlyingMatchesInstrument:
    def test_equity_underlying_mismatch_is_error(self) -> None:
        rec = _make_recommendation(
            instrument=InstrumentEquity(
                asset_type="equity", ticker=Symbol("AMD"), direction="long"
            ),
            underlying=Symbol("NVDA"),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "underlying_matches_instrument" in rules

    def test_option_underlying_mismatch_is_error(self) -> None:
        rec = _make_recommendation(
            instrument=InstrumentOption(
                asset_type="option",
                underlying=Symbol("AMD"),
                strike=price(180.0),
                expiration=date(2026, 5, 16),
                contract_type="call",
                direction="long",
            ),
            underlying=Symbol("NVDA"),
            position_size=PositionSize(
                quantity=4,
                dollar_value=money(3370.0),
                pct_of_portfolio=3.37,
                premium_at_risk=money(2000.0),
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "underlying_matches_instrument" in rules

    def test_strategy_underlying_match_passes(self) -> None:
        rec = _make_recommendation(
            instrument=InstrumentStrategy(
                asset_type="strategy",
                strategy_type="vertical_spread",
                underlying=Symbol("NVDA"),
                legs=(
                    StrategyLeg(
                        strike=price(820.0),
                        expiration=date(2026, 5, 16),
                        contract_type="call",
                        direction="long",
                        quantity_ratio=1,
                    ),
                    StrategyLeg(
                        strike=price(860.0),
                        expiration=date(2026, 5, 16),
                        contract_type="call",
                        direction="short",
                        quantity_ratio=1,
                    ),
                ),
            ),
            underlying=Symbol("NVDA"),
            position_size=PositionSize(
                quantity=4,
                dollar_value=money(3370.0),
                pct_of_portfolio=3.37,
                premium_at_risk=money(1500.0),
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is True


# ---------------------------------------------------------------------------
# Layer-2 (c) — at least one invalidation_legs[] entry has is_hard=True
# ---------------------------------------------------------------------------


class TestAtLeastOneHardLeg:
    def test_all_event_legs_is_error(self) -> None:
        """A recommendation with only event legs (all is_hard=False) → ERROR."""
        rec = _make_recommendation(
            invalidation_legs=(
                InvalidationLeg(
                    leg_id="INV-1",
                    type="event",
                    is_hard=False,
                    condition=EventCondition(event_description="Some event."),
                ),
                InvalidationLeg(
                    leg_id="INV-2",
                    type="event",
                    is_hard=False,
                    condition=EventCondition(event_description="Another event."),
                ),
            ),
            invalidation_rationale=(
                InvalidationRationale(leg_id="INV-1", rationale="Rationale 1."),
                InvalidationRationale(leg_id="INV-2", rationale="Rationale 2."),
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "at_least_one_hard_invalidation_leg" in rules


# ---------------------------------------------------------------------------
# Layer-2 (d) — recommendation_id unique within invocation
# ---------------------------------------------------------------------------


class TestRecommendationIdUnique:
    def test_duplicate_recommendation_id_is_error(self) -> None:
        rec1 = _make_recommendation(recommendation_id="REC-1")
        rec2 = _make_recommendation(recommendation_id="REC-1")
        output = _make_output(recommendations=(rec1, rec2))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "recommendation_id_unique" in rules

    def test_distinct_recommendation_ids_pass(self) -> None:
        rec1 = _make_recommendation(recommendation_id="REC-1")
        rec2 = _make_recommendation(recommendation_id="REC-2")
        output = _make_output(recommendations=(rec1, rec2))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is True


# ---------------------------------------------------------------------------
# Layer-2 (e) — sector ∈ active_sectors
# ---------------------------------------------------------------------------


class TestSectorInActiveSectors:
    def test_sector_outside_active_set_is_error(self) -> None:
        """A `tech` recommendation when active_sectors omits `tech` → ERROR."""
        rec = _make_recommendation(sector="tech")
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=frozenset({"semis", "financials", "energy"}),
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "sector_not_active" in rules

    def test_sector_inside_active_set_passes(self) -> None:
        rec = _make_recommendation(sector="semis")
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=frozenset({"semis"}),
        )
        assert result.is_valid is True


# ---------------------------------------------------------------------------
# Layer-2 (f) — instrument.asset_type permitted (feature_disabled defense)
# ---------------------------------------------------------------------------


class TestAssetTypePermitted:
    def test_option_with_feature_disabled_fail_is_error(self) -> None:
        """option + overall=FAIL + 'feature_disabled' in note → ERROR."""
        rec = _make_recommendation(
            instrument=InstrumentOption(
                asset_type="option",
                underlying=Symbol("NVDA"),
                strike=price(820.0),
                expiration=date(2026, 5, 16),
                contract_type="call",
                direction="long",
            ),
            underlying=Symbol("NVDA"),
            position_size=PositionSize(
                quantity=1,
                dollar_value=money(2000.0),
                pct_of_portfolio=2.0,
                premium_at_risk=money(2000.0),
            ),
            guardrail_validation_result=_make_guardrail_result(
                overall="FAIL",
                cumulative_impact_note="Rejected: feature_disabled — options on this profile.",
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "asset_type_feature_disabled" in rules

    def test_option_with_pass_overall_no_error(self) -> None:
        """option + overall=PASS does not trip the feature-disabled defense."""
        rec = _make_recommendation(
            instrument=InstrumentOption(
                asset_type="option",
                underlying=Symbol("NVDA"),
                strike=price(820.0),
                expiration=date(2026, 5, 16),
                contract_type="call",
                direction="long",
            ),
            underlying=Symbol("NVDA"),
            position_size=PositionSize(
                quantity=1,
                dollar_value=money(2000.0),
                pct_of_portfolio=2.0,
                premium_at_risk=money(2000.0),
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [e.rule for e in result.errors]
        assert "asset_type_feature_disabled" not in rules


# ---------------------------------------------------------------------------
# Layer-2 (g) — entry_window deadline not in the past
# ---------------------------------------------------------------------------


class TestEntryWindowNotExpired:
    def test_past_deadline_is_error(self) -> None:
        rec = _make_recommendation(
            entry_window=EntryWindow(
                deadline=_ts("2026-04-22T14:31:22Z"),  # before output.timestamp
                decay_type="binary",
                rationale="Already-expired window.",
            ),
            entry_window_rationale="Window rationale.",
        )
        output = _make_output(
            timestamp=_ts("2026-04-23T14:31:22Z"),
            recommendations=(rec,),
        )
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "entry_window_not_expired" in rules

    def test_future_deadline_passes(self) -> None:
        rec = _make_recommendation(
            entry_window=EntryWindow(
                deadline=_ts("2026-04-23T19:55:00Z"),
                decay_type="binary",
                rationale="Future window.",
            ),
            entry_window_rationale="Window rationale.",
        )
        output = _make_output(
            timestamp=_ts("2026-04-23T14:31:22Z"),
            recommendations=(rec,),
        )
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is True

    def test_no_entry_window_no_error(self) -> None:
        """Recommendations without an entry_window aren't subject to (g)."""
        rec = _make_recommendation()  # baseline has no entry_window
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [e.rule for e in result.errors]
        assert "entry_window_not_expired" not in rules


# ---------------------------------------------------------------------------
# Layer-2 (h) — entry_window distance vs time_expectation_hours (WARN)
# ---------------------------------------------------------------------------


class TestTimeHorizonConsistencyWarn:
    def test_deadline_beyond_horizon_is_warning_not_error(self) -> None:
        """deadline distance exceeds time_expectation_hours → WARNING, not ERROR."""
        rec = _make_recommendation(
            time_expectation_hours=2.0,
            entry_window=EntryWindow(
                deadline=_ts("2026-04-23T14:31:22Z") + timedelta(hours=24),
                decay_type="gradual",
                rationale="Entry window rationale.",
            ),
            entry_window_rationale="Window allows long entry tail.",
        )
        output = _make_output(
            timestamp=_ts("2026-04-23T14:31:22Z"),
            recommendations=(rec,),
        )
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        error_rules = [e.rule for e in result.errors]
        warning_rules = [w.rule for w in result.warnings]
        assert "time_horizon_consistency" not in error_rules
        assert "time_horizon_consistency" in warning_rules

    def test_deadline_within_horizon_no_warning(self) -> None:
        rec = _make_recommendation(
            time_expectation_hours=18.0,
            entry_window=EntryWindow(
                deadline=_ts("2026-04-23T14:31:22Z") + timedelta(hours=2),
                decay_type="binary",
                rationale="Entry window rationale.",
            ),
            entry_window_rationale="Window rationale.",
        )
        output = _make_output(
            timestamp=_ts("2026-04-23T14:31:22Z"),
            recommendations=(rec,),
        )
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        warning_rules = [w.rule for w in result.warnings]
        assert "time_horizon_consistency" not in warning_rules


# ---------------------------------------------------------------------------
# Layer-2 (i) — soft conviction-band sizing-deviation WARN
# ---------------------------------------------------------------------------


class TestConvictionBandWarn:
    def test_size_outside_band_is_warning(self) -> None:
        """conviction 4 (band 2.0-4.0%) but pct=5.5% emits the rule warning."""
        rec = _make_recommendation(
            conviction_level=4,
            position_size=PositionSize(
                quantity=10, dollar_value=money(5500.0), pct_of_portfolio=5.5
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        error_rules = [e.rule for e in result.errors]
        warning_rules = [w.rule for w in result.warnings]
        assert "conviction_band_deviation" not in error_rules
        assert "conviction_band_deviation" in warning_rules

    def test_size_inside_band_no_warning(self) -> None:
        rec = _make_recommendation(
            conviction_level=4,
            position_size=PositionSize(
                quantity=4, dollar_value=money(3370.0), pct_of_portfolio=3.37
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        warning_rules = [w.rule for w in result.warnings]
        assert "conviction_band_deviation" not in warning_rules

    def test_size_below_band_is_warning(self) -> None:
        """Below the band still emits the warning."""
        rec = _make_recommendation(
            conviction_level=4,
            position_size=PositionSize(quantity=1, dollar_value=money(500.0), pct_of_portfolio=0.5),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        warning_rules = [w.rule for w in result.warnings]
        assert "conviction_band_deviation" in warning_rules

    def test_custom_bands_override_default(self) -> None:
        """Passing conviction_bands overrides DEFAULT_CONVICTION_BANDS."""
        rec = _make_recommendation(
            conviction_level=4,
            position_size=PositionSize(
                quantity=4, dollar_value=money(3370.0), pct_of_portfolio=3.37
            ),
        )
        output = _make_output(recommendations=(rec,))
        # Tightened band: 4 = (1.0, 2.0); pct=3.37 sits outside.
        custom_bands = {
            1: (0.25, 0.75),
            2: (0.5, 1.5),
            3: (1.0, 3.0),
            4: (1.0, 2.0),
            5: (3.0, 5.0),
        }
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
            conviction_bands=custom_bands,
        )
        warning_rules = [w.rule for w in result.warnings]
        assert "conviction_band_deviation" in warning_rules


# ---------------------------------------------------------------------------
# Layer-3 — referential integrity in narrative fields
# ---------------------------------------------------------------------------


class TestLayer3Referential:
    def test_unknown_reference_in_thesis_narrative_is_error(self) -> None:
        rec = _make_recommendation(
            thesis_narrative="Cited [SA-TECH-2] and the missing [SA-TECH-99].",
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_retrieval_store("SA-TECH-2"),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        unknown_errors = [e for e in result.errors if e.rule == "unknown_reference"]
        assert any("SA-TECH-99" in e.message for e in unknown_errors)

    def test_unknown_reference_in_target_rationale_is_error(self) -> None:
        rec = _make_recommendation(target_rationale="Anchored to [QR-7].")
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        unknown_errors = [e for e in result.errors if e.rule == "unknown_reference"]
        assert any("QR-7" in e.message for e in unknown_errors)

    def test_unknown_reference_in_invalidation_rationale_is_error(self) -> None:
        rec = _make_recommendation(
            invalidation_rationale=(
                InvalidationRationale(
                    leg_id="INV-1", rationale="Threshold from [CR-99] — no such record."
                ),
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        unknown_errors = [e for e in result.errors if e.rule == "unknown_reference"]
        assert any("CR-99" in e.message for e in unknown_errors)

    def test_unknown_reference_in_position_size_rationale_is_error(self) -> None:
        rec = _make_recommendation(position_size_rationale="See [AR-9] for sizing logic.")
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        unknown_errors = [e for e in result.errors if e.rule == "unknown_reference"]
        assert any("AR-9" in e.message for e in unknown_errors)

    def test_unknown_reference_in_entry_window_rationale_is_error(self) -> None:
        rec = _make_recommendation(
            entry_window=EntryWindow(
                deadline=_ts("2026-04-23T19:55:00Z"),
                decay_type="binary",
                rationale="Window rationale.",
            ),
            entry_window_rationale="Per [SA-FIN-9] this window holds.",
        )
        output = _make_output(
            timestamp=_ts("2026-04-23T14:31:22Z"),
            recommendations=(rec,),
        )
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        unknown_errors = [e for e in result.errors if e.rule == "unknown_reference"]
        assert any("SA-FIN-9" in e.message for e in unknown_errors)

    def test_unknown_reference_in_counterarguments_is_error(self) -> None:
        rec = _make_recommendation(
            counterarguments_acknowledged="Bears point to [QR-CW-99] as a risk.",
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        unknown_errors = [e for e in result.errors if e.rule == "unknown_reference"]
        assert any("QR-CW-99" in e.message for e in unknown_errors)

    def test_known_references_pass(self) -> None:
        rec = _make_recommendation(thesis_narrative="Cited [SA-TECH-2] and [QR-CW-1].")
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_retrieval_store("SA-TECH-2", "QR-CW-1"),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is True

    def test_bare_prefix_citation_in_narrative_is_error(self) -> None:
        # ALP-521: a bracketed token whose body matches a ReferencePrefix value
        # but carries no -N index can never resolve in the retrieval store
        # (which is keyed by <prefix>-<index>). The 2026-05-04 E2E run emitted
        # four bare [CR] correlation-pair tokens that the consumer-side regex
        # silently dropped — surface them as bare_prefix_citation errors.
        rec = _make_recommendation(
            thesis_narrative="Correlation pair noted [CR] without index.",
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        bare_errors = [e for e in result.errors if e.rule == "bare_prefix_citation"]
        assert len(bare_errors) == 1
        assert "CR" in bare_errors[0].message
        assert "thesis_narrative" in bare_errors[0].field_path

    def test_bare_prefix_alongside_resolved_reference_only_flags_bare(self) -> None:
        # A well-formed ``[SA-TECH-2]`` (in store) coexists with a bare ``[CR]``.
        # Only the bare prefix should produce a bare_prefix_citation error.
        rec = _make_recommendation(
            thesis_narrative="See [SA-TECH-2] and pair note [CR].",
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_retrieval_store("SA-TECH-2"),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        bare_errors = [e for e in result.errors if e.rule == "bare_prefix_citation"]
        assert len(bare_errors) == 1
        assert "CR" in bare_errors[0].message
        # The well-formed reference must not surface as unknown.
        unknown_errors = [e for e in result.errors if e.rule == "unknown_reference"]
        assert unknown_errors == []

    def test_non_taxonomy_bracketed_token_is_not_bare_prefix(self) -> None:
        # ``[REC]`` is the analyst's own producer-side prefix and lives outside
        # the synthesizer's ReferencePrefix taxonomy; the bare-prefix detector
        # must not flag it. (parent issue § C: producer-side prefixes are not
        # in scope for bare-prefix detection.)
        rec = _make_recommendation(
            thesis_narrative="Reference [REC] from this output.",
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        bare_errors = [e for e in result.errors if e.rule == "bare_prefix_citation"]
        assert bare_errors == []

    def test_non_canonical_prefix_skipped(self) -> None:
        """[INV-1] is the leg-ID prefix, not a synthesizer ReferencePrefix.

        ``parse_reference_id`` returns ``None`` for ``INV-1`` because that
        prefix is not in the canonical taxonomy; the validator skips it
        rather than treating it as an unknown reference.
        """
        rec = _make_recommendation(
            invalidation_rationale=(
                InvalidationRationale(
                    leg_id="INV-1", rationale="Per leg [INV-1] — should not error."
                ),
            ),
        )
        output = _make_output(recommendations=(rec,))
        result = validate_analyst_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is True


# ---------------------------------------------------------------------------
# Watchlist-mode skip path
# ---------------------------------------------------------------------------


class TestWatchlistMode:
    def test_baseline_watchlist_passes(self) -> None:
        watch = WatchlistEntry(
            ticker=Symbol("NVDA"),
            sector="semis",
            thesis_summary="Watching for confirmation.",
            estimated_conviction=3,
            source_references=("SA-TECH-2",),
        )
        output = AnalystOutput(
            invocation_id=InvocationId("inv-001"),
            timestamp=_ts("2026-04-23T14:31:22Z"),
            mode="watchlist",
            watchlist=(watch,),
        )
        result = validate_analyst_output(
            output,
            retrieval_store=_retrieval_store("SA-TECH-2"),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is True

    def test_watchlist_sector_outside_active_set_is_error(self) -> None:
        watch = WatchlistEntry(
            ticker=Symbol("JPM"),
            sector="financials",
            thesis_summary="Watching JPM.",
            estimated_conviction=2,
        )
        output = AnalystOutput(
            invocation_id=InvocationId("inv-001"),
            timestamp=_ts("2026-04-23T14:31:22Z"),
            mode="watchlist",
            watchlist=(watch,),
        )
        result = validate_analyst_output(
            output,
            retrieval_store=_retrieval_store(),
            active_sectors=frozenset({"tech", "semis", "energy"}),
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "sector_not_active" in rules

    def test_watchlist_unknown_source_reference_is_error(self) -> None:
        watch = WatchlistEntry(
            ticker=Symbol("NVDA"),
            sector="semis",
            thesis_summary="Watching NVDA.",
            estimated_conviction=3,
            source_references=("SA-TECH-99",),
        )
        output = AnalystOutput(
            invocation_id=InvocationId("inv-001"),
            timestamp=_ts("2026-04-23T14:31:22Z"),
            mode="watchlist",
            watchlist=(watch,),
        )
        result = validate_analyst_output(
            output,
            retrieval_store=_retrieval_store(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is False
        rules = [e.rule for e in result.errors]
        assert "unknown_reference" in rules


# ---------------------------------------------------------------------------
# Aggregation — validator collects all errors, does not short-circuit
# ---------------------------------------------------------------------------


class TestAggregation:
    def test_multiple_errors_all_collected(self) -> None:
        """Validator accumulates errors across invariants and references."""
        rec1 = _make_recommendation(
            recommendation_id="REC-1",
            sector="tech",  # outside active_sectors below
            thesis_narrative="Cited [SA-FIN-99] which is missing.",
        )
        rec2 = _make_recommendation(
            recommendation_id="REC-1",  # duplicate id
            sector="semis",
        )
        output = _make_output(recommendations=(rec1, rec2))
        result = validate_analyst_output(
            output,
            retrieval_store=_retrieval_store(),
            active_sectors=frozenset({"semis", "energy"}),
        )
        assert result.is_valid is False
        rules = {e.rule for e in result.errors}
        assert "sector_not_active" in rules
        assert "unknown_reference" in rules
        assert "recommendation_id_unique" in rules


# ---------------------------------------------------------------------------
# Verification: prompts/decision/analyst.md <example_output> validates clean
# ---------------------------------------------------------------------------


_EXAMPLE_OUTPUT_PAYLOAD: dict[str, Any] = {
    "invocation_id": "inv-2026-04-23T14-30Z",
    "timestamp": "2026-04-23T14:31:22Z",
    "mode": "normal",
    "recommendations": [
        {
            "recommendation_id": "REC-1",
            "instrument": {"asset_type": "equity", "ticker": "NVDA", "direction": "long"},
            "underlying": "NVDA",
            "sector": "semis",
            "conviction_level": 4,
            "entry_order": {"type": "limit", "limit_price": 842.50},
            "position_size": {
                "quantity": 4,
                "dollar_value": 3370.00,
                "pct_of_portfolio": 3.37,
                "delta_adjusted_exposure": 3370.00,
            },
            "target": {
                "target_type": "absolute_price",
                "price": 890.00,
                "dollar_pl_target": 190.00,
            },
            "invalidation_legs": [
                {
                    "leg_id": "INV-1",
                    "type": "price",
                    "is_hard": True,
                    "condition": {
                        "underlying_trigger": "NVDA",
                        "comparator": "<=",
                        "trigger_price": 820.00,
                    },
                    "order_parameters": {"order_type": "market"},
                },
                {
                    "leg_id": "INV-2",
                    "type": "event",
                    "is_hard": False,
                    "condition": {"event_description": "MSFT cuts capex guidance."},
                },
            ],
            "entry_window": {
                "deadline": "2026-04-23T19:55:00Z",
                "decay_type": "binary",
                "rationale": "Pre-print positioning.",
            },
            "time_expectation_hours": 18,
            "guardrail_validation_result": {
                "overall": "PASS",
                "per_rule": [
                    {
                        "rule": "per_position_max_size",
                        "status": "PASS",
                        "current": 0.0,
                        "limit": 5.0,
                        "projected_after": 3.37,
                        "headroom_remaining": 1.63,
                        "unit": "% of portfolio",
                    }
                ],
                "delta_adjusted_exposure": 3370.00,
                "checked_at": "2026-04-23T14:31:10Z",
            },
            "thesis_narrative": (
                "Hyperscaler capex signals in [SA-TECH-2] align with [SA-TECH-4]."
            ),
            "target_rationale": "Anchored to prior reactions.",
            "invalidation_rationale": [
                {"leg_id": "INV-1", "rationale": "Break implies thesis broke."},
                {"leg_id": "INV-2", "rationale": "Capex cut weakens demand path."},
            ],
            "position_size_rationale": "Conviction 4 maps to 2-4% band; sized at 3.37%.",
            "entry_window_rationale": "Binary decay before earnings.",
            "counterarguments_acknowledged": "Implied move is in line.",
        }
    ],
}


class TestExampleOutputSmoke:
    def test_example_output_passes_validator(self) -> None:
        """The prompt's example_output validates cleanly with matching refs."""
        output = AnalystOutput.model_validate(_EXAMPLE_OUTPUT_PAYLOAD)
        result = validate_analyst_output(
            output,
            retrieval_store=_retrieval_store("SA-TECH-2", "SA-TECH-4"),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.is_valid is True
        assert result.warnings == ()


# ---------------------------------------------------------------------------
# Frozen-dataclass invariants (ALP-475: 10b conversion)
# ---------------------------------------------------------------------------


class TestValidationTypesAreFrozenDataclasses:
    """Per ALP-475, analyst validation public types are
    ``@dataclass(frozen=True, slots=True)`` — internal Pydantic types were
    converted because they never cross an LLM/persistence/vendor boundary.
    """

    def test_validation_error_is_frozen_dataclass(self) -> None:
        err = ValidationError(field_path="x.y", rule="r1", message="m1")
        assert dataclasses.is_dataclass(ValidationError)
        with pytest.raises(dataclasses.FrozenInstanceError):
            err.message = "mutated"  # type: ignore[misc]

    def test_validation_warning_is_frozen_dataclass(self) -> None:
        warn = ValidationWarning(field_path="x.y", rule="r1", message="m1")
        assert dataclasses.is_dataclass(ValidationWarning)
        with pytest.raises(dataclasses.FrozenInstanceError):
            warn.rule = "mutated"  # type: ignore[misc]

    def test_validation_result_is_frozen_dataclass(self) -> None:
        result = ValidationResult(errors=(), warnings=())
        assert dataclasses.is_dataclass(ValidationResult)
        assert result.is_valid is True
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.errors = (ValidationError(field_path="x", rule="r", message="m"),)  # type: ignore[misc]
