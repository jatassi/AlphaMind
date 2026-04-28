"""Tests for activity log records — story 03e."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import get_args

import pytest
from pydantic import ValidationError

from alphamind.portfolio_state.records.activity_log import (
    EVENT_TYPE_TO_DETAIL_CLASS,
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    AnyDetailType,
    BracketActivatedDetail,
    BracketCancelledCorporateActionDetail,
    BracketCompletedDetail,
    BracketDissolvedDetail,
    BracketIncompleteWarningDetail,
    BracketModificationSource,
    BracketModifiedDetail,
    CapitalReleasedDetail,
    CapitalReservedDetail,
    CashCreditedDetail,
    CashCreditReason,
    CashDebitedDetail,
    CashDebitReason,
    CommandAbandonedDetail,
    CorporateActionAppliedDetail,
    CorporateActionType,
    DistillationConfigChange,
    EventGroup,
    EventSource,
    EventType,
    GuardrailRejectionDetail,
    MarginCallDetail,
    MarginCallResolvedDetail,
    MarginLiquidationDetail,
    OrderCancelledDetail,
    OrderExpiredDetail,
    OrderFilledDetail,
    OrderModifiedDetail,
    OrderPartiallyFilledDetail,
    OrderRejectedDetail,
    OrderRejectionSource,
    OrderSubmittedDetail,
    PMDecisionDetail,
    PMVerdict,
    PositionAddedDetail,
    PositionClosedDetail,
    PositionExitMethod,
    PositionOpenedDetail,
    PositionOpenMechanism,
    PositionReducedDetail,
    RiskLimitApproachedDetail,
    RiskParameterChangedDetail,
    ThesisComponentAddedDetail,
    ThesisComponentUpdatedDetail,
    ThesisCreatedDetail,
    ThesisResolvedDetail,
    ThesisStatusChangedDetail,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)


def _entry(
    event_type: EventType,
    event_group: EventGroup,
    detail: object,
    source: EventSource = EventSource.FILL_PROCESSOR,
) -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id="eid-001",
        invocation_id="inv-001",
        timestamp=_UTC_TS,
        event_type=event_type,
        event_group=event_group,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=source,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Enum membership tests
# ---------------------------------------------------------------------------


class TestEnumMembers:
    """All enums are StrEnum with the documented members and no extras."""

    def test_event_group_is_str_enum(self) -> None:
        assert issubclass(EventGroup, StrEnum)

    def test_event_group_members(self) -> None:
        expected = {
            "POSITION_LIFECYCLE",
            "ORDER_LIFECYCLE",
            "BRACKET",
            "THESIS",
            "CASH_AND_MARGIN",
            "RISK_AND_GUARDRAIL",
            "PM_DECISION",
            "CORPORATE_ACTION",
            "CONFIGURATION",
        }
        assert {m.name for m in EventGroup} == expected

    def test_event_type_is_str_enum(self) -> None:
        assert issubclass(EventType, StrEnum)

    def test_event_type_has_exactly_36_members(self) -> None:
        assert len(EventType) == 36

    def test_event_type_position_lifecycle_members(self) -> None:
        for name in ("POSITION_OPENED", "POSITION_CLOSED", "POSITION_ADDED", "POSITION_REDUCED"):
            assert hasattr(EventType, name), f"Missing EventType.{name}"

    def test_event_type_order_lifecycle_members(self) -> None:
        for name in (
            "ORDER_SUBMITTED",
            "ORDER_FILLED",
            "ORDER_PARTIALLY_FILLED",
            "ORDER_CANCELLED",
            "ORDER_EXPIRED",
            "ORDER_REJECTED",
            "ORDER_MODIFIED",
        ):
            assert hasattr(EventType, name), f"Missing EventType.{name}"

    def test_event_type_bracket_members(self) -> None:
        for name in (
            "BRACKET_ACTIVATED",
            "BRACKET_COMPLETED",
            "BRACKET_DISSOLVED",
            "BRACKET_MODIFIED",
            "BRACKET_INCOMPLETE_WARNING",
            "BRACKET_CANCELLED_CORPORATE_ACTION",
        ):
            assert hasattr(EventType, name), f"Missing EventType.{name}"

    def test_event_type_thesis_members(self) -> None:
        for name in (
            "THESIS_CREATED",
            "THESIS_COMPONENT_ADDED",
            "THESIS_COMPONENT_UPDATED",
            "THESIS_RESOLVED",
            "THESIS_STATUS_CHANGED",
        ):
            assert hasattr(EventType, name), f"Missing EventType.{name}"

    def test_event_type_cash_and_margin_members(self) -> None:
        for name in (
            "CASH_DEBITED",
            "CASH_CREDITED",
            "CAPITAL_RESERVED",
            "CAPITAL_RELEASED",
            "MARGIN_CALL",
            "MARGIN_CALL_RESOLVED",
            "MARGIN_LIQUIDATION",
        ):
            assert hasattr(EventType, name), f"Missing EventType.{name}"

    def test_event_type_risk_and_guardrail_members(self) -> None:
        for name in ("GUARDRAIL_REJECTION", "RISK_LIMIT_APPROACHED", "RISK_PARAMETER_CHANGED"):
            assert hasattr(EventType, name), f"Missing EventType.{name}"

    def test_event_type_pm_decision_members(self) -> None:
        for name in ("PM_DECISION", "COMMAND_ABANDONED"):
            assert hasattr(EventType, name), f"Missing EventType.{name}"

    def test_event_type_corporate_action_members(self) -> None:
        assert hasattr(EventType, "CORPORATE_ACTION_APPLIED")

    def test_event_source_members(self) -> None:
        expected = {
            "FILL_PROCESSOR",
            "COMMAND_EXECUTOR",
            "BRACKET_MANAGER",
            "MARGIN_MONITOR",
            "GUARDRAIL_LAYER",
            "CORPORATE_ACTION_PROCESSOR",
            "CONFIG_RELOAD",
            "OPERATOR_CONSOLE",
        }
        assert {m.name for m in EventSource} == expected

    def test_position_exit_method_members(self) -> None:
        expected = {
            "STOP_TRIGGERED",
            "TARGET_REACHED",
            "PM_DECISION",
            "TIME_EXPIRED",
            "MARGIN_LIQUIDATION",
            "FORCED_BUY_IN",
            "CORPORATE_ACTION_CASH_MERGER",
        }
        assert {m.name for m in PositionExitMethod} == expected

    def test_position_open_mechanism_members(self) -> None:
        assert {m.name for m in PositionOpenMechanism} == {"ORDER_FILL", "SPIN_OFF_FROM_PARENT"}

    def test_order_rejection_source_members(self) -> None:
        assert {m.name for m in OrderRejectionSource} == {"GUARDRAIL", "BROKER"}

    def test_cash_debit_reason_members(self) -> None:
        assert {m.name for m in CashDebitReason} == {
            "ENTRY_FILL",
            "FEES",
            "CASH_DIVIDEND_SHORT_OBLIGATION",
        }

    def test_cash_credit_reason_members(self) -> None:
        assert {m.name for m in CashCreditReason} == {
            "EXIT_FILL",
            "CASH_DIVIDEND_LONG",
            "FRACTIONAL_SHARE_CASH_OUT",
            "CASH_MERGER_PROCEEDS",
        }

    def test_corporate_action_type_members(self) -> None:
        expected = {
            "SPLIT",
            "REVERSE_SPLIT",
            "STOCK_DIVIDEND",
            "CASH_DIVIDEND_LONG",
            "CASH_DIVIDEND_SHORT",
            "CASH_MERGER",
            "STOCK_MERGER",
            "SPIN_OFF",
            "SYMBOL_CHANGE",
        }
        assert {m.name for m in CorporateActionType} == expected

    def test_pm_verdict_members(self) -> None:
        assert {m.name for m in PMVerdict} == {
            "APPROVE",
            "APPROVE_WITH_MODIFICATION",
            "REJECT",
        }

    def test_bracket_modification_source_members(self) -> None:
        assert {m.name for m in BracketModificationSource} == {
            "PM",
            "FILL_ANCHOR_RECALCULATION",
            "CORPORATE_ACTION_ADJUSTMENT",
        }


# ---------------------------------------------------------------------------
# Mapping exhaustiveness
# ---------------------------------------------------------------------------


class TestMappingExhaustiveness:
    """EVENT_TYPE_TO_DETAIL_CLASS and EVENT_TYPE_TO_GROUP cover every EventType member."""

    def test_detail_class_mapping_covers_all_event_types(self) -> None:
        assert len(EVENT_TYPE_TO_DETAIL_CLASS) == len(EventType)

    def test_group_mapping_covers_all_event_types(self) -> None:
        assert len(EVENT_TYPE_TO_GROUP) == len(EventType)

    def test_detail_class_mapping_has_no_extra_keys(self) -> None:
        for key in EVENT_TYPE_TO_DETAIL_CLASS:
            assert key in EventType

    def test_group_mapping_has_no_extra_keys(self) -> None:
        for key in EVENT_TYPE_TO_GROUP:
            assert key in EventType

    def test_every_event_type_in_detail_class_mapping(self) -> None:
        for et in EventType:
            assert et in EVENT_TYPE_TO_DETAIL_CLASS, f"{et} missing from EVENT_TYPE_TO_DETAIL_CLASS"

    def test_every_event_type_in_group_mapping(self) -> None:
        for et in EventType:
            assert et in EVENT_TYPE_TO_GROUP, f"{et} missing from EVENT_TYPE_TO_GROUP"


# ---------------------------------------------------------------------------
# AnyDetailType alias
# ---------------------------------------------------------------------------


class TestAnyDetailTypeAlias:
    """AnyDetailType is exported and covers all 35 detail-payload classes."""

    def test_any_detail_type_has_36_members(self) -> None:
        members = get_args(AnyDetailType)
        assert len(members) == 36

    def test_any_detail_type_covers_all_detail_classes(self) -> None:
        members = set(get_args(AnyDetailType))
        for cls in EVENT_TYPE_TO_DETAIL_CLASS.values():
            assert cls in members, f"{cls.__name__} missing from AnyDetailType"


# ---------------------------------------------------------------------------
# Per-event-type detail class happy-path fixtures (35 total)
# ---------------------------------------------------------------------------


class TestDetailClassHappyPaths:
    """One happy-path round-trip test per event type."""

    def test_position_opened_detail(self) -> None:
        d = PositionOpenedDetail(
            ticker="AAPL",
            direction="LONG",
            fill_price=150.0,
            quantity=10.0,
            thesis_id="th-001",
            bracket_id="br-001",
            mechanism=PositionOpenMechanism.ORDER_FILL,
            parent_position_id=None,
        )
        assert d.ticker == "AAPL"

    def test_position_opened_detail_spin_off(self) -> None:
        d = PositionOpenedDetail(
            ticker="SPIN",
            direction="LONG",
            fill_price=50.0,
            quantity=5.0,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.SPIN_OFF_FROM_PARENT,
            parent_position_id="pos-parent-001",
        )
        assert d.parent_position_id == "pos-parent-001"

    def test_position_closed_detail(self) -> None:
        d = PositionClosedDetail(
            exit_method=PositionExitMethod.TARGET_REACHED,
            exit_price=175.0,
            realized_pnl_usd=250.0,
            thesis_resolution_category="WIN",
        )
        assert d.exit_method == PositionExitMethod.TARGET_REACHED

    def test_position_added_detail(self) -> None:
        d = PositionAddedDetail(
            additional_quantity=5.0,
            new_average_cost_basis=155.0,
            addition_thesis_component_id="tc-001",
        )
        assert d.additional_quantity == 5.0

    def test_position_reduced_detail(self) -> None:
        d = PositionReducedDetail(
            reduced_quantity=3.0,
            partial_realized_pnl_usd=75.0,
            close_rationale_classification="PARTIAL_TARGET",
        )
        assert d.reduced_quantity == 3.0

    def test_order_submitted_detail(self) -> None:
        d = OrderSubmittedDetail(
            order_parameters_json={"side": "buy", "qty": 10},
            pm_command_id="cmd-001",
        )
        assert d.pm_command_id == "cmd-001"

    def test_order_filled_detail(self) -> None:
        d = OrderFilledDetail(
            fill_price=150.25,
            fill_quantity=10.0,
            slippage=0.25,
            fees=1.50,
        )
        assert d.fill_price == 150.25

    def test_order_partially_filled_detail(self) -> None:
        d = OrderPartiallyFilledDetail(
            fill_price=150.0,
            fill_quantity=5.0,
            remaining_quantity=5.0,
        )
        assert d.remaining_quantity == 5.0

    def test_order_cancelled_detail(self) -> None:
        d = OrderCancelledDetail(
            cancel_reason="USER_REQUESTED",
            filled_quantity_at_cancellation=0,
        )
        assert d.filled_quantity_at_cancellation == 0

    def test_order_expired_detail(self) -> None:
        d = OrderExpiredDetail(filled_quantity_at_expiration=0)
        assert d.filled_quantity_at_expiration == 0

    def test_order_rejected_detail(self) -> None:
        d = OrderRejectedDetail(
            rejection_reason="INSUFFICIENT_FUNDS",
            rejection_source=OrderRejectionSource.BROKER,
        )
        assert d.rejection_source == OrderRejectionSource.BROKER

    def test_order_modified_detail(self) -> None:
        d = OrderModifiedDetail(
            field_changed="limit_price",
            old_value="150.00",
            new_value="152.00",
            pm_rationale="Adjusted for market movement",
        )
        assert d.field_changed == "limit_price"

    def test_bracket_activated_detail(self) -> None:
        d = BracketActivatedDetail(
            bracket_id="br-001",
            protective_leg_order_ids=("ord-sl-001", "ord-tp-001"),
        )
        assert len(d.protective_leg_order_ids) == 2

    def test_bracket_completed_detail(self) -> None:
        d = BracketCompletedDetail(
            triggered_leg_id="ord-tp-001",
            fill_details_json={"price": 175.0, "qty": 10},
        )
        assert d.triggered_leg_id == "ord-tp-001"

    def test_bracket_dissolved_detail(self) -> None:
        d = BracketDissolvedDetail(cancelled_leg_order_ids=("ord-sl-001",))
        assert len(d.cancelled_leg_order_ids) == 1

    def test_bracket_modified_detail_pm(self) -> None:
        d = BracketModifiedDetail(
            source=BracketModificationSource.PM,
            field_changed="stop_price",
            old_value="145.00",
            new_value="147.00",
            rationale="Trailing stop adjustment",
        )
        assert d.rationale == "Trailing stop adjustment"

    def test_bracket_modified_detail_fill_anchor(self) -> None:
        d = BracketModifiedDetail(
            source=BracketModificationSource.FILL_ANCHOR_RECALCULATION,
            field_changed="stop_price",
            old_value="145.00",
            new_value="147.50",
            rationale=None,
        )
        assert d.rationale is None

    def test_bracket_incomplete_warning_detail(self) -> None:
        d = BracketIncompleteWarningDetail(
            missing_leg_types=("STOP_LOSS",),
            expected_resolution="ORDER_SUBMISSION_PENDING",
        )
        assert "STOP_LOSS" in d.missing_leg_types

    def test_bracket_cancelled_corporate_action_detail(self) -> None:
        d = BracketCancelledCorporateActionDetail(
            bracket_id="br-001",
            cancellation_reason="CASH_MERGER",
            cancelled_leg_order_ids=("ord-sl-001", "ord-tp-001"),
        )
        assert d.bracket_id == "br-001"

    def test_thesis_created_detail(self) -> None:
        d = ThesisCreatedDetail(thesis_id="th-001", summary="Bullish on earnings beat")
        assert d.thesis_id == "th-001"

    def test_thesis_component_added_detail(self) -> None:
        d = ThesisComponentAddedDetail(component_id="tc-001", component_type="CATALYST")
        assert d.component_type == "CATALYST"

    def test_thesis_component_updated_detail(self) -> None:
        d = ThesisComponentUpdatedDetail(
            component_id="tc-001",
            field_changed="status",
            old_value="PENDING",
            new_value="CONFIRMED",
        )
        assert d.new_value == "CONFIRMED"

    def test_thesis_resolved_detail(self) -> None:
        d = ThesisResolvedDetail(
            resolution_category="WIN",
            component_outcomes_json={"tc-001": "CONFIRMED"},
        )
        assert d.resolution_category == "WIN"

    def test_thesis_status_changed_detail(self) -> None:
        d = ThesisStatusChangedDetail(old_status="ACTIVE", new_status="RESOLVED")
        assert d.new_status == "RESOLVED"

    def test_cash_debited_detail(self) -> None:
        d = CashDebitedDetail(
            amount_usd=1500.0,
            reason=CashDebitReason.ENTRY_FILL,
            new_balance_usd=98500.0,
        )
        assert d.reason == CashDebitReason.ENTRY_FILL

    def test_cash_credited_detail(self) -> None:
        d = CashCreditedDetail(
            amount_usd=1750.0,
            reason=CashCreditReason.EXIT_FILL,
            new_balance_usd=101750.0,
        )
        assert d.reason == CashCreditReason.EXIT_FILL

    def test_capital_reserved_detail(self) -> None:
        d = CapitalReservedDetail(order_id="ord-001", amount_usd=1500.0)
        assert d.amount_usd == 1500.0

    def test_capital_released_detail(self) -> None:
        d = CapitalReleasedDetail(order_id="ord-001", amount_usd=1500.0)
        assert d.amount_usd == 1500.0

    def test_margin_call_detail(self) -> None:
        d = MarginCallDetail(
            position_id="pos-001",
            margin_required_usd=5000.0,
            margin_available_usd=3000.0,
            deficit_usd=2000.0,
        )
        assert d.deficit_usd == 2000.0

    def test_margin_call_resolved_detail(self) -> None:
        d = MarginCallResolvedDetail(resolution_method="DEPOSIT")
        assert d.resolution_method == "DEPOSIT"

    def test_margin_liquidation_detail(self) -> None:
        d = MarginLiquidationDetail(
            position_id="pos-001",
            liquidation_price=140.0,
            loss_usd=1000.0,
        )
        assert d.loss_usd == 1000.0

    def test_guardrail_rejection_detail(self) -> None:
        d = GuardrailRejectionDetail(
            command_summary="BUY 100 AAPL",
            blocking_rule_ids=("rule-concentration-001",),
            current_limit_values_json={"concentration": 0.18},
            headroom_json={"concentration": -0.03},
            suggested_modification=None,
        )
        assert len(d.blocking_rule_ids) == 1

    def test_risk_limit_approached_detail(self) -> None:
        d = RiskLimitApproachedDetail(
            metric_id="sector_concentration",
            current_value=0.14,
            threshold_value=0.15,
            limit_value=0.20,
        )
        assert d.metric_id == "sector_concentration"

    def test_risk_parameter_changed_detail(self) -> None:
        d = RiskParameterChangedDetail(
            old_parameter_set_json={"max_concentration": 0.15},
            new_parameter_set_json={"max_concentration": 0.12},
            regime_label="HIGH_VIX",
        )
        assert d.regime_label == "HIGH_VIX"

    def test_pm_decision_detail(self) -> None:
        d = PMDecisionDetail(
            envelope_id="env-001",
            source_provenance_json={"source_type": "pm_analyst", "rec_id": "rec-001"},
            evaluation_json={"verdict": "APPROVE", "rationale": "Strong setup"},
            modifications_json=[],
            resulting_command_ids=("cmd-001",),
            verdict=PMVerdict.APPROVE,
        )
        assert d.verdict == PMVerdict.APPROVE

    def test_command_abandoned_detail(self) -> None:
        d = CommandAbandonedDetail(
            envelope_id="env-001",
            command_id="cmd-001",
            originating_agent="pm_analyst",
            failure_reason="BROKER_TIMEOUT",
            retry_attempt_count=3,
        )
        assert d.retry_attempt_count == 3

    def test_corporate_action_applied_detail(self) -> None:
        d = CorporateActionAppliedDetail(
            action_type=CorporateActionType.SPLIT,
            alpaca_activity_id="act-001",
            ticker="AAPL",
            new_ticker=None,
            ratio_or_amount=2.0,
            pre_action_quantity=10.0,
            post_action_quantity=20.0,
            pre_action_cost_basis=1500.0,
            post_action_cost_basis=1500.0,
            signed_cash_impact_usd=0.0,
            parent_position_id=None,
            resulting_position_status="OPEN",
        )
        assert d.action_type == CorporateActionType.SPLIT


# ---------------------------------------------------------------------------
# PositionOpenedDetail validator
# ---------------------------------------------------------------------------


class TestPositionOpenedDetailValidator:
    """parent_position_id must be None when mechanism is ORDER_FILL."""

    def test_order_fill_with_parent_position_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            PositionOpenedDetail(
                ticker="AAPL",
                direction="LONG",
                fill_price=150.0,
                quantity=10.0,
                thesis_id=None,
                bracket_id=None,
                mechanism=PositionOpenMechanism.ORDER_FILL,
                parent_position_id="pos-parent-001",
            )

    def test_spin_off_with_none_parent_allowed(self) -> None:
        # spin-off with None parent_position_id is valid per the spec
        d = PositionOpenedDetail(
            ticker="SPIN",
            direction="LONG",
            fill_price=50.0,
            quantity=5.0,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.SPIN_OFF_FROM_PARENT,
            parent_position_id=None,
        )
        assert d.parent_position_id is None


# ---------------------------------------------------------------------------
# BracketModifiedDetail validator
# ---------------------------------------------------------------------------


class TestBracketModifiedDetailValidator:
    """rationale must be non-None when source is not FILL_ANCHOR_RECALCULATION."""

    def test_pm_source_with_none_rationale_raises(self) -> None:
        with pytest.raises(ValidationError):
            BracketModifiedDetail(
                source=BracketModificationSource.PM,
                field_changed="stop_price",
                old_value="145.00",
                new_value="147.00",
                rationale=None,
            )

    def test_corporate_action_adjustment_with_none_rationale_raises(self) -> None:
        with pytest.raises(ValidationError):
            BracketModifiedDetail(
                source=BracketModificationSource.CORPORATE_ACTION_ADJUSTMENT,
                field_changed="stop_price",
                old_value="145.00",
                new_value="147.00",
                rationale=None,
            )

    def test_fill_anchor_with_none_rationale_allowed(self) -> None:
        d = BracketModifiedDetail(
            source=BracketModificationSource.FILL_ANCHOR_RECALCULATION,
            field_changed="stop_price",
            old_value="145.00",
            new_value="147.50",
            rationale=None,
        )
        assert d.rationale is None


# ---------------------------------------------------------------------------
# ActivityLogEntry happy-path (one per event type — 35 total)
# ---------------------------------------------------------------------------


class TestActivityLogEntryHappyPath:
    """One happy-path entry construction per event type."""

    def _make_entry(
        self,
        event_type: EventType,
        event_group: EventGroup,
        detail: object,
        source: EventSource = EventSource.FILL_PROCESSOR,
    ) -> ActivityLogEntry:
        return _entry(event_type, event_group, detail, source=source)

    def test_position_opened(self) -> None:
        detail = PositionOpenedDetail(
            ticker="AAPL",
            direction="LONG",
            fill_price=150.0,
            quantity=10.0,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.ORDER_FILL,
            parent_position_id=None,
        )
        e = self._make_entry(EventType.POSITION_OPENED, EventGroup.POSITION_LIFECYCLE, detail)
        assert e.event_type == EventType.POSITION_OPENED

    def test_position_closed(self) -> None:
        detail = PositionClosedDetail(
            exit_method=PositionExitMethod.TARGET_REACHED,
            exit_price=175.0,
            realized_pnl_usd=250.0,
            thesis_resolution_category="WIN",
        )
        e = self._make_entry(EventType.POSITION_CLOSED, EventGroup.POSITION_LIFECYCLE, detail)
        assert e.event_type == EventType.POSITION_CLOSED

    def test_position_added(self) -> None:
        detail = PositionAddedDetail(
            additional_quantity=5.0,
            new_average_cost_basis=155.0,
            addition_thesis_component_id="tc-001",
        )
        e = self._make_entry(EventType.POSITION_ADDED, EventGroup.POSITION_LIFECYCLE, detail)
        assert e.event_type == EventType.POSITION_ADDED

    def test_position_reduced(self) -> None:
        detail = PositionReducedDetail(
            reduced_quantity=3.0,
            partial_realized_pnl_usd=75.0,
            close_rationale_classification="PARTIAL_TARGET",
        )
        e = self._make_entry(EventType.POSITION_REDUCED, EventGroup.POSITION_LIFECYCLE, detail)
        assert e.event_type == EventType.POSITION_REDUCED

    def test_order_submitted(self) -> None:
        detail = OrderSubmittedDetail(
            order_parameters_json={"side": "buy"},
            pm_command_id="cmd-001",
        )
        e = self._make_entry(EventType.ORDER_SUBMITTED, EventGroup.ORDER_LIFECYCLE, detail)
        assert e.event_type == EventType.ORDER_SUBMITTED

    def test_order_filled(self) -> None:
        detail = OrderFilledDetail(fill_price=150.25, fill_quantity=10.0, slippage=0.25, fees=1.50)
        e = self._make_entry(EventType.ORDER_FILLED, EventGroup.ORDER_LIFECYCLE, detail)
        assert e.event_type == EventType.ORDER_FILLED

    def test_order_partially_filled(self) -> None:
        detail = OrderPartiallyFilledDetail(
            fill_price=150.0, fill_quantity=5.0, remaining_quantity=5.0
        )
        e = self._make_entry(EventType.ORDER_PARTIALLY_FILLED, EventGroup.ORDER_LIFECYCLE, detail)
        assert e.event_type == EventType.ORDER_PARTIALLY_FILLED

    def test_order_cancelled(self) -> None:
        detail = OrderCancelledDetail(
            cancel_reason="USER_REQUESTED", filled_quantity_at_cancellation=0
        )
        e = self._make_entry(EventType.ORDER_CANCELLED, EventGroup.ORDER_LIFECYCLE, detail)
        assert e.event_type == EventType.ORDER_CANCELLED

    def test_order_expired(self) -> None:
        detail = OrderExpiredDetail(filled_quantity_at_expiration=0)
        e = self._make_entry(EventType.ORDER_EXPIRED, EventGroup.ORDER_LIFECYCLE, detail)
        assert e.event_type == EventType.ORDER_EXPIRED

    def test_order_rejected(self) -> None:
        detail = OrderRejectedDetail(
            rejection_reason="INSUFFICIENT_FUNDS",
            rejection_source=OrderRejectionSource.BROKER,
        )
        e = self._make_entry(EventType.ORDER_REJECTED, EventGroup.ORDER_LIFECYCLE, detail)
        assert e.event_type == EventType.ORDER_REJECTED

    def test_order_modified(self) -> None:
        detail = OrderModifiedDetail(
            field_changed="limit_price",
            old_value="150.00",
            new_value="152.00",
            pm_rationale="Market moved",
        )
        e = self._make_entry(EventType.ORDER_MODIFIED, EventGroup.ORDER_LIFECYCLE, detail)
        assert e.event_type == EventType.ORDER_MODIFIED

    def test_bracket_activated(self) -> None:
        detail = BracketActivatedDetail(
            bracket_id="br-001",
            protective_leg_order_ids=("ord-sl-001",),
        )
        e = self._make_entry(
            EventType.BRACKET_ACTIVATED,
            EventGroup.BRACKET,
            detail,
            source=EventSource.BRACKET_MANAGER,
        )
        assert e.event_type == EventType.BRACKET_ACTIVATED

    def test_bracket_completed(self) -> None:
        detail = BracketCompletedDetail(
            triggered_leg_id="ord-tp-001",
            fill_details_json={"price": 175.0},
        )
        e = self._make_entry(
            EventType.BRACKET_COMPLETED,
            EventGroup.BRACKET,
            detail,
            source=EventSource.BRACKET_MANAGER,
        )
        assert e.event_type == EventType.BRACKET_COMPLETED

    def test_bracket_dissolved(self) -> None:
        detail = BracketDissolvedDetail(cancelled_leg_order_ids=("ord-sl-001",))
        e = self._make_entry(
            EventType.BRACKET_DISSOLVED,
            EventGroup.BRACKET,
            detail,
            source=EventSource.BRACKET_MANAGER,
        )
        assert e.event_type == EventType.BRACKET_DISSOLVED

    def test_bracket_modified(self) -> None:
        detail = BracketModifiedDetail(
            source=BracketModificationSource.PM,
            field_changed="stop_price",
            old_value="145.00",
            new_value="147.00",
            rationale="Trailing adjustment",
        )
        e = self._make_entry(
            EventType.BRACKET_MODIFIED,
            EventGroup.BRACKET,
            detail,
            source=EventSource.BRACKET_MANAGER,
        )
        assert e.event_type == EventType.BRACKET_MODIFIED

    def test_bracket_incomplete_warning(self) -> None:
        detail = BracketIncompleteWarningDetail(
            missing_leg_types=("STOP_LOSS",),
            expected_resolution="PENDING",
        )
        e = self._make_entry(
            EventType.BRACKET_INCOMPLETE_WARNING,
            EventGroup.BRACKET,
            detail,
            source=EventSource.BRACKET_MANAGER,
        )
        assert e.event_type == EventType.BRACKET_INCOMPLETE_WARNING

    def test_bracket_cancelled_corporate_action(self) -> None:
        detail = BracketCancelledCorporateActionDetail(
            bracket_id="br-001",
            cancellation_reason="CASH_MERGER",
            cancelled_leg_order_ids=("ord-sl-001",),
        )
        e = self._make_entry(
            EventType.BRACKET_CANCELLED_CORPORATE_ACTION,
            EventGroup.BRACKET,
            detail,
            source=EventSource.CORPORATE_ACTION_PROCESSOR,
        )
        assert e.event_type == EventType.BRACKET_CANCELLED_CORPORATE_ACTION

    def test_thesis_created(self) -> None:
        detail = ThesisCreatedDetail(thesis_id="th-001", summary="Bullish on earnings")
        e = self._make_entry(EventType.THESIS_CREATED, EventGroup.THESIS, detail)
        assert e.event_type == EventType.THESIS_CREATED

    def test_thesis_component_added(self) -> None:
        detail = ThesisComponentAddedDetail(component_id="tc-001", component_type="CATALYST")
        e = self._make_entry(EventType.THESIS_COMPONENT_ADDED, EventGroup.THESIS, detail)
        assert e.event_type == EventType.THESIS_COMPONENT_ADDED

    def test_thesis_component_updated(self) -> None:
        detail = ThesisComponentUpdatedDetail(
            component_id="tc-001",
            field_changed="status",
            old_value="PENDING",
            new_value="CONFIRMED",
        )
        e = self._make_entry(EventType.THESIS_COMPONENT_UPDATED, EventGroup.THESIS, detail)
        assert e.event_type == EventType.THESIS_COMPONENT_UPDATED

    def test_thesis_resolved(self) -> None:
        detail = ThesisResolvedDetail(
            resolution_category="WIN",
            component_outcomes_json={"tc-001": "CONFIRMED"},
        )
        e = self._make_entry(EventType.THESIS_RESOLVED, EventGroup.THESIS, detail)
        assert e.event_type == EventType.THESIS_RESOLVED

    def test_thesis_status_changed(self) -> None:
        detail = ThesisStatusChangedDetail(old_status="ACTIVE", new_status="RESOLVED")
        e = self._make_entry(EventType.THESIS_STATUS_CHANGED, EventGroup.THESIS, detail)
        assert e.event_type == EventType.THESIS_STATUS_CHANGED

    def test_cash_debited(self) -> None:
        detail = CashDebitedDetail(
            amount_usd=1500.0,
            reason=CashDebitReason.ENTRY_FILL,
            new_balance_usd=98500.0,
        )
        e = self._make_entry(
            EventType.CASH_DEBITED,
            EventGroup.CASH_AND_MARGIN,
            detail,
            source=EventSource.FILL_PROCESSOR,
        )
        assert e.event_type == EventType.CASH_DEBITED

    def test_cash_credited(self) -> None:
        detail = CashCreditedDetail(
            amount_usd=1750.0,
            reason=CashCreditReason.EXIT_FILL,
            new_balance_usd=101750.0,
        )
        e = self._make_entry(
            EventType.CASH_CREDITED,
            EventGroup.CASH_AND_MARGIN,
            detail,
            source=EventSource.FILL_PROCESSOR,
        )
        assert e.event_type == EventType.CASH_CREDITED

    def test_capital_reserved(self) -> None:
        detail = CapitalReservedDetail(order_id="ord-001", amount_usd=1500.0)
        e = self._make_entry(
            EventType.CAPITAL_RESERVED,
            EventGroup.CASH_AND_MARGIN,
            detail,
            source=EventSource.COMMAND_EXECUTOR,
        )
        assert e.event_type == EventType.CAPITAL_RESERVED

    def test_capital_released(self) -> None:
        detail = CapitalReleasedDetail(order_id="ord-001", amount_usd=1500.0)
        e = self._make_entry(
            EventType.CAPITAL_RELEASED,
            EventGroup.CASH_AND_MARGIN,
            detail,
            source=EventSource.COMMAND_EXECUTOR,
        )
        assert e.event_type == EventType.CAPITAL_RELEASED

    def test_margin_call(self) -> None:
        detail = MarginCallDetail(
            position_id="pos-001",
            margin_required_usd=5000.0,
            margin_available_usd=3000.0,
            deficit_usd=2000.0,
        )
        e = self._make_entry(
            EventType.MARGIN_CALL,
            EventGroup.CASH_AND_MARGIN,
            detail,
            source=EventSource.MARGIN_MONITOR,
        )
        assert e.event_type == EventType.MARGIN_CALL

    def test_margin_call_resolved(self) -> None:
        detail = MarginCallResolvedDetail(resolution_method="DEPOSIT")
        e = self._make_entry(
            EventType.MARGIN_CALL_RESOLVED,
            EventGroup.CASH_AND_MARGIN,
            detail,
            source=EventSource.MARGIN_MONITOR,
        )
        assert e.event_type == EventType.MARGIN_CALL_RESOLVED

    def test_margin_liquidation(self) -> None:
        detail = MarginLiquidationDetail(
            position_id="pos-001",
            liquidation_price=140.0,
            loss_usd=1000.0,
        )
        e = self._make_entry(
            EventType.MARGIN_LIQUIDATION,
            EventGroup.CASH_AND_MARGIN,
            detail,
            source=EventSource.MARGIN_MONITOR,
        )
        assert e.event_type == EventType.MARGIN_LIQUIDATION

    def test_guardrail_rejection(self) -> None:
        detail = GuardrailRejectionDetail(
            command_summary="BUY 100 AAPL",
            blocking_rule_ids=("rule-001",),
            current_limit_values_json={"concentration": 0.18},
            headroom_json={"concentration": -0.03},
            suggested_modification=None,
        )
        e = self._make_entry(
            EventType.GUARDRAIL_REJECTION,
            EventGroup.RISK_AND_GUARDRAIL,
            detail,
            source=EventSource.GUARDRAIL_LAYER,
        )
        assert e.event_type == EventType.GUARDRAIL_REJECTION

    def test_risk_limit_approached(self) -> None:
        detail = RiskLimitApproachedDetail(
            metric_id="sector_concentration",
            current_value=0.14,
            threshold_value=0.15,
            limit_value=0.20,
        )
        e = self._make_entry(
            EventType.RISK_LIMIT_APPROACHED,
            EventGroup.RISK_AND_GUARDRAIL,
            detail,
            source=EventSource.GUARDRAIL_LAYER,
        )
        assert e.event_type == EventType.RISK_LIMIT_APPROACHED

    def test_risk_parameter_changed(self) -> None:
        detail = RiskParameterChangedDetail(
            old_parameter_set_json={"max_concentration": 0.15},
            new_parameter_set_json={"max_concentration": 0.12},
            regime_label="HIGH_VIX",
        )
        e = self._make_entry(
            EventType.RISK_PARAMETER_CHANGED,
            EventGroup.RISK_AND_GUARDRAIL,
            detail,
            source=EventSource.GUARDRAIL_LAYER,
        )
        assert e.event_type == EventType.RISK_PARAMETER_CHANGED

    def test_pm_decision(self) -> None:
        detail = PMDecisionDetail(
            envelope_id="env-001",
            source_provenance_json={"source_type": "pm_analyst"},
            evaluation_json={"verdict": "APPROVE"},
            modifications_json=[],
            resulting_command_ids=("cmd-001",),
            verdict=PMVerdict.APPROVE,
        )
        e = self._make_entry(
            EventType.PM_DECISION,
            EventGroup.PM_DECISION,
            detail,
            source=EventSource.COMMAND_EXECUTOR,
        )
        assert e.event_type == EventType.PM_DECISION

    def test_command_abandoned(self) -> None:
        detail = CommandAbandonedDetail(
            envelope_id="env-001",
            command_id="cmd-001",
            originating_agent="pm_analyst",
            failure_reason="BROKER_TIMEOUT",
            retry_attempt_count=3,
        )
        e = self._make_entry(
            EventType.COMMAND_ABANDONED,
            EventGroup.PM_DECISION,
            detail,
            source=EventSource.COMMAND_EXECUTOR,
        )
        assert e.event_type == EventType.COMMAND_ABANDONED

    def test_corporate_action_applied(self) -> None:
        detail = CorporateActionAppliedDetail(
            action_type=CorporateActionType.SPLIT,
            alpaca_activity_id="act-001",
            ticker="AAPL",
            new_ticker=None,
            ratio_or_amount=2.0,
            pre_action_quantity=10.0,
            post_action_quantity=20.0,
            pre_action_cost_basis=1500.0,
            post_action_cost_basis=1500.0,
            signed_cash_impact_usd=0.0,
            parent_position_id=None,
            resulting_position_status="OPEN",
        )
        e = self._make_entry(
            EventType.CORPORATE_ACTION_APPLIED,
            EventGroup.CORPORATE_ACTION,
            detail,
            source=EventSource.CORPORATE_ACTION_PROCESSOR,
        )
        assert e.event_type == EventType.CORPORATE_ACTION_APPLIED


# ---------------------------------------------------------------------------
# ActivityLogEntry validation failures
# ---------------------------------------------------------------------------


class TestActivityLogEntryValidation:
    """ActivityLogEntry rejects invalid inputs."""

    def test_mismatched_detail_raises_with_both_class_names(self) -> None:
        """detail mismatch raises ValidationError naming expected and actual class."""
        detail = PositionClosedDetail(
            exit_method=PositionExitMethod.TARGET_REACHED,
            exit_price=175.0,
            realized_pnl_usd=250.0,
            thesis_resolution_category="WIN",
        )
        with pytest.raises(ValidationError) as exc_info:
            ActivityLogEntry(
                entry_id="eid-001",
                invocation_id="inv-001",
                timestamp=_UTC_TS,
                event_type=EventType.ORDER_FILLED,
                event_group=EventGroup.ORDER_LIFECYCLE,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=EventSource.FILL_PROCESSOR,
                detail=detail,
            )
        msg = str(exc_info.value)
        assert "OrderFilledDetail" in msg
        assert "PositionClosedDetail" in msg

    def test_mismatched_event_group_raises(self) -> None:
        """event_group mismatch raises ValidationError naming both groups."""
        detail = PositionOpenedDetail(
            ticker="AAPL",
            direction="LONG",
            fill_price=150.0,
            quantity=10.0,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.ORDER_FILL,
            parent_position_id=None,
        )
        with pytest.raises(ValidationError) as exc_info:
            ActivityLogEntry(
                entry_id="eid-001",
                invocation_id="inv-001",
                timestamp=_UTC_TS,
                event_type=EventType.POSITION_OPENED,
                event_group=EventGroup.ORDER_LIFECYCLE,  # wrong group
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=EventSource.FILL_PROCESSOR,
                detail=detail,
            )
        msg = str(exc_info.value)
        assert "ORDER_LIFECYCLE" in msg
        assert "POSITION_LIFECYCLE" in msg

    def test_naive_timestamp_raises(self) -> None:
        """naive timestamp raises ValidationError."""
        detail = PositionOpenedDetail(
            ticker="AAPL",
            direction="LONG",
            fill_price=150.0,
            quantity=10.0,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.ORDER_FILL,
            parent_position_id=None,
        )
        naive_ts = _UTC_TS.replace(tzinfo=None)
        with pytest.raises(ValidationError):
            ActivityLogEntry(
                entry_id="eid-001",
                invocation_id="inv-001",
                timestamp=naive_ts,
                event_type=EventType.POSITION_OPENED,
                event_group=EventGroup.POSITION_LIFECYCLE,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=EventSource.FILL_PROCESSOR,
                detail=detail,
            )

    def test_empty_entry_id_raises(self) -> None:
        """empty entry_id raises ValidationError."""
        detail = PositionOpenedDetail(
            ticker="AAPL",
            direction="LONG",
            fill_price=150.0,
            quantity=10.0,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.ORDER_FILL,
            parent_position_id=None,
        )
        with pytest.raises(ValidationError):
            ActivityLogEntry(
                entry_id="",
                invocation_id="inv-001",
                timestamp=_UTC_TS,
                event_type=EventType.POSITION_OPENED,
                event_group=EventGroup.POSITION_LIFECYCLE,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=EventSource.FILL_PROCESSOR,
                detail=detail,
            )

    def test_empty_invocation_id_raises(self) -> None:
        """empty invocation_id raises ValidationError."""
        detail = PositionOpenedDetail(
            ticker="AAPL",
            direction="LONG",
            fill_price=150.0,
            quantity=10.0,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.ORDER_FILL,
            parent_position_id=None,
        )
        with pytest.raises(ValidationError):
            ActivityLogEntry(
                entry_id="eid-001",
                invocation_id="",
                timestamp=_UTC_TS,
                event_type=EventType.POSITION_OPENED,
                event_group=EventGroup.POSITION_LIFECYCLE,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=EventSource.FILL_PROCESSOR,
                detail=detail,
            )


# ---------------------------------------------------------------------------
# Configuration event additions (story 14a)
# ---------------------------------------------------------------------------


class TestConfigurationEventEnumMembers:
    """The configuration-event enum additions are present (story 14a contract)."""

    def test_event_group_configuration_member_exists(self) -> None:
        from alphamind.portfolio_state.records.activity_log import EventGroup

        assert EventGroup.CONFIGURATION.value == "CONFIGURATION"

    def test_event_type_distillation_config_change_exists(self) -> None:
        from alphamind.portfolio_state.records.activity_log import EventType

        assert EventType.DISTILLATION_CONFIG_CHANGE.value == "DISTILLATION_CONFIG_CHANGE"

    def test_event_source_config_reload_exists(self) -> None:
        from alphamind.portfolio_state.records.activity_log import EventSource

        assert EventSource.CONFIG_RELOAD.value == "CONFIG_RELOAD"

    def test_event_source_operator_console_exists(self) -> None:
        from alphamind.portfolio_state.records.activity_log import EventSource

        assert EventSource.OPERATOR_CONSOLE.value == "OPERATOR_CONSOLE"


class TestDistillationConfigChange:
    """``DistillationConfigChange`` represents one entry in the ``changes`` array."""

    def test_round_trip_through_pydantic(self) -> None:
        from alphamind.portfolio_state.records.activity_log import DistillationConfigChange

        change = DistillationConfigChange(
            key_path="anomaly_detection.volume_anomaly_sigma",
            old_value=2.5,
            new_value=3.0,
        )
        assert change.key_path == "anomaly_detection.volume_anomaly_sigma"
        assert change.old_value == 2.5
        assert change.new_value == 3.0
        rebuilt = DistillationConfigChange.model_validate(change.model_dump())
        assert rebuilt == change

    def test_is_frozen(self) -> None:
        from alphamind.portfolio_state.records.activity_log import DistillationConfigChange

        change = DistillationConfigChange(key_path="x", old_value=1, new_value=2)
        with pytest.raises(ValidationError):
            change.key_path = "y"

    def test_empty_key_path_raises(self) -> None:
        from alphamind.portfolio_state.records.activity_log import DistillationConfigChange

        with pytest.raises(ValidationError):
            DistillationConfigChange(key_path="", old_value=1, new_value=2)


_HASH_HEX_64 = "f" * 64
_OTHER_HASH_HEX_64 = "a" * 64


def _change(
    key_path: str = "anomaly_detection.volume_anomaly_sigma",
    old_value: object = 2.5,
    new_value: object = 3.0,
) -> DistillationConfigChange:
    return DistillationConfigChange(key_path=key_path, old_value=old_value, new_value=new_value)


class TestDistillationConfigChangeDetail:
    """``DistillationConfigChangeDetail`` is the per-event-type detail payload."""

    def test_round_trip_through_pydantic(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
        )

        detail = DistillationConfigChangeDetail(
            config_file="config/distillation.yaml",
            prior_hash=_OTHER_HASH_HEX_64,
            new_hash=_HASH_HEX_64,
            changes=(_change(),),
            git_sha="abc1234",
        )
        assert detail.config_file == "config/distillation.yaml"
        rebuilt = DistillationConfigChangeDetail.model_validate(detail.model_dump())
        assert rebuilt == detail

    def test_default_config_file(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
        )

        detail = DistillationConfigChangeDetail(
            prior_hash=None,
            new_hash=_HASH_HEX_64,
            changes=(),
            git_sha="abc1234",
        )
        assert detail.config_file == "config/distillation.yaml"

    def test_prior_hash_none_allowed(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
        )

        detail = DistillationConfigChangeDetail(
            prior_hash=None,
            new_hash=_HASH_HEX_64,
            changes=(),
            git_sha="abc1234",
        )
        assert detail.prior_hash is None

    def test_is_frozen(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
        )

        detail = DistillationConfigChangeDetail(
            prior_hash=None,
            new_hash=_HASH_HEX_64,
            changes=(),
            git_sha="abc1234",
        )
        with pytest.raises(ValidationError):
            detail.git_sha = "deadbeef"

    def test_empty_config_file_raises(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
        )

        with pytest.raises(ValidationError):
            DistillationConfigChangeDetail(
                config_file="",
                prior_hash=None,
                new_hash=_HASH_HEX_64,
                changes=(),
                git_sha="abc1234",
            )

    def test_empty_new_hash_raises(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
        )

        with pytest.raises(ValidationError):
            DistillationConfigChangeDetail(
                prior_hash=None,
                new_hash="",
                changes=(),
                git_sha="abc1234",
            )

    def test_missing_new_hash_raises(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
        )

        with pytest.raises(ValidationError):
            DistillationConfigChangeDetail.model_validate(
                {
                    "prior_hash": None,
                    "changes": (),
                    "git_sha": "abc1234",
                }
            )

    def test_empty_git_sha_raises(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
        )

        with pytest.raises(ValidationError):
            DistillationConfigChangeDetail(
                prior_hash=None,
                new_hash=_HASH_HEX_64,
                changes=(),
                git_sha="",
            )

    def test_unsorted_changes_raises_naming_offending_pair(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
        )

        with pytest.raises(ValidationError) as exc_info:
            DistillationConfigChangeDetail(
                prior_hash=None,
                new_hash=_HASH_HEX_64,
                changes=(
                    _change(key_path="b.x"),
                    _change(key_path="a.y"),
                ),
                git_sha="abc1234",
            )
        msg = str(exc_info.value)
        assert "b.x" in msg
        assert "a.y" in msg

    def test_duplicate_key_path_raises(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
        )

        with pytest.raises(ValidationError):
            DistillationConfigChangeDetail(
                prior_hash=None,
                new_hash=_HASH_HEX_64,
                changes=(
                    _change(key_path="a.x"),
                    _change(key_path="a.x"),
                ),
                git_sha="abc1234",
            )

    def test_non_string_key_path_rejected(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChange,
        )

        with pytest.raises(ValidationError):
            DistillationConfigChange.model_validate(
                {"key_path": 123, "old_value": 1, "new_value": 2}
            )


class TestDistillationConfigChangeRegistration:
    """The new event type is wired into the catalog mappings."""

    def test_event_type_to_detail_class_lookup(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            EVENT_TYPE_TO_DETAIL_CLASS,
            DistillationConfigChangeDetail,
            EventType,
        )

        assert (
            EVENT_TYPE_TO_DETAIL_CLASS[EventType.DISTILLATION_CONFIG_CHANGE]
            is DistillationConfigChangeDetail
        )

    def test_event_type_to_group_lookup(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            EVENT_TYPE_TO_GROUP,
            EventGroup,
            EventType,
        )

        assert EVENT_TYPE_TO_GROUP[EventType.DISTILLATION_CONFIG_CHANGE] == EventGroup.CONFIGURATION

    def test_any_detail_type_includes_distillation_detail(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            AnyDetailType,
            DistillationConfigChangeDetail,
        )

        assert DistillationConfigChangeDetail in get_args(AnyDetailType)

    def test_activity_log_entry_accepts_distillation_config_change(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            ActivityLogEntry,
            DistillationConfigChangeDetail,
            EventGroup,
            EventSource,
            EventType,
        )

        detail = DistillationConfigChangeDetail(
            prior_hash=None,
            new_hash=_HASH_HEX_64,
            changes=(),
            git_sha="abc1234",
        )
        entry = ActivityLogEntry(
            entry_id="eid-001",
            invocation_id="inv-001",
            timestamp=_UTC_TS,
            event_type=EventType.DISTILLATION_CONFIG_CHANGE,
            event_group=EventGroup.CONFIGURATION,
            position_id=None,
            order_id=None,
            thesis_id=None,
            source=EventSource.CONFIG_RELOAD,
            detail=detail,
        )
        assert entry.event_type == EventType.DISTILLATION_CONFIG_CHANGE

    def test_activity_log_entry_rejects_wrong_group(self) -> None:
        from alphamind.portfolio_state.records.activity_log import (
            ActivityLogEntry,
            DistillationConfigChangeDetail,
            EventGroup,
            EventSource,
            EventType,
        )

        detail = DistillationConfigChangeDetail(
            prior_hash=None,
            new_hash=_HASH_HEX_64,
            changes=(),
            git_sha="abc1234",
        )
        with pytest.raises(ValidationError):
            ActivityLogEntry(
                entry_id="eid-001",
                invocation_id="inv-001",
                timestamp=_UTC_TS,
                event_type=EventType.DISTILLATION_CONFIG_CHANGE,
                event_group=EventGroup.PM_DECISION,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=EventSource.CONFIG_RELOAD,
                detail=detail,
            )
