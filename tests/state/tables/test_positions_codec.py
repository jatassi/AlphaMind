"""Tests for accrued_borrow_cost_usd codec round-trip (ALP-716).

Covers:
- SHORT equity position with accrued_borrow_cost_usd=42.75 round-trips through
  record_to_row -> row_to_record with the field intact.
- LONG equity position round-trips with accrued_borrow_cost_usd remaining None.
- Legacy details_json blob lacking the key deserialises with accrued_borrow_cost_usd=None
  (backward-compat for LONG positions persisted before this story landed).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from alphamind._kernel.ids import BracketId, PositionId, Symbol, ThesisId
from alphamind._kernel.money import money, price, signed_money
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.state.tables.positions_codec import (
    record_to_row,
    row_to_record,
)

# Import private helper for the legacy-blob test
from alphamind.state.tables.positions_codec import _equity_from_dict  # noqa: PLC2701

_FILL_TS = datetime(2026, 5, 27, 14, 30, tzinfo=UTC)

_FILL = PositionFill(
    fill_timestamp=_FILL_TS,
    fill_price=price(150.25),
    fill_quantity=60.0,
    slippage=signed_money(0.05),
    fees=money(1.25),
)


def _make_short_record(*, accrued_borrow_cost_usd: float = 42.75) -> PositionRecord:
    return PositionRecord(
        position_id=PositionId("pos-short-1"),
        thesis_id=ThesisId("thesis-short-1"),
        bracket_id=BracketId("bracket-short-1"),
        status=PositionStatus.OPEN,
        direction=Direction.SHORT,
        entry_timestamp=_FILL_TS,
        details=EquityPositionDetails(
            ticker=Symbol("GME"),
            share_count=100.0,
            average_cost_basis_per_share=25.0,
            borrow_rate_pct=15.0,
            accrued_borrow_cost_usd=accrued_borrow_cost_usd,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=2500.0,
        ),
        execution_history=(_FILL,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _make_long_record() -> PositionRecord:
    return PositionRecord(
        position_id=PositionId("pos-long-1"),
        thesis_id=ThesisId("thesis-long-1"),
        bracket_id=BracketId("bracket-long-1"),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_FILL_TS,
        details=EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=100.0,
            average_cost_basis_per_share=150.0,
        ),
        execution_history=(_FILL,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


class TestAccruedBorrowCostCodec:
    """AC-4 & AC-5: round-trip codec for accrued_borrow_cost_usd."""

    def test_short_position_round_trips_accrued_borrow_cost(self) -> None:
        """AC-4: SHORT with accrued_borrow_cost_usd=42.75 survives record_to_row -> row_to_record."""
        record = _make_short_record(accrued_borrow_cost_usd=42.75)
        row = record_to_row(record)
        restored = row_to_record(row)

        assert isinstance(restored.details, EquityPositionDetails)
        # Decimal-equivalent equality OK per spec
        assert float(restored.details.accrued_borrow_cost_usd) == pytest.approx(42.75)  # type: ignore[arg-type]

    def test_short_position_details_json_contains_key(self) -> None:
        """Codec serialises accrued_borrow_cost_usd into details_json."""
        record = _make_short_record(accrued_borrow_cost_usd=42.75)
        row = record_to_row(record)
        blob = json.loads(row.details_json)
        assert "accrued_borrow_cost_usd" in blob
        assert float(blob["accrued_borrow_cost_usd"]) == pytest.approx(42.75)

    def test_long_position_round_trips_with_none(self) -> None:
        """AC-5: LONG position round-trips with accrued_borrow_cost_usd remaining None."""
        record = _make_long_record()
        row = record_to_row(record)
        restored = row_to_record(row)

        assert isinstance(restored.details, EquityPositionDetails)
        assert restored.details.accrued_borrow_cost_usd is None

    def test_zero_accrued_cost_round_trips(self) -> None:
        """Boundary: 0.0 value (fresh SHORT entry) also round-trips faithfully."""
        record = _make_short_record(accrued_borrow_cost_usd=0.0)
        row = record_to_row(record)
        restored = row_to_record(row)

        assert isinstance(restored.details, EquityPositionDetails)
        assert restored.details.accrued_borrow_cost_usd == 0.0


class TestLegacyBlobDeserialization:
    """AC-6: legacy details_json blobs lacking accrued_borrow_cost_usd deserialise to None."""

    def test_missing_key_deserialises_to_none(self) -> None:
        """A pre-Story-01 blob (no accrued_borrow_cost_usd key) gives None for LONG position."""
        legacy_payload = {
            "instrument_type": "EQUITY",
            "ticker": "AAPL",
            "share_count": 100.0,
            "average_cost_basis_per_share": 150.0,
            "borrow_rate_pct": None,
            "locate_status": None,
            "margin_held_usd": None,
            # deliberately absent: "accrued_borrow_cost_usd"
        }
        details = _equity_from_dict(legacy_payload)
        assert details.accrued_borrow_cost_usd is None

    def test_explicit_none_in_blob_deserialises_to_none(self) -> None:
        """A blob with the key explicitly set to null also deserialises to None."""
        payload = {
            "instrument_type": "EQUITY",
            "ticker": "AAPL",
            "share_count": 100.0,
            "average_cost_basis_per_share": 150.0,
            "borrow_rate_pct": None,
            "accrued_borrow_cost_usd": None,
            "locate_status": None,
            "margin_held_usd": None,
        }
        details = _equity_from_dict(payload)
        assert details.accrued_borrow_cost_usd is None
