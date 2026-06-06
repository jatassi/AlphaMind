"""Tests for proposal_hydration.py (ALP-558).

Covers all dispatch branches of :func:`hydrate_originating_proposal` and the
validation-failure path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from alphamind.execution.counterfactual_replay_engine.proposal_hydration import (
    ProposalHydrationError,
    hydrate_originating_proposal,
)


# ---------------------------------------------------------------------------
# Minimal PMDecisionDetail stub — we only need two fields consumed here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FakeDetail:
    source_provenance_json: dict[str, Any]
    originating_proposal_json: dict[str, Any]


# ---------------------------------------------------------------------------
# Fixtures — minimal valid JSON bodies for each proposal shape
# ---------------------------------------------------------------------------


def _analyst_recommendation_json() -> dict[str, Any]:
    """Minimal valid Recommendation body (model_dump(mode='json'))."""
    return {
        "recommendation_id": "REC-1",
        "instrument": {
            "asset_type": "equity",
            "ticker": "AAPL",
            "direction": "long",
        },
        "underlying": "AAPL",
        "sector": "tech",
        "conviction_level": 3,
        "entry_order": {"type": "market"},
        "position_size": {
            "quantity": 10.0,
            "dollar_value": "1000.00",
            "pct_of_portfolio": 0.05,
        },
        "target": {
            "target_type": "absolute_price",
            "price": "200.00",
            "dollar_pl_target": "500.00",
        },
        "invalidation_legs": [
            {
                "leg_id": "INV-1",
                "type": "price",
                "is_hard": True,
                "condition": {
                    "underlying_trigger": "AAPL",
                    "comparator": "<=",
                    "trigger_price": "170.00",
                },
                "order_parameters": {"order_type": "market"},
            }
        ],
        "time_expectation_hours": 24.0,
        "guardrail_validation_result": {
            "overall": "PASS",
            "per_rule": [],
            "checked_at": "2026-01-02T15:00:00+00:00",
        },
        "thesis_narrative": "Strong earnings beat expected.",
        "target_rationale": "Technical resistance at 200.",
        "invalidation_rationale": [{"leg_id": "INV-1", "rationale": "Support broken."}],
        "position_size_rationale": "Standard 5% allocation.",
        "counterarguments_acknowledged": "Fed tightening risk acknowledged.",
    }


def _strategist_position_assessment_json() -> dict[str, Any]:
    """Minimal valid PositionAssessment body (close action)."""
    return {
        "assessment_id": "SA-1",
        "position_id": "POS-1",
        "thesis_id": "THESIS-1",
        "underlying": "AAPL",
        "sector": "tech",
        "thesis_status": "invalidated",
        "recommended_action": "close",
        "action_parameters": {
            "action": "close",
            "quantity": "all",
            "order_type": "market",
            "close_rationale_type": "thesis_invalidated",
        },
        "exposure_impact": {
            "sector_delta_adjusted_change": "-1000.00",
            "net_directional_impact": "-1000.00",
        },
        "status_rationale": "Thesis invalidated by earnings miss.",
        "action_rationale": "Close to preserve capital.",
    }


def _strategist_pending_order_assessment_json() -> dict[str, Any]:
    """Minimal valid PendingOrderAssessment body (cancel action)."""
    return {
        "pending_order_assessment_id": "SA-ORD-1",
        "order_id": "ORD-1",
        "position_id": "POS-1",
        "order_type": "entry_limit",
        "order_age_hours": 4.0,
        "fill_probability_assessment": "unlikely",
        "recommended_action": "cancel",
        "drift_rationale": "Price moved away from limit.",
        "action_rationale": "Unlikely to fill; cancel to free buying power.",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHydrateAnalystProposal:
    def test_pm_analyst_returns_recommendation(self) -> None:
        from alphamind.decision.analyst.models import Recommendation

        detail = _FakeDetail(
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json=_analyst_recommendation_json(),
        )
        result = hydrate_originating_proposal(detail)  # type: ignore[arg-type]
        assert isinstance(result, Recommendation)
        assert result.recommendation_id == "REC-1"

    def test_pm_analyst_equity_instrument(self) -> None:
        from alphamind.decision.analyst.models import InstrumentEquity

        detail = _FakeDetail(
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json=_analyst_recommendation_json(),
        )
        result = hydrate_originating_proposal(detail)  # type: ignore[arg-type]
        assert isinstance(result.instrument, InstrumentEquity)  # type: ignore[union-attr]
        assert result.instrument.ticker == "AAPL"  # type: ignore[union-attr]


class TestHydrateStrategistPositionAssessment:
    def test_pm_strategist_position_assessment_returns_position_assessment(self) -> None:
        from alphamind.decision.strategist.models import PositionAssessment

        detail = _FakeDetail(
            source_provenance_json={
                "source_provenance": "pm_strategist",
                "recommendation_type": "position_assessment",
            },
            originating_proposal_json=_strategist_position_assessment_json(),
        )
        result = hydrate_originating_proposal(detail)  # type: ignore[arg-type]
        assert isinstance(result, PositionAssessment)
        assert str(result.assessment_id) == "SA-1"

    def test_pm_strategist_position_assessment_action(self) -> None:
        detail = _FakeDetail(
            source_provenance_json={
                "source_provenance": "pm_strategist",
                "recommendation_type": "position_assessment",
            },
            originating_proposal_json=_strategist_position_assessment_json(),
        )
        result = hydrate_originating_proposal(detail)  # type: ignore[arg-type]
        assert result.recommended_action == "close"  # type: ignore[union-attr]


class TestHydrateStrategistPendingOrderAssessment:
    def test_pm_strategist_pending_order_returns_pending_order_assessment(self) -> None:
        from alphamind.decision.strategist.models import PendingOrderAssessment

        detail = _FakeDetail(
            source_provenance_json={
                "source_provenance": "pm_strategist",
                "recommendation_type": "pending_order_assessment",
            },
            originating_proposal_json=_strategist_pending_order_assessment_json(),
        )
        result = hydrate_originating_proposal(detail)  # type: ignore[arg-type]
        assert isinstance(result, PendingOrderAssessment)
        assert result.recommended_action == "cancel"

    def test_pm_strategist_pending_order_id(self) -> None:
        detail = _FakeDetail(
            source_provenance_json={
                "source_provenance": "pm_strategist",
                "recommendation_type": "pending_order_assessment",
            },
            originating_proposal_json=_strategist_pending_order_assessment_json(),
        )
        result = hydrate_originating_proposal(detail)  # type: ignore[arg-type]
        assert str(result.pending_order_assessment_id) == "SA-ORD-1"  # type: ignore[union-attr]


class TestHydrationErrors:
    def test_invalid_json_shape_raises_hydration_error(self) -> None:
        """A malformed proposal body raises ProposalHydrationError, not ValidationError."""
        detail = _FakeDetail(
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json={"bad": "shape"},
        )
        with pytest.raises(ProposalHydrationError):
            hydrate_originating_proposal(detail)  # type: ignore[arg-type]

    def test_unknown_source_provenance_raises_hydration_error(self) -> None:
        detail = _FakeDetail(
            source_provenance_json={"source_provenance": "pm_unknown"},
            originating_proposal_json={},
        )
        with pytest.raises(ProposalHydrationError):
            hydrate_originating_proposal(detail)  # type: ignore[arg-type]

    def test_missing_recommendation_type_for_strategist_raises(self) -> None:
        detail = _FakeDetail(
            source_provenance_json={
                "source_provenance": "pm_strategist",
                # missing recommendation_type key
            },
            originating_proposal_json=_strategist_position_assessment_json(),
        )
        with pytest.raises(ProposalHydrationError):
            hydrate_originating_proposal(detail)  # type: ignore[arg-type]

    def test_unknown_recommendation_type_raises(self) -> None:
        detail = _FakeDetail(
            source_provenance_json={
                "source_provenance": "pm_strategist",
                "recommendation_type": "unknown_type",
            },
            originating_proposal_json=_strategist_position_assessment_json(),
        )
        with pytest.raises(ProposalHydrationError):
            hydrate_originating_proposal(detail)  # type: ignore[arg-type]
