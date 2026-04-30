"""Tests for the breach_behavior canonical type surface."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.config.models.guardrails import (
    BreachResponse as UpstreamBreachResponse,
)
from alphamind.config.models.guardrails import (
    EnforcementTier as UpstreamEnforcementTier,
)
from alphamind.config.models.guardrails import (
    EscalationZones as UpstreamEscalationZones,
)
from alphamind.config.models.guardrails import (
    ProgressiveTier as UpstreamProgressiveTier,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet as UpstreamActiveRiskParameterSet,
)
from alphamind.portfolio_state.records.capital import (
    DrawdownState as UpstreamDrawdownState,
)
from alphamind.portfolio_state.records.capital import (
    DrawdownTier as UpstreamDrawdownTier,
)
from alphamind.portfolio_state.records.capital import (
    RegimeLabel as UpstreamRegimeLabel,
)
from alphamind.portfolio_state.records.capital import (
    RegimeTransitionState as UpstreamRegimeTransitionState,
)
from alphamind.portfolio_state.records.capital import (
    RiskBudgetConsumption as UpstreamRiskBudgetConsumption,
)
from alphamind.portfolio_state.records.capital import (
    RiskBudgetEntry as UpstreamRiskBudgetEntry,
)
from alphamind.portfolio_state.records.capital import (
    RiskZone as UpstreamRiskZone,
)
from alphamind.portfolio_state.records.positions import (
    Direction as UpstreamDirection,
)
from alphamind.portfolio_state.records.positions import (
    InstrumentType as UpstreamInstrumentType,
)
from alphamind.portfolio_state.records.positions import (
    PositionRecord as UpstreamPositionRecord,
)
from alphamind.risk_guardrails.breach_behavior import (
    ActiveRiskParameterSet,
    BreachDetails,
    BreachResponse,
    CloseRationaleType,
    Direction,
    DrawdownState,
    DrawdownTier,
    EmergencyContext,
    EmergencyTrigger,
    EnforcementTier,
    EngineCloseCommand,
    EngineEnvelope,
    EngineGuardrailTriggerRecord,
    EscalationZones,
    HaltState,
    HardRejectionPayload,
    InstrumentType,
    PositionRecord,
    PositionSelectionAction,
    PositionSelectionResult,
    ProgressiveTier,
    RegimeLabel,
    RegimeTransitionState,
    RejectionRuleEntry,
    RiskBudgetConsumption,
    RiskBudgetEntry,
    RiskManagementSubtype,
    RiskZone,
    SecondaryBreachCheckResult,
    SecondaryBreachOutcome,
)


def test_reexports_reference_upstream_classes() -> None:
    """Re-exported enums and records must be the same class objects as upstream."""
    assert RiskZone is UpstreamRiskZone
    assert DrawdownTier is UpstreamDrawdownTier
    assert RegimeLabel is UpstreamRegimeLabel
    assert RegimeTransitionState is UpstreamRegimeTransitionState
    assert BreachResponse is UpstreamBreachResponse
    assert EnforcementTier is UpstreamEnforcementTier
    assert EscalationZones is UpstreamEscalationZones
    assert ProgressiveTier is UpstreamProgressiveTier
    assert DrawdownState is UpstreamDrawdownState
    assert RiskBudgetEntry is UpstreamRiskBudgetEntry
    assert RiskBudgetConsumption is UpstreamRiskBudgetConsumption
    assert ActiveRiskParameterSet is UpstreamActiveRiskParameterSet
    assert Direction is UpstreamDirection
    assert InstrumentType is UpstreamInstrumentType
    assert PositionRecord is UpstreamPositionRecord


def test_emergency_trigger_members() -> None:
    """EmergencyTrigger lists exactly the four documented triggers with their wire values."""
    assert EmergencyTrigger.REGIME_JUMP.value == "regime_jump"
    assert EmergencyTrigger.MULTI_RULE_BREACH.value == "multi_rule_breach"
    assert EmergencyTrigger.DAILY_DRAWDOWN_VELOCITY.value == "daily_drawdown_velocity"
    assert EmergencyTrigger.MARGIN_CALL.value == "margin_call"
    assert {member.value for member in EmergencyTrigger} == {
        "regime_jump",
        "multi_rule_breach",
        "daily_drawdown_velocity",
        "margin_call",
    }
    # StrEnum implies str subclass — wire-format friendly
    assert isinstance(EmergencyTrigger.REGIME_JUMP, str)


def test_secondary_breach_outcome_members() -> None:
    """SecondaryBreachOutcome wire values match the engine-envelope schema verbatim."""
    assert SecondaryBreachOutcome.NO_SECONDARY_BREACH.value == "no_secondary_breach"
    assert SecondaryBreachOutcome.SECONDARY_BREACH_AVOIDED.value == "secondary_breach_avoided"
    assert SecondaryBreachOutcome.DEFERRED_TO_PM.value == "deferred_to_pm"
    assert {member.value for member in SecondaryBreachOutcome} == {
        "no_secondary_breach",
        "secondary_breach_avoided",
        "deferred_to_pm",
    }


def test_close_rationale_type_members() -> None:
    """CloseRationaleType is engine-envelope-restricted to risk_management."""
    assert CloseRationaleType.RISK_MANAGEMENT.value == "risk_management"
    assert {member.value for member in CloseRationaleType} == {"risk_management"}


def test_risk_management_subtype_members() -> None:
    """RiskManagementSubtype on engine envelopes is engine_guardrail only."""
    assert RiskManagementSubtype.ENGINE_GUARDRAIL.value == "engine_guardrail"
    assert {member.value for member in RiskManagementSubtype} == {"engine_guardrail"}


# ---------------------------------------------------------------------------
# HaltState
# ---------------------------------------------------------------------------


def test_halt_state_daily_only_constructs() -> None:
    halt = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.6,
        daily_drawdown_limit_pct=2.5,
    )
    assert halt.daily_halt_active is True
    assert halt.cumulative_full_halt_active is False


def test_halt_state_cumulative_only_constructs() -> None:
    halt = HaltState(
        daily_halt_active=False,
        cumulative_full_halt_active=True,
        daily_drawdown_pct=1.0,
        daily_drawdown_limit_pct=2.5,
    )
    assert halt.cumulative_full_halt_active is True


def test_halt_state_both_active_constructs() -> None:
    halt = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=True,
        daily_drawdown_pct=2.6,
        daily_drawdown_limit_pct=2.5,
    )
    assert halt.daily_halt_active is True
    assert halt.cumulative_full_halt_active is True


def test_halt_state_neither_active_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        HaltState(
            daily_halt_active=False,
            cumulative_full_halt_active=False,
            daily_drawdown_pct=1.0,
            daily_drawdown_limit_pct=2.5,
        )
    assert "at least one halt is active" in str(exc_info.value)


@pytest.mark.parametrize("field", ["daily_drawdown_pct", "daily_drawdown_limit_pct"])
def test_halt_state_negative_drawdown_rejected(field: str) -> None:
    payload: dict[str, object] = {
        "daily_halt_active": True,
        "cumulative_full_halt_active": False,
        "daily_drawdown_pct": 2.6,
        "daily_drawdown_limit_pct": 2.5,
    }
    payload[field] = -0.1
    with pytest.raises(ValidationError) as exc_info:
        HaltState.model_validate(payload)
    assert field in str(exc_info.value)


def test_halt_state_is_frozen() -> None:
    halt = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.6,
        daily_drawdown_limit_pct=2.5,
    )
    with pytest.raises(ValidationError):
        halt.daily_halt_active = False


# ---------------------------------------------------------------------------
# EmergencyContext
# ---------------------------------------------------------------------------


def test_emergency_context_constructs() -> None:
    ctx = EmergencyContext(
        trigger=EmergencyTrigger.REGIME_JUMP,
        trigger_detail="Regime jump: low-vol -> crisis (VIX 12 -> 38)",
        minutes_since_last_invocation=8.0,
        normal_cadence_minutes=120.0,
    )
    assert ctx.trigger is EmergencyTrigger.REGIME_JUMP
    assert ctx.minutes_since_last_invocation == 8.0
    assert ctx.normal_cadence_minutes == 120.0


@pytest.mark.parametrize(
    "field",
    ["minutes_since_last_invocation", "normal_cadence_minutes"],
)
@pytest.mark.parametrize("bad_value", [0.0, -1.0, -0.001])
def test_emergency_context_rejects_non_positive_minutes(field: str, bad_value: float) -> None:
    payload: dict[str, object] = {
        "trigger": EmergencyTrigger.REGIME_JUMP,
        "trigger_detail": "Regime jump",
        "minutes_since_last_invocation": 8.0,
        "normal_cadence_minutes": 120.0,
    }
    payload[field] = bad_value
    with pytest.raises(ValidationError) as exc_info:
        EmergencyContext.model_validate(payload)
    assert field in str(exc_info.value)


def test_emergency_context_is_frozen() -> None:
    ctx = EmergencyContext(
        trigger=EmergencyTrigger.MARGIN_CALL,
        trigger_detail="Margin call",
        minutes_since_last_invocation=1.0,
        normal_cadence_minutes=120.0,
    )
    with pytest.raises(ValidationError):
        ctx.trigger_detail = "changed"


# ---------------------------------------------------------------------------
# BreachDetails / SecondaryBreachCheckResult / EngineGuardrailTriggerRecord
# ---------------------------------------------------------------------------


def test_breach_details_constructs_with_required_fields() -> None:
    details = BreachDetails(current_value=23.7, limit_value=25.0, overage=-1.3)
    assert details.current_value == 23.7
    assert details.limit_value == 25.0
    assert details.overage == -1.3
    assert details.unit is None
    assert details.regime_at_breach is None


def test_breach_details_accepts_optional_unit_and_regime() -> None:
    details = BreachDetails(
        current_value=28.2,
        limit_value=25.0,
        overage=3.2,
        unit="pct_of_portfolio",
        regime_at_breach=RegimeLabel.ELEVATED,
    )
    assert details.unit == "pct_of_portfolio"
    assert details.regime_at_breach is RegimeLabel.ELEVATED


def test_breach_details_is_frozen() -> None:
    details = BreachDetails(current_value=1.0, limit_value=2.0, overage=-1.0)
    with pytest.raises(ValidationError):
        details.current_value = 99.0


def test_secondary_breach_check_result_constructs() -> None:
    result = SecondaryBreachCheckResult(result=SecondaryBreachOutcome.NO_SECONDARY_BREACH)
    assert result.result is SecondaryBreachOutcome.NO_SECONDARY_BREACH
    assert result.notes is None


def test_secondary_breach_check_result_carries_notes() -> None:
    result = SecondaryBreachCheckResult(
        result=SecondaryBreachOutcome.SECONDARY_BREACH_AVOIDED,
        notes="Selected alternate position to cure primary without breaching net long limit.",
    )
    assert result.notes is not None and "alternate position" in result.notes


def test_secondary_breach_check_result_is_frozen() -> None:
    result = SecondaryBreachCheckResult(result=SecondaryBreachOutcome.DEFERRED_TO_PM)
    with pytest.raises(ValidationError):
        result.notes = "changed"


def test_engine_guardrail_trigger_record_constructs() -> None:
    record = EngineGuardrailTriggerRecord(
        rule_breached="per_position_max_loss",
        trigger_timestamp=datetime(2026, 4, 29, 14, 30, tzinfo=UTC),
        breach_details=BreachDetails(current_value=-30.5, limit_value=-30.0, overage=-0.5),
        position_selection_rationale=(
            "Position triggering the position-level max loss limit (largest unrealized loss "
            "in the breaching rule)."
        ),
    )
    assert record.cascade_id is None
    assert record.secondary_breach_check_result is None


def test_engine_guardrail_trigger_record_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError) as exc_info:
        EngineGuardrailTriggerRecord(
            rule_breached="per_position_max_loss",
            trigger_timestamp=datetime(2026, 4, 29, 14, 30),  # noqa: DTZ001
            breach_details=BreachDetails(current_value=-30.5, limit_value=-30.0, overage=-0.5),
            position_selection_rationale="rationale",
        )
    assert "timezone-aware" in str(exc_info.value)


def test_engine_guardrail_trigger_record_carries_optional_cascade_and_secondary() -> None:
    record = EngineGuardrailTriggerRecord(
        rule_breached="margin_call",
        trigger_timestamp=datetime(2026, 4, 29, 14, 30, tzinfo=UTC),
        breach_details=BreachDetails(current_value=0.0, limit_value=1.0, overage=-1.0),
        position_selection_rationale="Worst risk/reward ratio at current price.",
        cascade_id="cascade-2026-04-29-14-30",
        secondary_breach_check_result=SecondaryBreachCheckResult(
            result=SecondaryBreachOutcome.NO_SECONDARY_BREACH,
        ),
    )
    assert record.cascade_id == "cascade-2026-04-29-14-30"
    assert record.secondary_breach_check_result is not None
    assert record.secondary_breach_check_result.result is SecondaryBreachOutcome.NO_SECONDARY_BREACH


def test_engine_guardrail_trigger_record_is_frozen() -> None:
    record = EngineGuardrailTriggerRecord(
        rule_breached="per_position_max_loss",
        trigger_timestamp=datetime(2026, 4, 29, 14, 30, tzinfo=UTC),
        breach_details=BreachDetails(current_value=-30.5, limit_value=-30.0, overage=-0.5),
        position_selection_rationale="rationale",
    )
    with pytest.raises(ValidationError):
        record.rule_breached = "changed"


# ---------------------------------------------------------------------------
# EngineCloseCommand
# ---------------------------------------------------------------------------


def test_engine_close_command_full_close_market() -> None:
    cmd = EngineCloseCommand(
        command_id="MON.session-1.7.1",
        position_id="pos-42",
        quantity_or_all="all",
    )
    assert cmd.command_type == "close"
    assert cmd.close_rationale_type is CloseRationaleType.RISK_MANAGEMENT
    assert cmd.risk_management_subtype is RiskManagementSubtype.ENGINE_GUARDRAIL
    assert cmd.execution_method == "market"
    assert cmd.limit_price is None
    assert cmd.quantity_or_all == "all"


def test_engine_close_command_partial_trim_market() -> None:
    cmd = EngineCloseCommand(
        command_id="MON.session-1.7.1",
        position_id="pos-42",
        quantity_or_all=12.5,
    )
    assert cmd.quantity_or_all == 12.5


def test_engine_close_command_limit_requires_limit_price() -> None:
    cmd = EngineCloseCommand(
        command_id="MON.session-1.7.1",
        position_id="pos-42",
        quantity_or_all="all",
        execution_method="limit",
        limit_price=199.50,
    )
    assert cmd.limit_price == 199.50


def test_engine_close_command_limit_without_price_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        EngineCloseCommand(
            command_id="MON.session-1.7.1",
            position_id="pos-42",
            quantity_or_all="all",
            execution_method="limit",
        )
    assert "limit_price required" in str(exc_info.value)


def test_engine_close_command_market_with_price_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        EngineCloseCommand(
            command_id="MON.session-1.7.1",
            position_id="pos-42",
            quantity_or_all="all",
            execution_method="market",
            limit_price=199.50,
        )
    assert "limit_price must be None" in str(exc_info.value)


@pytest.mark.parametrize("bad_quantity", [0.0, -1.0, -0.5])
def test_engine_close_command_rejects_non_positive_numeric_quantity(bad_quantity: float) -> None:
    with pytest.raises(ValidationError) as exc_info:
        EngineCloseCommand(
            command_id="MON.session-1.7.1",
            position_id="pos-42",
            quantity_or_all=bad_quantity,
        )
    assert "must be positive" in str(exc_info.value)


def test_engine_close_command_is_frozen() -> None:
    cmd = EngineCloseCommand(
        command_id="MON.session-1.7.1",
        position_id="pos-42",
        quantity_or_all="all",
    )
    with pytest.raises(ValidationError):
        cmd.position_id = "pos-99"


# ---------------------------------------------------------------------------
# EngineEnvelope
# ---------------------------------------------------------------------------


_ENV_TS = datetime(2026, 4, 29, 14, 30, tzinfo=UTC)


def _make_trigger_record(
    *,
    timestamp: datetime = _ENV_TS,
    rule: str = "per_position_max_loss",
) -> EngineGuardrailTriggerRecord:
    return EngineGuardrailTriggerRecord(
        rule_breached=rule,
        trigger_timestamp=timestamp,
        breach_details=BreachDetails(current_value=-30.5, limit_value=-30.0, overage=-0.5),
        position_selection_rationale="Position triggering the position-level max loss limit.",
    )


def _make_close_command(*, command_id: str = "MON.session-1.7.1") -> EngineCloseCommand:
    return EngineCloseCommand(
        command_id=command_id,
        position_id="pos-42",
        quantity_or_all="all",
    )


def test_engine_envelope_constructs() -> None:
    envelope = EngineEnvelope(
        envelope_id="MON.session-1.7",
        trigger_timestamp=_ENV_TS,
        guardrail_trigger_record=_make_trigger_record(),
        command=_make_close_command(),
    )
    assert envelope.invocation_id is None
    assert envelope.source_provenance == "engine_guardrail"


@pytest.mark.parametrize(
    "bad_id",
    [
        "PM.123",  # wrong prefix
        "MON..123",  # empty session segment
        "MON.s.abc",  # non-digit trigger
        "MON.s",  # missing trigger
        "MON.s.",  # empty trigger
        "MON.s.1.2",  # too many segments
        "",  # empty
    ],
)
def test_engine_envelope_rejects_invalid_envelope_id(bad_id: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        EngineEnvelope(
            envelope_id=bad_id,
            trigger_timestamp=_ENV_TS,
            guardrail_trigger_record=_make_trigger_record(),
            command=_make_close_command(command_id=f"{bad_id}.1"),
        )
    assert "envelope_id" in str(exc_info.value)


def test_engine_envelope_rejects_naive_timestamp() -> None:
    naive = datetime(2026, 4, 29, 14, 30)  # noqa: DTZ001
    with pytest.raises(ValidationError) as exc_info:
        EngineEnvelope(
            envelope_id="MON.session-1.7",
            trigger_timestamp=naive,
            guardrail_trigger_record=_make_trigger_record(),
            command=_make_close_command(),
        )
    assert "timezone-aware" in str(exc_info.value)


def test_engine_envelope_rejects_mismatched_top_level_and_record_timestamps() -> None:
    other_ts = datetime(2026, 4, 29, 14, 31, tzinfo=UTC)
    with pytest.raises(ValidationError) as exc_info:
        EngineEnvelope(
            envelope_id="MON.session-1.7",
            trigger_timestamp=_ENV_TS,
            guardrail_trigger_record=_make_trigger_record(timestamp=other_ts),
            command=_make_close_command(),
        )
    assert "trigger_timestamp must match" in str(exc_info.value)


def test_engine_envelope_rejects_command_id_not_prefixed_by_envelope_id() -> None:
    with pytest.raises(ValidationError) as exc_info:
        EngineEnvelope(
            envelope_id="MON.session-1.7",
            trigger_timestamp=_ENV_TS,
            guardrail_trigger_record=_make_trigger_record(),
            command=_make_close_command(command_id="MON.session-1.99.1"),
        )
    assert "command_id must begin with" in str(exc_info.value)


def test_engine_envelope_is_frozen() -> None:
    envelope = EngineEnvelope(
        envelope_id="MON.session-1.7",
        trigger_timestamp=_ENV_TS,
        guardrail_trigger_record=_make_trigger_record(),
        command=_make_close_command(),
    )
    with pytest.raises(ValidationError):
        envelope.envelope_id = "MON.session-2.1"


# ---------------------------------------------------------------------------
# RejectionRuleEntry / HardRejectionPayload
# ---------------------------------------------------------------------------


def _make_rule_entry(rule_id: str = "sector_concentration_tech") -> RejectionRuleEntry:
    return RejectionRuleEntry(
        rule_id=rule_id,
        current_value=23.7,
        limit_value=25.0,
        projected_after=28.2,
        overage=3.2,
        headroom_remaining=1.3,
        unit="pct_of_portfolio",
    )


def test_rejection_rule_entry_constructs() -> None:
    entry = _make_rule_entry()
    assert entry.rule_id == "sector_concentration_tech"
    assert entry.current_value == 23.7
    assert entry.unit == "pct_of_portfolio"


def test_rejection_rule_entry_is_frozen() -> None:
    entry = _make_rule_entry()
    with pytest.raises(ValidationError):
        entry.rule_id = "changed"


def test_hard_rejection_payload_constructs() -> None:
    breaching = (_make_rule_entry(),)
    headroom_after = (
        RejectionRuleEntry(
            rule_id="sector_concentration_tech",
            current_value=23.7,
            limit_value=25.0,
            projected_after=24.9,
            overage=0.0,
            headroom_remaining=0.1,
            unit="pct_of_portfolio",
        ),
    )
    payload = HardRejectionPayload(
        rejected_command_id="inv-2026-04-29.ENV-REC-1.1.1",
        breaching_rules=breaching,
        suggested_modification="reduce size by 42%",
        headroom_after_hypothetical_compliance=headroom_after,
    )
    assert payload.rejected_command_id == "inv-2026-04-29.ENV-REC-1.1.1"
    assert len(payload.breaching_rules) == 1
    assert "reduce size" in payload.suggested_modification


def test_hard_rejection_payload_rejects_empty_breaching_rules() -> None:
    with pytest.raises(ValidationError) as exc_info:
        HardRejectionPayload(
            rejected_command_id="inv-2026-04-29.ENV-REC-1.1.1",
            breaching_rules=(),
            suggested_modification="reduce size by 42%",
            headroom_after_hypothetical_compliance=(),
        )
    assert "at least one breaching rule" in str(exc_info.value)


def test_hard_rejection_payload_is_frozen() -> None:
    payload = HardRejectionPayload(
        rejected_command_id="inv-2026-04-29.ENV-REC-1.1.1",
        breaching_rules=(_make_rule_entry(),),
        suggested_modification="reduce size by 42%",
        headroom_after_hypothetical_compliance=(),
    )
    with pytest.raises(ValidationError):
        payload.suggested_modification = "changed"


# ---------------------------------------------------------------------------
# PositionSelectionAction / PositionSelectionResult
# ---------------------------------------------------------------------------


def test_position_selection_action_members() -> None:
    assert PositionSelectionAction.FULL_CLOSE.value == "full_close"
    assert PositionSelectionAction.PARTIAL_TRIM.value == "partial_trim"
    assert {member.value for member in PositionSelectionAction} == {"full_close", "partial_trim"}


def test_position_selection_result_full_close_constructs() -> None:
    result = PositionSelectionResult(
        position_id="pos-42",
        action=PositionSelectionAction.FULL_CLOSE,
        rationale="Largest unrealized loss; most liquid.",
    )
    assert result.action is PositionSelectionAction.FULL_CLOSE
    assert result.target_post_action_size_pct_of_portfolio is None


def test_position_selection_result_partial_trim_constructs() -> None:
    result = PositionSelectionResult(
        position_id="pos-42",
        action=PositionSelectionAction.PARTIAL_TRIM,
        target_post_action_size_pct_of_portfolio=4.75,
        rationale="Trim short to 95% of per-position limit.",
    )
    assert result.target_post_action_size_pct_of_portfolio == 4.75


def test_position_selection_result_full_close_with_target_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        PositionSelectionResult(
            position_id="pos-42",
            action=PositionSelectionAction.FULL_CLOSE,
            target_post_action_size_pct_of_portfolio=4.75,
            rationale="rationale",
        )
    assert "must be None for FULL_CLOSE" in str(exc_info.value)


def test_position_selection_result_partial_trim_without_target_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        PositionSelectionResult(
            position_id="pos-42",
            action=PositionSelectionAction.PARTIAL_TRIM,
            rationale="rationale",
        )
    assert "required for PARTIAL_TRIM" in str(exc_info.value)


def test_position_selection_result_is_frozen() -> None:
    result = PositionSelectionResult(
        position_id="pos-42",
        action=PositionSelectionAction.FULL_CLOSE,
        rationale="rationale",
    )
    with pytest.raises(ValidationError):
        result.position_id = "changed"
