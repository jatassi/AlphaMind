"""Tests for the BORROW_COST_ACCRUED event type, BorrowCostAccruedDetail, and wiring (ALP-718)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from alphamind._kernel.money import money
from alphamind.portfolio_state.events import ActivityLogEntry
from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail


class TestBorrowCostAccruedEventType:
    """EventType.BORROW_COST_ACCRUED exists with the correct string value."""

    def test_event_type_value(self) -> None:
        from alphamind.portfolio_state.events import EventType

        assert EventType.BORROW_COST_ACCRUED == "BORROW_COST_ACCRUED"

    def test_event_type_is_in_enum(self) -> None:
        from alphamind.portfolio_state.events import EventType

        assert "BORROW_COST_ACCRUED" in {e.value for e in EventType}


class TestBorrowCostAccruedDetail:
    """BorrowCostAccruedDetail is a frozen-slotted dataclass with the 5 required fields."""

    def _make_detail(self) -> BorrowCostAccruedDetail:
        from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail

        return BorrowCostAccruedDetail(
            accrued_amount_usd=money("12.34"),
            cumulative_accrued_usd=money("56.78"),
            annual_fee_pct_used=15.0,
            notional_usd_used=money("10000.00"),
            accrual_date=date(2026, 5, 27),
        )

    def test_importable_from_cash_margin(self) -> None:
        from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail

        assert BorrowCostAccruedDetail is not None

    def test_importable_from_events_package(self) -> None:
        from alphamind.portfolio_state.events import BorrowCostAccruedDetail

        assert BorrowCostAccruedDetail is not None

    def test_importable_from_activity_log_shim(self) -> None:
        from alphamind.portfolio_state.events.activity_log import BorrowCostAccruedDetail

        assert BorrowCostAccruedDetail is not None

    def test_is_frozen(self) -> None:
        import dataclasses

        # The dataclass is declared with frozen=True; verify by asserting that
        # __setattr__ is the frozen guard (not the default object.__setattr__).
        # The simplest portable check: frozen dataclasses have ``__delattr__``
        # and ``__setattr__`` overridden to raise ``FrozenInstanceError``.
        detail = self._make_detail()
        assert dataclasses.is_dataclass(detail)
        with pytest.raises(dataclasses.FrozenInstanceError):
            detail.__setattr__("annual_fee_pct_used", 20.0)

    def test_has_five_fields_in_order(self) -> None:
        import dataclasses

        from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail

        field_names = [f.name for f in dataclasses.fields(BorrowCostAccruedDetail)]
        assert field_names == [
            "accrued_amount_usd",
            "cumulative_accrued_usd",
            "annual_fee_pct_used",
            "notional_usd_used",
            "accrual_date",
        ]

    def test_construction_with_valid_fields(self) -> None:
        detail = self._make_detail()
        assert detail.accrued_amount_usd == Decimal("12.34")
        assert detail.cumulative_accrued_usd == Decimal("56.78")
        assert detail.annual_fee_pct_used == 15.0
        assert detail.notional_usd_used == Decimal("10000.00")
        assert detail.accrual_date == date(2026, 5, 27)

    def test_slots_present(self) -> None:
        from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail

        assert hasattr(BorrowCostAccruedDetail, "__slots__")


class TestBorrowCostAccruedRegistry:
    """Registry maps BORROW_COST_ACCRUED to BorrowCostAccruedDetail + CASH_AND_MARGIN."""

    def test_event_type_to_group(self) -> None:
        from alphamind.portfolio_state.events import (
            EVENT_TYPE_TO_GROUP,
            EventGroup,
            EventType,
        )

        assert EVENT_TYPE_TO_GROUP[EventType.BORROW_COST_ACCRUED] is EventGroup.CASH_AND_MARGIN

    def test_event_type_to_detail_class(self) -> None:
        from alphamind.portfolio_state.events import EVENT_TYPE_TO_DETAIL_CLASS, EventType
        from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail

        assert EVENT_TYPE_TO_DETAIL_CLASS[EventType.BORROW_COST_ACCRUED] is BorrowCostAccruedDetail

    def test_cash_margin_registry_includes_borrow_cost_accrued(self) -> None:
        from alphamind.portfolio_state.events import cash_margin
        from alphamind.portfolio_state.events.types import EventType

        keys = {entry[0] for entry in cash_margin._REGISTRY}
        assert EventType.BORROW_COST_ACCRUED in keys

    def test_cash_margin_registry_now_covers_eight_types(self) -> None:
        from alphamind.portfolio_state.events import cash_margin

        assert len(cash_margin._REGISTRY) == 8


class TestActivityLogEntryWithBorrowCostAccrued:
    """ActivityLogEntry accepts valid BORROW_COST_ACCRUED, rejects wrong group."""

    def _make_valid_entry(self) -> ActivityLogEntry:
        from alphamind.portfolio_state.events import (
            ActivityLogEntry,
            EventGroup,
            EventSource,
            EventType,
        )
        from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail

        return ActivityLogEntry(
            entry_id="entry-001",
            invocation_id="inv-001",
            timestamp=datetime(2026, 5, 27, 16, 0, 0, tzinfo=UTC),
            event_type=EventType.BORROW_COST_ACCRUED,
            event_group=EventGroup.CASH_AND_MARGIN,
            position_id="pos-001",
            order_id=None,
            thesis_id=None,
            source=EventSource.MARGIN_MONITOR,
            detail=BorrowCostAccruedDetail(
                accrued_amount_usd=money("5.00"),
                cumulative_accrued_usd=money("25.00"),
                annual_fee_pct_used=12.5,
                notional_usd_used=money("8000.00"),
                accrual_date=date(2026, 5, 27),
            ),
        )

    def test_valid_entry_constructs_without_error(self) -> None:
        entry = self._make_valid_entry()
        from alphamind.portfolio_state.events import EventType

        assert entry.event_type is EventType.BORROW_COST_ACCRUED

    def test_wrong_event_group_raises_value_error(self) -> None:
        from alphamind.portfolio_state.events import (
            ActivityLogEntry,
            EventGroup,
            EventSource,
            EventType,
        )
        from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail

        with pytest.raises(ValueError, match="event_group mismatch"):
            ActivityLogEntry(
                entry_id="entry-001",
                invocation_id="inv-001",
                timestamp=datetime(2026, 5, 27, 16, 0, 0, tzinfo=UTC),
                event_type=EventType.BORROW_COST_ACCRUED,
                event_group=EventGroup.RISK_AND_GUARDRAIL,  # wrong group
                position_id="pos-001",
                order_id=None,
                thesis_id=None,
                source=EventSource.MARGIN_MONITOR,
                detail=BorrowCostAccruedDetail(
                    accrued_amount_usd=money("5.00"),
                    cumulative_accrued_usd=money("25.00"),
                    annual_fee_pct_used=12.5,
                    notional_usd_used=money("8000.00"),
                    accrual_date=date(2026, 5, 27),
                ),
            )


class TestBorrowCostAccruedCodecRoundTrip:
    """BorrowCostAccruedDetail round-trips through encode_detail/decode_detail."""

    def test_round_trip_exact_equality(self) -> None:
        from alphamind.portfolio_state.events import decode_detail, encode_detail
        from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail

        detail = BorrowCostAccruedDetail(
            accrued_amount_usd=money("12.34"),
            cumulative_accrued_usd=money("56.78"),
            annual_fee_pct_used=15.0,
            notional_usd_used=money("10000.00"),
            accrual_date=date(2026, 5, 27),
        )
        encoded = encode_detail(detail)
        decoded = decode_detail(encoded, BorrowCostAccruedDetail)
        assert decoded == detail

    def test_money_precision_preserved(self) -> None:
        from alphamind.portfolio_state.events import decode_detail, encode_detail
        from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail

        detail = BorrowCostAccruedDetail(
            accrued_amount_usd=money("0.001"),
            cumulative_accrued_usd=money("0.003"),
            annual_fee_pct_used=15.0,
            notional_usd_used=money("9999.999"),
            accrual_date=date(2026, 5, 27),
        )
        decoded = decode_detail(encode_detail(detail), BorrowCostAccruedDetail)
        assert decoded.accrued_amount_usd == Decimal("0.001")
        assert decoded.notional_usd_used == Decimal("9999.999")

    def test_accrual_date_round_trips(self) -> None:
        from alphamind.portfolio_state.events import decode_detail, encode_detail
        from alphamind.portfolio_state.events.cash_margin import BorrowCostAccruedDetail

        target_date = date(2026, 5, 27)
        detail = BorrowCostAccruedDetail(
            accrued_amount_usd=money("1.00"),
            cumulative_accrued_usd=money("1.00"),
            annual_fee_pct_used=10.0,
            notional_usd_used=money("1000.00"),
            accrual_date=target_date,
        )
        decoded = decode_detail(encode_detail(detail), BorrowCostAccruedDetail)
        assert decoded.accrual_date == target_date
