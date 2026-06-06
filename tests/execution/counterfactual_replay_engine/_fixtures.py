"""Shared fixtures for the engine-driver tests (ALP-564).

Builders for valid PM-decision activity-log rows + their originating-proposal
JSON bodies, and the in-memory engine / session fixtures. Kept in one place so
``test_queue`` / ``test_engine_dispatch`` / ``test_batch_driver`` agree on the
exact ``source_provenance_json`` discriminator keys and proposal shapes the
driver hydrates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from alphamind.config.models.execution import FeeSchedule, OrderType, PaperHarness
from alphamind.config.models.replay_engine import CounterfactualReplayEngineConfig
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail
from alphamind.portfolio_state.events.types import (
    EventGroup,
    EventSource,
    EventType,
    PMVerdict,
)
from alphamind.state.invocation_context.activity_log import activity_log_entry_to_row

REPLAY_CONFIG = CounterfactualReplayEngineConfig(
    iv_lag_low_confidence_threshold_minutes=30,
    strategist_default_forward_window_hours=72,
)


def make_paper_harness() -> PaperHarness:
    """A minimal ``PaperHarness`` for the drag primitives the simulators call."""
    return PaperHarness(
        spread_buffer_pct=0.1,
        impact_coefficients={
            OrderType.market: 0.5,
            OrderType.limit: 0.3,
            OrderType.stop: 0.4,
        },
        fee_schedule=FeeSchedule(
            cat_per_executed_share=0.0001,
            taf_per_share_sells=0.000166,
            sec_pct_of_notional_sells=0.0000278,
            orf_per_options_contract=0.02685,
            occ_per_options_contract=0.02,
        ),
    )


def analyst_equity_recommendation_json(*, ticker: str = "AAPL") -> dict[str, Any]:
    """Minimal valid analyst equity ``Recommendation`` body (market entry, 24h horizon)."""
    return {
        "recommendation_id": "REC-1",
        "instrument": {"asset_type": "equity", "ticker": ticker, "direction": "long"},
        "underlying": ticker,
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
                    "underlying_trigger": ticker,
                    "comparator": "<=",
                    "trigger_price": "90.00",
                },
                "order_parameters": {"order_type": "market"},
            }
        ],
        "time_expectation_hours": 24.0,
        "guardrail_validation_result": {
            "overall": "PASS",
            "per_rule": [],
            "checked_at": "2026-06-01T15:00:00+00:00",
        },
        "thesis_narrative": "Strong earnings beat expected.",
        "target_rationale": "Technical resistance at 200.",
        "invalidation_rationale": [{"leg_id": "INV-1", "rationale": "Support broken."}],
        "position_size_rationale": "Standard 5% allocation.",
        "counterarguments_acknowledged": "Fed tightening risk acknowledged.",
    }


def analyst_option_recommendation_json(*, ticker: str = "AAPL") -> dict[str, Any]:
    """Minimal valid analyst single-leg option ``Recommendation`` body."""
    body = analyst_equity_recommendation_json(ticker=ticker)
    body["recommendation_id"] = "REC-2"
    body["instrument"] = {
        "asset_type": "option",
        "underlying": ticker,
        "strike": "150.00",
        "expiration": "2026-07-17",
        "contract_type": "call",
        "direction": "long",
    }
    return body


def strategist_close_position_assessment_json(
    *, position_id: str = "POS-1", underlying: str = "AAPL"
) -> dict[str, Any]:
    """Minimal valid strategist ``PositionAssessment`` body (close action)."""
    return {
        "assessment_id": "SA-1",
        "position_id": position_id,
        "thesis_id": "THESIS-1",
        "underlying": underlying,
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


def strategist_pending_cancel_assessment_json(
    *, position_id: str = "POS-1", order_id: str = "ORD-1"
) -> dict[str, Any]:
    """Minimal valid strategist ``PendingOrderAssessment`` body (cancel action)."""
    return {
        "pending_order_assessment_id": "SA-ORD-1",
        "order_id": order_id,
        "position_id": position_id,
        "order_type": "entry_limit",
        "order_age_hours": 4.0,
        "fill_probability_assessment": "unlikely",
        "recommended_action": "cancel",
        "drift_rationale": "Price moved away from limit.",
        "action_rationale": "Unlikely to fill; cancel to free buying power.",
    }


def pm_decision_entry(
    *,
    entry_id: str,
    invocation_id: str,
    timestamp: datetime,
    envelope_id: str,
    verdict: PMVerdict,
    source_provenance_json: dict[str, Any],
    originating_proposal_json: dict[str, Any],
    position_id: str | None = None,
) -> ActivityLogEntry:
    """Build a PM_DECISION ``ActivityLogEntry`` with a hydratable proposal body."""
    detail = PMDecisionDetail(
        envelope_id=envelope_id,
        source_provenance_json=source_provenance_json,
        evaluation_json={"thesis_quality": "strong"},
        modifications_json=[],
        resulting_command_ids=("cmd-1",),
        verdict=verdict,
        originating_proposal_json=originating_proposal_json,
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=detail,
    )


def seed_invocation(session: Session, invocation_id: str) -> None:
    """Seed the ``process_lifetimes`` + ``invocations`` FK parents for *invocation_id*.

    The activity_log → invocations FK is non-deferrable, so the invocation row
    must exist (and be flushed) before any PM_DECISION row referencing it.
    """
    from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

    plr = stub_process_lifetime_row()
    # The process_lifetime parent is shared across invocations; merge so a second
    # seed_invocation call in the same session is a no-op rather than a PK clash.
    session.merge(plr)
    session.flush()
    session.add(stub_invocation_row(invocation_id))
    session.flush()


def add_pm_decision_row(session: Session, entry: ActivityLogEntry) -> None:
    """Persist a PM_DECISION entry as an ``ActivityLogRow``.

    The activity_log → invocations FK is satisfied by seeding the invocation row
    first (see :func:`seed_invocation`); callers do that once per invocation_id.
    """
    session.add(activity_log_entry_to_row(entry))
