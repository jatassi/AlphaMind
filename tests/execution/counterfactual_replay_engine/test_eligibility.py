"""Tests for eligibility.py (ALP-558).

Covers every rule branch in :func:`check_eligibility` and both
:func:`compute_replay_window` variants (analyst + strategist).

Repositories are in-memory fakes — not mocked internal collaborators. They
satisfy the Protocol structural interface and stand in for the SQL-backed
implementations wired at story 08.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from alphamind.config.models.replay_engine import CounterfactualReplayEngineConfig
from alphamind.execution.counterfactual_replay_engine.eligibility import (
    check_eligibility,
    compute_replay_window,
)
from alphamind.execution.counterfactual_replay_engine.enums import (
    ReplayStatus,
    UnevaluableReason,
)
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar

# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------

_CONFIG = CounterfactualReplayEngineConfig(
    iv_lag_low_confidence_threshold_minutes=30,
    strategist_default_forward_window_hours=24,
)

# ---------------------------------------------------------------------------
# Fake repository implementations
# ---------------------------------------------------------------------------


class _AlwaysHasBars:
    def has_bars_over_window(self, ticker: str, start: datetime, end: datetime) -> bool:
        return True

    def load_bars(self, *, ticker: str, start: datetime, end: datetime) -> tuple[OhlcvBar, ...]:
        # Eligibility never loads bars; satisfy the widened Protocol (ALP-559).
        return ()


class _NeverHasBars:
    def has_bars_over_window(self, ticker: str, start: datetime, end: datetime) -> bool:
        return False

    def load_bars(self, *, ticker: str, start: datetime, end: datetime) -> tuple[OhlcvBar, ...]:
        return ()


class _AlwaysHasSnapshot:
    def has_snapshot_at_or_before(self, contract_ticker: str, when: datetime) -> bool:
        return True


class _NeverHasSnapshot:
    def has_snapshot_at_or_before(self, contract_ticker: str, when: datetime) -> bool:
        return False


class _NoCorporateActions:
    def has_action_in_window(self, ticker: str, start: datetime, end: datetime) -> bool:
        return False


class _HasCorporateAction:
    def has_action_in_window(self, ticker: str, start: datetime, end: datetime) -> bool:
        return True


# ---------------------------------------------------------------------------
# Proposal factories
# ---------------------------------------------------------------------------

_TS = datetime(2026, 1, 10, 14, 0, tzinfo=UTC)


def _equity_recommendation(
    *,
    has_entry_window: bool = False,
    has_time_leg: bool = False,
    time_expectation_hours: float = 8.0,
) -> Any:
    """Build a minimal Recommendation with equity instrument."""
    from alphamind.decision.analyst.models import Recommendation

    time_stop_deadline = datetime(2026, 1, 11, 14, 0, tzinfo=UTC)
    entry_window_deadline = datetime(2026, 1, 10, 16, 0, tzinfo=UTC)

    invalidation_legs: list[dict[str, Any]] = [
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
    ]
    if has_time_leg:
        invalidation_legs.append(
            {
                "leg_id": "INV-2",
                "type": "time",
                "is_hard": True,
                "condition": {"deadline": time_stop_deadline.isoformat()},
                "order_parameters": {"order_type": "market"},
            }
        )

    body: dict[str, Any] = {
        "recommendation_id": "REC-1",
        "instrument": {"asset_type": "equity", "ticker": "AAPL", "direction": "long"},
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
        "invalidation_legs": invalidation_legs,
        "time_expectation_hours": time_expectation_hours,
        "guardrail_validation_result": {
            "overall": "PASS",
            "per_rule": [],
            "checked_at": "2026-01-10T14:00:00+00:00",
        },
        "thesis_narrative": "Test thesis.",
        "target_rationale": "Test target.",
        "invalidation_rationale": [{"leg_id": "INV-1", "rationale": "Support broken."}],
        "position_size_rationale": "Standard allocation.",
        "counterarguments_acknowledged": "Acknowledged.",
    }
    if has_entry_window:
        body["entry_window"] = {
            "deadline": entry_window_deadline.isoformat(),
            "decay_type": "binary",
            "rationale": "Entry window rationale.",
        }
        body["entry_window_rationale"] = "Entry window rationale."

    return Recommendation.model_validate(body)


def _option_recommendation() -> Any:
    """Build a minimal Recommendation with option instrument."""
    from alphamind.decision.analyst.models import Recommendation

    return Recommendation.model_validate(
        {
            "recommendation_id": "REC-2",
            "instrument": {
                "asset_type": "option",
                "underlying": "AAPL",
                "strike": "180.00",
                "expiration": "2026-03-21",
                "contract_type": "call",
                "direction": "long",
            },
            "underlying": "AAPL",
            "sector": "tech",
            "conviction_level": 3,
            "entry_order": {"type": "market"},
            "position_size": {
                "quantity": 1.0,
                "dollar_value": "500.00",
                "pct_of_portfolio": 0.02,
            },
            "target": {
                "target_type": "absolute_price",
                "price": "200.00",
                "dollar_pl_target": "200.00",
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
            "time_expectation_hours": 48.0,
            "guardrail_validation_result": {
                "overall": "PASS",
                "per_rule": [],
                "checked_at": "2026-01-10T14:00:00+00:00",
            },
            "thesis_narrative": "Call option thesis.",
            "target_rationale": "Breakout target.",
            "invalidation_rationale": [{"leg_id": "INV-1", "rationale": "Support broken."}],
            "position_size_rationale": "Small options allocation.",
            "counterarguments_acknowledged": "Acknowledged.",
        }
    )


def _strategy_recommendation() -> Any:
    """Build a Recommendation with strategy instrument (multi-leg)."""
    from alphamind.decision.analyst.models import Recommendation

    return Recommendation.model_validate(
        {
            "recommendation_id": "REC-3",
            "instrument": {
                "asset_type": "strategy",
                "strategy_type": "vertical_spread",
                "underlying": "AAPL",
                "legs": [
                    {
                        "strike": "180.00",
                        "expiration": "2026-03-21",
                        "contract_type": "call",
                        "direction": "long",
                        "quantity_ratio": 1,
                    },
                    {
                        "strike": "200.00",
                        "expiration": "2026-03-21",
                        "contract_type": "call",
                        "direction": "short",
                        "quantity_ratio": 1,
                    },
                ],
            },
            "underlying": "AAPL",
            "sector": "tech",
            "conviction_level": 3,
            "entry_order": {"type": "market"},
            "position_size": {
                "quantity": 1.0,
                "dollar_value": "300.00",
                "pct_of_portfolio": 0.01,
                "premium_at_risk": "300.00",
            },
            "target": {
                "target_type": "pl_percentage",
                "price": "200.00",
                "dollar_pl_target": "150.00",
                "pl_percentage": 0.5,
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
                "checked_at": "2026-01-10T14:00:00+00:00",
            },
            "thesis_narrative": "Vertical spread thesis.",
            "target_rationale": "50% max profit.",
            "invalidation_rationale": [{"leg_id": "INV-1", "rationale": "Support broken."}],
            "position_size_rationale": "Small strategy allocation.",
            "counterarguments_acknowledged": "Acknowledged.",
        }
    )


def _position_assessment_hold() -> Any:
    """Build a PositionAssessment with recommended_action=hold."""
    from alphamind.decision.strategist.models import PositionAssessment

    return PositionAssessment.model_validate(
        {
            "assessment_id": "SA-1",
            "position_id": "POS-1",
            "thesis_id": "THESIS-1",
            "underlying": "AAPL",
            "sector": "tech",
            "thesis_status": "on-track",
            "recommended_action": "hold",
            "status_rationale": "Thesis still intact.",
            "action_rationale": "Hold for now.",
        }
    )


def _position_assessment_close() -> Any:
    """Build a PositionAssessment with recommended_action=close."""
    from alphamind.decision.strategist.models import PositionAssessment

    return PositionAssessment.model_validate(
        {
            "assessment_id": "SA-2",
            "position_id": "POS-2",
            "thesis_id": "THESIS-2",
            "underlying": "MSFT",
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
                "sector_delta_adjusted_change": "-2000.00",
                "net_directional_impact": "-2000.00",
            },
            "status_rationale": "Thesis invalidated.",
            "action_rationale": "Close position.",
        }
    )


def _pending_order_assessment_maintain() -> Any:
    """Build a PendingOrderAssessment with recommended_action=maintain."""
    from alphamind.decision.strategist.models import PendingOrderAssessment

    return PendingOrderAssessment.model_validate(
        {
            "pending_order_assessment_id": "SA-ORD-1",
            "order_id": "ORD-1",
            "position_id": "POS-1",
            "order_type": "entry_limit",
            "order_age_hours": 2.0,
            "fill_probability_assessment": "plausible",
            "recommended_action": "maintain",
            "drift_rationale": "Price within range.",
            "action_rationale": "Maintain order.",
        }
    )


def _pending_order_assessment_cancel() -> Any:
    """Build a PendingOrderAssessment with recommended_action=cancel."""
    from alphamind.decision.strategist.models import PendingOrderAssessment

    return PendingOrderAssessment.model_validate(
        {
            "pending_order_assessment_id": "SA-ORD-2",
            "order_id": "ORD-2",
            "position_id": "POS-2",
            "order_type": "entry_limit",
            "order_age_hours": 8.0,
            "fill_probability_assessment": "unlikely",
            "recommended_action": "cancel",
            "drift_rationale": "Price moved away.",
            "action_rationale": "Cancel order.",
        }
    )


# ---------------------------------------------------------------------------
# Tests: check_eligibility — Rule A
# ---------------------------------------------------------------------------


class TestRuleAUnsupportedInstrument:
    """Strategy proposals → UNSUPPORTED_INSTRUMENT, no window."""

    def test_strategy_proposal_returns_unevaluable(self) -> None:
        proposal = _strategy_recommendation()
        status, reason, _ws, _we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert status is ReplayStatus.UNEVALUABLE
        assert reason is UnevaluableReason.UNSUPPORTED_INSTRUMENT

    def test_strategy_proposal_returns_none_window(self) -> None:
        proposal = _strategy_recommendation()
        _, _, ws, we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert ws is None
        assert we is None


# ---------------------------------------------------------------------------
# Tests: check_eligibility — Rule B
# ---------------------------------------------------------------------------


class TestRuleBStrategistNoAction:
    """hold / maintain → STRATEGIST_POSITION_ACTION_NOT_SUPPORTED."""

    def test_position_hold_returns_unevaluable(self) -> None:
        proposal = _position_assessment_hold()
        status, reason, _ws, _we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert status is ReplayStatus.UNEVALUABLE
        assert reason is UnevaluableReason.STRATEGIST_POSITION_ACTION_NOT_SUPPORTED

    def test_pending_order_maintain_returns_unevaluable(self) -> None:
        proposal = _pending_order_assessment_maintain()
        status, reason, _ws, _we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert status is ReplayStatus.UNEVALUABLE
        assert reason is UnevaluableReason.STRATEGIST_POSITION_ACTION_NOT_SUPPORTED

    def test_position_hold_returns_none_window(self) -> None:
        proposal = _position_assessment_hold()
        _, _, ws, we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert ws is None
        assert we is None


# ---------------------------------------------------------------------------
# Tests: check_eligibility — Rule C
# ---------------------------------------------------------------------------


class TestRuleCDataMissing:
    """Missing bars → DATA_MISSING; missing IV snapshot for option → DATA_MISSING."""

    def test_equity_missing_bars_returns_data_missing(self) -> None:
        proposal = _equity_recommendation()
        status, reason, _ws, _we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_NeverHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert status is ReplayStatus.UNEVALUABLE
        assert reason is UnevaluableReason.DATA_MISSING

    def test_equity_missing_bars_includes_window(self) -> None:
        proposal = _equity_recommendation()
        _, _, ws, we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_NeverHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert ws is not None
        assert we is not None
        assert we > ws

    def test_option_missing_iv_snapshot_returns_data_missing(self) -> None:
        proposal = _option_recommendation()
        status, reason, _ws, _we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_NeverHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert status is ReplayStatus.UNEVALUABLE
        assert reason is UnevaluableReason.DATA_MISSING

    def test_option_missing_iv_snapshot_includes_window(self) -> None:
        proposal = _option_recommendation()
        _, _, ws, we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_NeverHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert ws is not None
        assert we is not None

    def test_option_with_iv_snapshot_and_bars_not_data_missing(self) -> None:
        proposal = _option_recommendation()
        _status, reason, _, _ = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert reason is not UnevaluableReason.DATA_MISSING


# ---------------------------------------------------------------------------
# Tests: check_eligibility — Rule D
# ---------------------------------------------------------------------------


class TestRuleDCorporateAction:
    """Corporate action in window → CORPORATE_ACTION_IN_WINDOW."""

    def test_equity_corporate_action_returns_unevaluable(self) -> None:
        proposal = _equity_recommendation()
        status, reason, _ws, _we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_HasCorporateAction(),
        )
        assert status is ReplayStatus.UNEVALUABLE
        assert reason is UnevaluableReason.CORPORATE_ACTION_IN_WINDOW

    def test_corporate_action_includes_window(self) -> None:
        proposal = _equity_recommendation()
        _, _, ws, we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_HasCorporateAction(),
        )
        assert ws is not None
        assert we is not None


# ---------------------------------------------------------------------------
# Tests: check_eligibility — Default (EVALUATED)
# ---------------------------------------------------------------------------


class TestDefaultEvaluated:
    """Happy-path: all data present, no CA → EVALUATED, no reason."""

    def test_equity_happy_path_returns_evaluated(self) -> None:
        proposal = _equity_recommendation()
        status, reason, _ws, _we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert status is ReplayStatus.EVALUATED
        assert reason is None

    def test_equity_happy_path_includes_window(self) -> None:
        proposal = _equity_recommendation()
        _, _, ws, we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert ws is not None
        assert we is not None
        assert we > ws

    def test_option_happy_path_returns_evaluated(self) -> None:
        proposal = _option_recommendation()
        status, reason, _ws, _we = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert status is ReplayStatus.EVALUATED
        assert reason is None

    def test_position_close_returns_evaluated(self) -> None:
        proposal = _position_assessment_close()
        status, reason, _, _ = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert status is ReplayStatus.EVALUATED
        assert reason is None

    def test_pending_cancel_returns_evaluated(self) -> None:
        proposal = _pending_order_assessment_cancel()
        status, reason, _, _ = check_eligibility(
            proposal,
            proposal_timestamp=_TS,
            config=_CONFIG,
            bar_repo=_AlwaysHasBars(),
            options_snapshot_repo=_AlwaysHasSnapshot(),
            corporate_action_repo=_NoCorporateActions(),
        )
        assert status is ReplayStatus.EVALUATED
        assert reason is None

    def test_no_rule_emits_unsupported_bracket_type(self) -> None:
        """UNSUPPORTED_BRACKET_TYPE is reserved; no eligibility rule produces it."""
        # Exercise every non-strategy, non-hold path and verify the enum member
        # never appears. This guards against accidental mis-assignment.
        proposals = [
            _equity_recommendation(),
            _option_recommendation(),
            _position_assessment_close(),
            _pending_order_assessment_cancel(),
        ]
        for proposal in proposals:
            _, reason, _, _ = check_eligibility(
                proposal,
                proposal_timestamp=_TS,
                config=_CONFIG,
                bar_repo=_AlwaysHasBars(),
                options_snapshot_repo=_AlwaysHasSnapshot(),
                corporate_action_repo=_NoCorporateActions(),
            )
            assert reason is not UnevaluableReason.UNSUPPORTED_BRACKET_TYPE


class TestComputeReplayWindow:
    def test_window_start_equals_proposal_timestamp(self) -> None:
        proposal = _equity_recommendation()
        start, _end = compute_replay_window(proposal, proposal_timestamp=_TS, config=_CONFIG)
        assert start == _TS

    def test_analyst_window_end_includes_time_expectation(self) -> None:
        """Without entry_window or time leg, window = ts + time_expectation_hours."""
        proposal = _equity_recommendation(time_expectation_hours=12.0)
        _, end = compute_replay_window(proposal, proposal_timestamp=_TS, config=_CONFIG)
        assert end >= _TS + timedelta(hours=12.0)

    def test_analyst_window_end_includes_entry_window(self) -> None:
        """Entry window duration adds to the total window length."""
        proposal_with_ew = _equity_recommendation(has_entry_window=True, time_expectation_hours=8.0)
        proposal_without_ew = _equity_recommendation(
            has_entry_window=False, time_expectation_hours=8.0
        )

        _, end_with = compute_replay_window(
            proposal_with_ew, proposal_timestamp=_TS, config=_CONFIG
        )
        _, end_without = compute_replay_window(
            proposal_without_ew, proposal_timestamp=_TS, config=_CONFIG
        )
        assert end_with >= end_without

    def test_analyst_window_end_includes_time_leg(self) -> None:
        """A time-type invalidation leg extends the window to its deadline."""
        proposal_with_time_leg = _equity_recommendation(
            has_time_leg=True, time_expectation_hours=8.0
        )
        proposal_without_time_leg = _equity_recommendation(
            has_time_leg=False, time_expectation_hours=8.0
        )

        _, end_with = compute_replay_window(
            proposal_with_time_leg, proposal_timestamp=_TS, config=_CONFIG
        )
        _, end_without = compute_replay_window(
            proposal_without_time_leg, proposal_timestamp=_TS, config=_CONFIG
        )
        assert end_with >= end_without

    def test_analyst_window_end_greater_than_start(self) -> None:
        proposal = _equity_recommendation(time_expectation_hours=4.0)
        start, end = compute_replay_window(proposal, proposal_timestamp=_TS, config=_CONFIG)
        assert end > start

    def test_strategist_close_window_uses_config_hours(self) -> None:
        """Strategist CLOSE uses config.strategist_default_forward_window_hours."""
        proposal = _position_assessment_close()
        start, end = compute_replay_window(proposal, proposal_timestamp=_TS, config=_CONFIG)
        assert start == _TS
        assert end == _TS + timedelta(hours=_CONFIG.strategist_default_forward_window_hours)

    def test_strategist_window_end_from_config_not_hardcoded(self) -> None:
        """Changing config changes window end — no literal is baked in."""
        config_48 = CounterfactualReplayEngineConfig(
            iv_lag_low_confidence_threshold_minutes=30,
            strategist_default_forward_window_hours=48,
        )
        proposal = _position_assessment_close()
        _, end_24 = compute_replay_window(proposal, proposal_timestamp=_TS, config=_CONFIG)
        _, end_48 = compute_replay_window(proposal, proposal_timestamp=_TS, config=config_48)
        assert end_48 == end_24 + timedelta(hours=24)

    def test_pending_order_cancel_uses_config_hours(self) -> None:
        proposal = _pending_order_assessment_cancel()
        start, end = compute_replay_window(proposal, proposal_timestamp=_TS, config=_CONFIG)
        assert start == _TS
        assert end == _TS + timedelta(hours=_CONFIG.strategist_default_forward_window_hours)

    def test_strategist_add_with_new_time_expiration_extends_window(self) -> None:
        """ADD with bracket_adjustment.new_time_expiration extends beyond default."""
        from alphamind.decision.strategist.models import PositionAssessment

        late_expiration = _TS + timedelta(hours=72)
        proposal = PositionAssessment.model_validate(
            {
                "assessment_id": "SA-3",
                "position_id": "POS-3",
                "thesis_id": "THESIS-3",
                "underlying": "MSFT",
                "sector": "tech",
                "thesis_status": "on-track",
                "recommended_action": "add",
                "action_parameters": {
                    "action": "add",
                    "additional_quantity": 5.0,
                    "additional_dollar_value": "500.00",
                    "entry_order": {"type": "market"},
                    "bracket_adjustment": {
                        "action": "adjust-bracket",
                        "new_time_expiration": late_expiration.isoformat(),
                    },
                },
                "exposure_impact": {
                    "sector_delta_adjusted_change": "500.00",
                    "net_directional_impact": "500.00",
                },
                "guardrail_validation_result": {
                    "overall": "PASS",
                    "per_rule": [],
                    "checked_at": "2026-01-10T14:00:00+00:00",
                },
                "add_conviction_justification": "High conviction add.",
                "status_rationale": "Thesis on track.",
                "action_rationale": "Add to winning position.",
            }
        )
        _, end = compute_replay_window(proposal, proposal_timestamp=_TS, config=_CONFIG)
        # Window end should reach the bracket expiration, not just config default
        default_end = _TS + timedelta(hours=_CONFIG.strategist_default_forward_window_hours)
        assert end >= late_expiration
        assert end > default_end
