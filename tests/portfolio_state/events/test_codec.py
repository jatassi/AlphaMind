"""Codec round-trip tests — frozen dataclasses ↔ JSON with Decimal precision.

The codec replaces the Pydantic ``model_dump_json`` / ``model_validate_json``
pair on the activity-log path. Every detail dataclass must survive a JSON
round-trip with exact equality, including ``Money`` / ``Price`` precision
(no binary-float drift on values like ``money("0.001")``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from alphamind._kernel.ids import BracketId, OrderId, PositionId, Symbol, ThesisId
from alphamind._kernel.money import money, price, signed_money
from alphamind.portfolio_state.events import (
    EVENT_TYPE_TO_DETAIL_CLASS,
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
    DistillationConfigChangeDetail,
    EmergencyInvocationRequestedDetail,
    EnvelopeParseFailedDetail,
    EnvelopeRejectionDetail,
    EventType,
    GreeksRefreshFailedDetail,
    GuardrailRejectionDetail,
    HaltActivatedDetail,
    HaltLiftedDetail,
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
    ProfileSwitchedDetail,
    ReconciliationAlertDetail,
    ReconciliationCorrectionDetail,
    RiskLimitApproachedDetail,
    RiskParameterChangedDetail,
    ThesisComponentAddedDetail,
    ThesisComponentUpdatedDetail,
    ThesisCreatedDetail,
    ThesisResolvedDetail,
    ThesisStatusChangedDetail,
    decode_detail,
    encode_detail,
)
from alphamind.config.models.main import Profile

_UTC_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
_HASH_HEX_64 = "f" * 64


def _all_detail_instances() -> list[tuple[type, object]]:
    """One representative instance per detail class.

    Each instance exercises the trickiest fields for that class — Money/Price,
    tuples, nested dataclasses, datetime-with-tz, Literal-typed strings.
    """
    return [
        (
            PositionOpenedDetail,
            PositionOpenedDetail(
                ticker=Symbol("AAPL"),
                direction="LONG",
                fill_price=price("150.25"),
                quantity=10.0,
                thesis_id=ThesisId("th-001"),
                bracket_id=BracketId("br-001"),
                mechanism=PositionOpenMechanism.ORDER_FILL,
                parent_position_id=None,
            ),
        ),
        (
            PositionClosedDetail,
            PositionClosedDetail(
                exit_method=PositionExitMethod.TARGET_REACHED,
                exit_price=money("175.50"),
                realized_pnl_usd=signed_money("250.75"),
                thesis_resolution_category="WIN",
            ),
        ),
        (
            PositionAddedDetail,
            PositionAddedDetail(
                additional_quantity=5.0,
                new_average_cost_basis=price("155.10"),
                addition_thesis_component_id="tc-001",
            ),
        ),
        (
            PositionReducedDetail,
            PositionReducedDetail(
                reduced_quantity=3.0,
                partial_realized_pnl_usd=signed_money("-75.25"),
                close_rationale_classification="PARTIAL_TARGET",
            ),
        ),
        (
            OrderSubmittedDetail,
            OrderSubmittedDetail(
                order_parameters_json={"side": "buy", "qty": 10},
                pm_command_id="cmd-001",
            ),
        ),
        (
            OrderFilledDetail,
            OrderFilledDetail(
                fill_price=price("150.001"),
                fill_quantity=10.0,
                slippage=signed_money("-0.250"),
                fees=money("0.001"),
            ),
        ),
        (
            OrderPartiallyFilledDetail,
            OrderPartiallyFilledDetail(
                fill_price=price("150.0"),
                fill_quantity=5.0,
                remaining_quantity=5.0,
            ),
        ),
        (
            OrderCancelledDetail,
            OrderCancelledDetail(cancel_reason="USER_REQUESTED", filled_quantity_at_cancellation=0),
        ),
        (
            OrderExpiredDetail,
            OrderExpiredDetail(filled_quantity_at_expiration=2),
        ),
        (
            OrderRejectedDetail,
            OrderRejectedDetail(
                rejection_reason="INSUFFICIENT_FUNDS",
                rejection_source=OrderRejectionSource.BROKER,
            ),
        ),
        (
            OrderModifiedDetail,
            OrderModifiedDetail(
                field_changed="limit_price",
                old_value="150.00",
                new_value="152.00",
                pm_rationale="Adjusted for market movement",
            ),
        ),
        (
            BracketActivatedDetail,
            BracketActivatedDetail(
                bracket_id=BracketId("br-001"),
                protective_leg_order_ids=("ord-sl-001", "ord-tp-001"),
            ),
        ),
        (
            BracketCompletedDetail,
            BracketCompletedDetail(
                triggered_leg_id="ord-tp-001",
                fill_details_json={"price": 175.0, "qty": 10},
            ),
        ),
        (
            BracketDissolvedDetail,
            BracketDissolvedDetail(cancelled_leg_order_ids=("ord-sl-001",)),
        ),
        (
            BracketModifiedDetail,
            BracketModifiedDetail(
                source=BracketModificationSource.FILL_ANCHOR_RECALCULATION,
                field_changed="stop_price",
                old_value="145.00",
                new_value="147.50",
                rationale=None,
            ),
        ),
        (
            BracketIncompleteWarningDetail,
            BracketIncompleteWarningDetail(
                missing_leg_types=("STOP_LOSS",),
                expected_resolution="PENDING",
            ),
        ),
        (
            BracketCancelledCorporateActionDetail,
            BracketCancelledCorporateActionDetail(
                bracket_id=BracketId("br-001"),
                cancellation_reason="CASH_MERGER",
                cancelled_leg_order_ids=("ord-sl-001", "ord-tp-001"),
            ),
        ),
        (
            ThesisCreatedDetail,
            ThesisCreatedDetail(thesis_id=ThesisId("th-001"), summary="Bullish on earnings"),
        ),
        (
            ThesisComponentAddedDetail,
            ThesisComponentAddedDetail(component_id="tc-001", component_type="CATALYST"),
        ),
        (
            ThesisComponentUpdatedDetail,
            ThesisComponentUpdatedDetail(
                component_id="tc-001",
                field_changed="status",
                old_value="PENDING",
                new_value="CONFIRMED",
            ),
        ),
        (
            ThesisResolvedDetail,
            ThesisResolvedDetail(
                resolution_category="WIN",
                component_outcomes_json={"tc-001": "CONFIRMED"},
            ),
        ),
        (
            ThesisStatusChangedDetail,
            ThesisStatusChangedDetail(old_status="ACTIVE", new_status="RESOLVED"),
        ),
        (
            CashDebitedDetail,
            CashDebitedDetail(
                amount_usd=money("1500.00"),
                reason=CashDebitReason.ENTRY_FILL,
                new_balance_usd=signed_money("98500.00"),
            ),
        ),
        (
            CashCreditedDetail,
            CashCreditedDetail(
                amount_usd=money("1750.50"),
                reason=CashCreditReason.EXIT_FILL,
                new_balance_usd=signed_money("101750.50"),
            ),
        ),
        (
            CapitalReservedDetail,
            CapitalReservedDetail(order_id=OrderId("ord-001"), amount_usd=money("1500.00")),
        ),
        (
            CapitalReleasedDetail,
            CapitalReleasedDetail(order_id=OrderId("ord-001"), amount_usd=money("1500.00")),
        ),
        (
            MarginCallDetail,
            MarginCallDetail(
                position_id=PositionId("pos-001"),
                margin_required_usd=money("5000.00"),
                margin_available_usd=money("3000.00"),
                deficit_usd=money("2000.00"),
            ),
        ),
        (
            MarginCallResolvedDetail,
            MarginCallResolvedDetail(resolution_method="DEPOSIT"),
        ),
        (
            MarginLiquidationDetail,
            MarginLiquidationDetail(
                position_id=PositionId("pos-001"),
                liquidation_price=price("140.05"),
                loss_usd=signed_money("-1000.25"),
            ),
        ),
        (
            GuardrailRejectionDetail,
            GuardrailRejectionDetail(
                command_summary="BUY 100 AAPL",
                blocking_rule_ids=("rule-concentration-001",),
                current_limit_values_json={"concentration": 0.18},
                headroom_json={"concentration": -0.03},
                suggested_modification=None,
            ),
        ),
        (
            RiskLimitApproachedDetail,
            RiskLimitApproachedDetail(
                metric_id="sector_concentration",
                current_value=0.14,
                threshold_value=0.15,
                limit_value=0.20,
            ),
        ),
        (
            RiskParameterChangedDetail,
            RiskParameterChangedDetail(
                old_parameter_set_json={"max_concentration": 0.15},
                new_parameter_set_json={"max_concentration": 0.12},
                regime_label="HIGH_VIX",
            ),
        ),
        (
            EmergencyInvocationRequestedDetail,
            EmergencyInvocationRequestedDetail(
                trigger_type="regime_jump",
                trigger_reason="Regime jump: normal -> crisis",
                cooldown_remaining_seconds=0,
            ),
        ),
        (
            HaltActivatedDetail,
            HaltActivatedDetail(
                halt_type="daily_drawdown",
                current_drawdown_pct=5.0,
                limit_pct=5.0,
                detected_at=_UTC_TS,
            ),
        ),
        (
            HaltLiftedDetail,
            HaltLiftedDetail(
                halt_type="daily_drawdown",
                current_drawdown_pct=2.0,
                lifted_at=_UTC_TS,
            ),
        ),
        (
            GreeksRefreshFailedDetail,
            GreeksRefreshFailedDetail(
                underlying_ticker=Symbol("SPY"),
                occ_symbol="SPY240115C00500000",
                failure_reason="iv_fetch_timeout",
                prior_as_of=_UTC_TS,
            ),
        ),
        (
            PMDecisionDetail,
            PMDecisionDetail(
                envelope_id="env-001",
                source_provenance_json={"source_type": "pm_analyst"},
                evaluation_json={"verdict": "APPROVE"},
                modifications_json=[],
                resulting_command_ids=("cmd-001",),
                verdict=PMVerdict.APPROVE,
            ),
        ),
        (
            CommandAbandonedDetail,
            CommandAbandonedDetail(
                envelope_id="env-001",
                command_id="cmd-001",
                originating_agent="pm_analyst",
                command_type="ADD",
                failure_reason="BROKER_TIMEOUT",
                retry_attempt_count=3,
            ),
        ),
        (
            EnvelopeParseFailedDetail,
            EnvelopeParseFailedDetail(
                attempted_envelope_id="ENV-REC-99",
                attempted_command_id="inv-2026-05-08.ENV-REC-99.0.0",
                validation_error_repr="source_provenance: Field required",
                raw_args_json='{"envelope_id": "ENV-REC-99"}',
            ),
        ),
        (
            EnvelopeRejectionDetail,
            EnvelopeRejectionDetail(
                envelope_id="ENV-REC-2",
                referenced_position_id="POS-NONEXISTENT",
                attempted_command_count=1,
                blocking_criteria=("position_id_resolves",),
                validation_errors_json='[{"criterion": "position_id_resolves"}]',
            ),
        ),
        (
            CorporateActionAppliedDetail,
            CorporateActionAppliedDetail(
                action_type=CorporateActionType.SPLIT,
                alpaca_activity_id="act-001",
                ticker=Symbol("AAPL"),
                new_ticker=None,
                ratio_or_amount=2.0,
                pre_action_quantity=10.0,
                post_action_quantity=20.0,
                pre_action_cost_basis=money("1500.00"),
                post_action_cost_basis=money("1500.00"),
                signed_cash_impact_usd=signed_money("0.00"),
                parent_position_id=None,
                resulting_position_status="OPEN",
            ),
        ),
        (
            ReconciliationAlertDetail,
            ReconciliationAlertDetail(
                domain="position",
                field_name="share_count",
                local_value=100.0,
                alpaca_value=99.5,
                delta_description="local 100.0 vs Alpaca 99.5",
            ),
        ),
        (
            ReconciliationCorrectionDetail,
            ReconciliationCorrectionDetail(
                domain="position",
                field_name="share_count",
                prior_local_value=100.0,
                applied_alpaca_value=99.5,
            ),
        ),
        (
            DistillationConfigChangeDetail,
            DistillationConfigChangeDetail(
                prior_hash=None,
                new_hash=_HASH_HEX_64,
                changes=(
                    DistillationConfigChange(key_path="a.first", old_value=1, new_value=2),
                    DistillationConfigChange(key_path="b.second", old_value=2.5, new_value=3.0),
                ),
                git_sha="abc1234",
            ),
        ),
        (
            ProfileSwitchedDetail,
            ProfileSwitchedDetail(
                previous_profile=Profile.small,
                new_profile=Profile.large,
                is_no_op=False,
            ),
        ),
    ]


class TestCodecRoundTrip:
    """Every detail dataclass round-trips through encode/decode with exact equality."""

    @pytest.mark.parametrize(("detail_cls", "instance"), _all_detail_instances())
    def test_round_trip_exact_equality(self, detail_cls: type, instance: object) -> None:
        encoded = encode_detail(instance)
        decoded: object = decode_detail(encoded, detail_cls)
        assert decoded == instance, f"{detail_cls.__name__} did not round-trip"

    def test_covers_every_event_type(self) -> None:
        """Every EventType has a representative instance in this test list."""
        covered_classes = {cls for cls, _ in _all_detail_instances()}
        for event_type in EventType:
            expected_class = EVENT_TYPE_TO_DETAIL_CLASS[event_type]
            assert expected_class in covered_classes, (
                f"missing round-trip coverage for {event_type.name} → {expected_class.__name__}"
            )

    def test_money_precision_preserved_through_encode_decode(self) -> None:
        """``money('0.001')`` survives encode/decode exactly as ``Decimal('0.001')``."""
        detail = OrderFilledDetail(
            fill_price=price("100.50"),
            fill_quantity=10.0,
            slippage=signed_money("0"),
            fees=money("0.001"),
        )
        decoded = decode_detail(encode_detail(detail), OrderFilledDetail)
        assert isinstance(decoded, OrderFilledDetail)
        # The fees value must be exactly ``Decimal('0.001')`` — no binary-float drift.
        assert decoded.fees == Decimal("0.001")
        assert str(decoded.fees) == "0.001"
        # And the round-trip must compare equal at the dataclass level too.
        assert decoded == detail


class TestResolveFieldHintsNarrowedCatch:
    """ALP-480 — ``_resolve_field_hints`` narrows its exception catch to
    ``(NameError, TypeError, AttributeError)``; other exception types must
    propagate so a real bug in the decoder is not masked.

    Previously the function caught bare ``Exception`` and silently fell back
    to ``{}``; the narrowed catch swallows only the expected forward-reference
    resolution failures.
    """

    def test_unexpected_exception_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Injecting a non-narrowed exception class from ``get_type_hints``
        propagates instead of being swallowed."""
        from alphamind.portfolio_state.events import codec as codec_mod

        class _SyntheticBugError(RuntimeError):
            """Stand-in for an unexpected decoder bug."""

        def _raise_synthetic(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise _SyntheticBugError("decoder bug")

        monkeypatch.setattr(codec_mod, "get_type_hints", _raise_synthetic)
        with pytest.raises(_SyntheticBugError):
            codec_mod._resolve_field_hints(OrderFilledDetail)

    def test_name_error_falls_through_to_empty_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The narrowed catch still handles ``NameError`` — the canonical
        forward-reference resolution failure."""
        from alphamind.portfolio_state.events import codec as codec_mod

        def _raise_name_error(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise NameError("missing forward reference")

        monkeypatch.setattr(codec_mod, "get_type_hints", _raise_name_error)
        assert codec_mod._resolve_field_hints(OrderFilledDetail) == {}
