"""Tests for the PENDING-position skeleton builder (ALP-716).

Verifies that _build_pending_position sets accrued_borrow_cost_usd=0.0 for
SHORT EQUITY proposals and accrued_borrow_cost_usd=None for LONG EQUITY proposals.

The function is private but tested directly because the ACs require explicit
skeleton validation without the overhead of a full async/DB fixture suite.
"""

from __future__ import annotations

from alphamind.commands.command_models import EquityInstrument
from alphamind.execution.write_paths.phase2.open import _build_pending_position
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionRecord,
    PositionStatus,
)

_SHORT_EQUITY_INSTRUMENT = EquityInstrument(asset_type="equity", ticker="GME", direction="short")
_LONG_EQUITY_INSTRUMENT = EquityInstrument(asset_type="equity", ticker="AAPL", direction="long")


def _build_short_skeleton() -> PositionRecord:
    return _build_pending_position(
        position_id="pos-pending-short-1",
        thesis_id="thesis-short-1",
        bracket_id="bracket-short-1",
        instrument=_SHORT_EQUITY_INSTRUMENT,
        direction=Direction.SHORT,
    )


def _build_long_skeleton() -> PositionRecord:
    return _build_pending_position(
        position_id="pos-pending-long-1",
        thesis_id="thesis-long-1",
        bracket_id="bracket-long-1",
        instrument=_LONG_EQUITY_INSTRUMENT,
        direction=Direction.LONG,
    )


class TestShortEquityPendingSkeleton:
    """AC-7: SHORT EQUITY skeleton has accrued_borrow_cost_usd=0.0."""

    def test_short_skeleton_constructs_cleanly(self) -> None:
        """_build_pending_position for SHORT EQUITY raises no validator error."""
        record = _build_short_skeleton()
        assert record.status == PositionStatus.PENDING
        assert record.direction == Direction.SHORT

    def test_short_skeleton_accrued_borrow_cost_is_zero(self) -> None:
        """AC-7: SHORT skeleton has accrued_borrow_cost_usd=0.0."""
        record = _build_short_skeleton()
        assert isinstance(record.details, EquityPositionDetails)
        assert record.details.accrued_borrow_cost_usd == 0.0

    def test_short_skeleton_other_short_fields_also_present(self) -> None:
        """Regression: existing short-only fields still initialise correctly."""
        record = _build_short_skeleton()
        assert isinstance(record.details, EquityPositionDetails)
        details = record.details
        assert details.borrow_rate_pct == 0.0
        assert details.margin_held_usd == 0.0
        assert details.locate_status is not None


class TestLongEquityPendingSkeleton:
    """AC-8: LONG EQUITY skeleton has accrued_borrow_cost_usd=None."""

    def test_long_skeleton_constructs_cleanly(self) -> None:
        """_build_pending_position for LONG EQUITY raises no error."""
        record = _build_long_skeleton()
        assert record.status == PositionStatus.PENDING
        assert record.direction == Direction.LONG

    def test_long_skeleton_accrued_borrow_cost_is_none(self) -> None:
        """AC-8: LONG skeleton has accrued_borrow_cost_usd=None."""
        record = _build_long_skeleton()
        assert isinstance(record.details, EquityPositionDetails)
        assert record.details.accrued_borrow_cost_usd is None

    def test_long_skeleton_other_short_fields_also_none(self) -> None:
        """Regression: LONG position has all four short-only fields as None."""
        record = _build_long_skeleton()
        assert isinstance(record.details, EquityPositionDetails)
        details = record.details
        assert details.borrow_rate_pct is None
        assert details.locate_status is None
        assert details.margin_held_usd is None
