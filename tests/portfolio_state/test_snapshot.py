"""Tests for PortfolioStateSnapshot and its inline rollup types (story 04a)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money
from alphamind.portfolio_state.events.activity_log import (
    EventType,
)
from alphamind.portfolio_state.records.orders import (
    EquityInstrumentSpec,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
)
from alphamind.portfolio_state.records.positions import (
    PositionStatus,
)
from alphamind.portfolio_state.snapshot import (
    PortfolioStateSnapshot,
)

from ._view_builders import (
    _make_active_risk_parameters,
    _make_activity_entry,
    _make_bracket,
    _make_cash_ledger,
    _make_directional_exposure,
    _make_drawdown_state,
    _make_open_position,
    _make_pending_position,
    _make_pm_decision_entry,
    _make_portfolio_pnl,
    _make_risk_budget,
    _make_sector_entry,
    _make_snapshot,
    _make_thesis_quality,
)

# ---------------------------------------------------------------------------
# Shared timestamps
# ---------------------------------------------------------------------------

_T0 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
_T1 = datetime(2025, 1, 1, 10, 1, 0, tzinfo=UTC)
_T2 = datetime(2025, 1, 1, 10, 2, 0, tzinfo=UTC)
_INV_ID = "inv-test-001"


# ---------------------------------------------------------------------------
# Low-level record builders
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PortfolioPnL
# ---------------------------------------------------------------------------


class TestPortfolioPnL:
    def test_valid_construction(self) -> None:
        pnl = _make_portfolio_pnl()
        assert pnl.total_unrealized_pnl_usd == 500.0
        assert pnl.rolling_realized_pnl == {
            "1d": 200.0,
            "3d": 600.0,
            "5d": 1000.0,
            "20d": 3000.0,
        }
        assert pnl.win_rate_pct == 62.5
        assert pnl.profit_factor == 2.0

    def test_nullable_fields_accept_none(self) -> None:
        pnl = _make_portfolio_pnl(
            win_rate_pct=None,
            average_win_size_usd=None,
            average_loss_size_usd=None,
            profit_factor=None,
        )
        assert pnl.win_rate_pct is None
        assert pnl.average_win_size_usd is None
        assert pnl.profit_factor is None

    def test_frozen(self) -> None:
        pnl = _make_portfolio_pnl()
        with pytest.raises((ValueError, TypeError, AttributeError)):
            pnl.total_unrealized_pnl_usd = money(999.0)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SectorExposureEntry
# ---------------------------------------------------------------------------


class TestSectorExposureEntry:
    def test_valid_construction(self) -> None:
        entry = _make_sector_entry()
        assert entry.sector == "TECHNOLOGY"
        assert entry.long_delta_adjusted_usd == 10000.0
        assert entry.long_short_ratio == 2.0

    def test_long_short_ratio_none_when_no_short(self) -> None:
        entry = _make_sector_entry(short_delta_adjusted_usd=0.0, long_short_ratio=None)
        assert entry.long_short_ratio is None

    def test_zero_pct_fields_accepted(self) -> None:
        entry = _make_sector_entry(long_pct_of_portfolio=0.0, short_pct_of_portfolio=0.0)
        assert entry.long_pct_of_portfolio == 0.0
        assert entry.short_pct_of_portfolio == 0.0

    def test_unclassified_sentinel(self) -> None:
        entry = _make_sector_entry(sector="UNCLASSIFIED")
        assert entry.sector == "UNCLASSIFIED"

    def test_frozen(self) -> None:
        entry = _make_sector_entry()
        with pytest.raises((ValueError, TypeError, AttributeError)):
            entry.sector = "OTHER"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DirectionalExposure
# ---------------------------------------------------------------------------


class TestDirectionalExposure:
    def test_valid_construction(self) -> None:
        de = _make_directional_exposure()
        assert de.total_long_delta_adjusted_usd == 50000.0
        assert de.gross_pct_of_portfolio == 70.0

    def test_zero_portfolio_all_zeros(self) -> None:
        de = _make_directional_exposure(
            total_long_delta_adjusted_usd=0.0,
            total_short_delta_adjusted_usd=0.0,
            net_directional_pct_of_portfolio=0.0,
            gross_pct_of_portfolio=0.0,
        )
        assert de.gross_pct_of_portfolio == 0.0

    def test_negative_gross_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_directional_exposure(gross_pct_of_portfolio=-1.0)

    def test_frozen(self) -> None:
        de = _make_directional_exposure()
        with pytest.raises((ValueError, TypeError, AttributeError)):
            de.gross_pct_of_portfolio = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Happy-path PortfolioStateSnapshot
# ---------------------------------------------------------------------------


class TestPortfolioStateSnapshotHappyPath:
    def test_constructs_without_error(self) -> None:
        snap = _make_snapshot()
        assert snap.invocation_id == _INV_ID
        assert len(snap.open_positions) == 2
        assert len(snap.pending_positions) == 1
        assert len(snap.brackets) == 3

    def test_position_by_id_open(self) -> None:
        snap = _make_snapshot()
        pos = snap.position_by_id("POS-001")
        assert pos is not None
        assert pos.position_id == "POS-001"

    def test_position_by_id_pending(self) -> None:
        snap = _make_snapshot()
        pos = snap.position_by_id("POS-003")
        assert pos is not None
        assert pos.position_id == "POS-003"

    def test_position_by_id_unknown(self) -> None:
        snap = _make_snapshot()
        assert snap.position_by_id("UNKNOWN") is None

    def test_bracket_for_position_found(self) -> None:
        snap = _make_snapshot()
        brk = snap.bracket_for_position("POS-001")
        assert brk is not None
        assert brk.bracket_id == "BRK-001"

    def test_bracket_for_position_unknown(self) -> None:
        snap = _make_snapshot()
        assert snap.bracket_for_position("UNKNOWN") is None

    def test_active_thesis_for_position_found(self) -> None:
        snap = _make_snapshot()
        thesis = snap.active_thesis_for_position("POS-001")
        assert thesis is not None
        assert thesis.thesis_id == "THESIS-001"

    def test_active_thesis_for_position_no_thesis(self) -> None:
        snap = _make_snapshot()
        # POS-002 has no thesis
        assert snap.active_thesis_for_position("POS-002") is None

    def test_active_thesis_for_position_unknown(self) -> None:
        snap = _make_snapshot()
        assert snap.active_thesis_for_position("UNKNOWN") is None

    def test_pending_orders_for_position_found(self) -> None:
        snap = _make_snapshot()
        orders = snap.pending_orders_for_position("POS-001")
        assert len(orders) == 1
        assert orders[0].order_id == "ORD-001"

    def test_pending_orders_for_position_empty(self) -> None:
        snap = _make_snapshot()
        orders = snap.pending_orders_for_position("POS-002")
        assert orders == ()

    def test_pending_orders_for_unknown_position(self) -> None:
        snap = _make_snapshot()
        orders = snap.pending_orders_for_position("UNKNOWN")
        assert orders == ()

    def test_pipeline_invocation_started_at_present(self) -> None:
        snap = _make_snapshot()
        assert snap.pipeline_invocation_started_at == _T2


# ---------------------------------------------------------------------------
# Validator: open position status
# ---------------------------------------------------------------------------


class TestOpenPositionStatusValidator:
    def test_open_position_correct_status_passes(self) -> None:
        snap = _make_snapshot()
        assert all(p.status == PositionStatus.OPEN for p in snap.open_positions)

    def test_open_position_wrong_status_raises(self) -> None:
        bad_pos = _make_pending_position("POS-BAD")
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(open_positions=(bad_pos,))


# ---------------------------------------------------------------------------
# Validator: pending position status
# ---------------------------------------------------------------------------


class TestPendingPositionStatusValidator:
    def test_pending_position_correct_status_passes(self) -> None:
        snap = _make_snapshot()
        assert all(p.status == PositionStatus.PENDING for p in snap.pending_positions)

    def test_pending_position_wrong_status_raises(self) -> None:
        bad_pos = _make_open_position("POS-004")
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(pending_positions=(bad_pos,))


# ---------------------------------------------------------------------------
# Validator: pending order status
# ---------------------------------------------------------------------------


class TestPendingOrderStatusValidator:
    def test_partially_filled_order_passes(self) -> None:
        order = OrderRecord(
            order_id=OrderId("ORD-002"),
            position_id=PositionId("POS-001"),
            bracket_id=BracketId("BRK-001"),
            role=OrderRole.ENTRY,
            instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
            direction=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            price_parameters=PriceParameters(limit_price=None, stop_trigger_price=None),
            quantity=10.0,
            duration=OrderDuration.DAY,
            status=OrderStatus.PARTIALLY_FILLED,
            alpaca_order_id=AlpacaOrderId("alp-002"),
            alpaca_order_id_chain=(AlpacaOrderId("alp-002"),),
            submission_timestamp=_T0,
            last_update_timestamp=_T0,
            filled_quantity=5.0,
            avg_fill_price=150.0,
            remaining_quantity=5.0,
            modification_count=0,
            originating_thesis_id=None,
            originating_pm_command_id=None,
            age_hours=1.0,
        )
        snap = _make_snapshot(pending_orders=(order,))
        assert snap.pending_orders[0].status == OrderStatus.PARTIALLY_FILLED

    def test_filled_order_in_pending_raises(self) -> None:
        filled_order = OrderRecord(
            order_id=OrderId("ORD-FILLED"),
            position_id=PositionId("POS-001"),
            bracket_id=BracketId("BRK-001"),
            role=OrderRole.ENTRY,
            instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
            direction=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            price_parameters=PriceParameters(limit_price=None, stop_trigger_price=None),
            quantity=10.0,
            duration=OrderDuration.DAY,
            status=OrderStatus.FILLED,
            alpaca_order_id=AlpacaOrderId("alp-filled"),
            alpaca_order_id_chain=(AlpacaOrderId("alp-filled"),),
            submission_timestamp=_T0,
            last_update_timestamp=_T0,
            filled_quantity=10.0,
            avg_fill_price=150.0,
            remaining_quantity=0.0,
            modification_count=0,
            originating_thesis_id=None,
            originating_pm_command_id=None,
            age_hours=1.0,
        )
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(pending_orders=(filled_order,))


# ---------------------------------------------------------------------------
# Validator: orphan-free brackets
# ---------------------------------------------------------------------------


class TestOrphanBracketValidator:
    def test_all_brackets_resolve_passes(self) -> None:
        snap = _make_snapshot()
        for brk in snap.brackets:
            assert snap.position_by_id(brk.position_id) is not None

    def test_orphan_bracket_raises(self) -> None:
        orphan = _make_bracket("BRK-ORPHAN", "POS-NONEXISTENT")
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(brackets=(_make_bracket("BRK-001", "POS-001"), orphan))


# ---------------------------------------------------------------------------
# Validator: position modification trail
# ---------------------------------------------------------------------------


class TestPositionModificationTrailValidator:
    def test_valid_trail_passes(self) -> None:
        snap = _make_snapshot()
        assert "POS-001" in snap.position_modification_trail

    def test_unresolvable_key_raises(self) -> None:
        entry = _make_activity_entry("ENTRY-X", _INV_ID, "POS-GHOST")
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(position_modification_trail={"POS-GHOST": (entry,)})


# ---------------------------------------------------------------------------
# Validator: intra_invocation_changelog scoping
# ---------------------------------------------------------------------------


class TestIntraInvocationChangelogValidator:
    def test_matching_invocation_id_passes(self) -> None:
        snap = _make_snapshot()
        assert all(e.invocation_id == _INV_ID for e in snap.intra_invocation_changelog)

    def test_mismatched_invocation_id_raises(self) -> None:
        wrong_entry = _make_activity_entry("ENTRY-WRONG", "inv-other-999", "POS-001")
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(intra_invocation_changelog=(wrong_entry,))


# ---------------------------------------------------------------------------
# Validator: timestamp ordering
# ---------------------------------------------------------------------------


class TestTimestampOrderingValidator:
    def test_phase1_before_assembled_passes(self) -> None:
        snap = _make_snapshot(phase1_committed_at=_T0, snapshot_assembled_at=_T1)
        assert snap.phase1_committed_at <= snap.snapshot_assembled_at

    def test_phase1_equal_assembled_passes(self) -> None:
        snap = _make_snapshot(phase1_committed_at=_T0, snapshot_assembled_at=_T0)
        assert snap.phase1_committed_at == snap.snapshot_assembled_at

    def test_phase1_after_assembled_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(phase1_committed_at=_T1, snapshot_assembled_at=_T0)

    def test_pipeline_start_after_assembled_passes(self) -> None:
        snap = _make_snapshot(
            snapshot_assembled_at=_T1,
            pipeline_invocation_started_at=_T2,
        )
        assert snap.pipeline_invocation_started_at is not None
        assert snap.pipeline_invocation_started_at >= snap.snapshot_assembled_at

    def test_pipeline_start_none_passes(self) -> None:
        snap = _make_snapshot(pipeline_invocation_started_at=None)
        assert snap.pipeline_invocation_started_at is None

    def test_pipeline_start_before_assembled_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(
                snapshot_assembled_at=_T1,
                pipeline_invocation_started_at=_T0,
            )


# ---------------------------------------------------------------------------
# Validator: PM decision log event type
# ---------------------------------------------------------------------------


class TestPMDecisionLogValidator:
    def test_pm_decision_entries_pass(self) -> None:
        snap = _make_snapshot()
        assert all(e.event_type == EventType.PM_DECISION for e in snap.recent_pm_decision_log)

    def test_non_pm_decision_entry_raises(self) -> None:
        non_pm = _make_activity_entry("ENTRY-NON-PM", _INV_ID, "POS-001")
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(recent_pm_decision_log=(non_pm,))


# ---------------------------------------------------------------------------
# Validator: position ID uniqueness
# ---------------------------------------------------------------------------


class TestPositionIdUniquenessValidator:
    def test_unique_ids_pass(self) -> None:
        snap = _make_snapshot()
        all_ids = [p.position_id for p in snap.open_positions] + [
            p.position_id for p in snap.pending_positions
        ]
        assert len(all_ids) == len(set(all_ids))

    def test_duplicate_position_id_open_pending_raises(self) -> None:
        pos_open = _make_open_position("POS-DUP")
        pos_pending = _make_pending_position("POS-DUP")
        brk = _make_bracket("BRK-DUP", "POS-DUP")
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(
                open_positions=(pos_open,),
                pending_positions=(pos_pending,),
                brackets=(brk,),
                position_modification_trail={},
                intra_invocation_changelog=(),
                pending_orders=(),
                active_theses=(),
            )


# ---------------------------------------------------------------------------
# Validator: bracket ID uniqueness
# ---------------------------------------------------------------------------


class TestBracketIdUniquenessValidator:
    def test_unique_bracket_ids_pass(self) -> None:
        snap = _make_snapshot()
        ids = [b.bracket_id for b in snap.brackets]
        assert len(ids) == len(set(ids))

    def test_duplicate_bracket_id_raises(self) -> None:
        brk1 = _make_bracket("BRK-DUP", "POS-001")
        brk2 = _make_bracket("BRK-DUP", "POS-002")
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(brackets=(brk1, brk2))


# ---------------------------------------------------------------------------
# Validator: invocation_id non-empty
# ---------------------------------------------------------------------------


class TestInvocationIdValidator:
    def test_non_empty_invocation_id_passes(self) -> None:
        new_inv = "any-non-empty"
        changelog = (_make_activity_entry("ENTRY-001", new_inv, "POS-001"),)
        pm_log = (_make_pm_decision_entry("ENTRY-PM-001", new_inv),)
        mod_trail = {"POS-001": (_make_activity_entry("ENTRY-MOD-001", new_inv, "POS-001"),)}
        snap = _make_snapshot(
            invocation_id=new_inv,
            intra_invocation_changelog=changelog,
            recent_pm_decision_log=pm_log,
            position_modification_trail=mod_trail,
        )
        assert snap.invocation_id == new_inv

    def test_empty_invocation_id_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(invocation_id="")


# ---------------------------------------------------------------------------
# Validator: tz-aware timestamps
# ---------------------------------------------------------------------------


class TestTimestampTzAwarenessValidator:
    def test_naive_phase1_committed_at_raises(self) -> None:
        naive = _T0.replace(tzinfo=None)
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(phase1_committed_at=naive)

    def test_naive_snapshot_assembled_at_raises(self) -> None:
        naive = _T1.replace(tzinfo=None)
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(
                phase1_committed_at=_T0,
                snapshot_assembled_at=naive,
            )

    def test_naive_pipeline_started_at_raises(self) -> None:
        naive = _T2.replace(tzinfo=None)
        with pytest.raises((ValueError, TypeError)):
            _make_snapshot(pipeline_invocation_started_at=naive)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_cannot_mutate_open_positions(self) -> None:
        snap = _make_snapshot()
        with pytest.raises((ValueError, TypeError, AttributeError)):
            snap.open_positions = ()  # type: ignore[misc]

    def test_cannot_mutate_invocation_id(self) -> None:
        snap = _make_snapshot()
        with pytest.raises((ValueError, TypeError, AttributeError)):
            snap.invocation_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Deep copy equality
# ---------------------------------------------------------------------------


class TestDeepCopyEquality:
    def test_deepcopy_equals_original(self) -> None:
        snap = _make_snapshot()
        assert copy.deepcopy(snap) == snap


# ---------------------------------------------------------------------------
# Empty portfolio
# ---------------------------------------------------------------------------


class TestEmptyPortfolio:
    def test_empty_portfolio_constructs_without_error(self) -> None:
        snap = PortfolioStateSnapshot(
            invocation_id="inv-empty-001",
            phase1_committed_at=_T0,
            snapshot_assembled_at=_T1,
            pipeline_invocation_started_at=None,
            open_positions=(),
            pending_positions=(),
            sector_exposure=(),
            directional_exposure=_make_directional_exposure(
                total_long_delta_adjusted_usd=0.0,
                total_short_delta_adjusted_usd=0.0,
                net_directional_pct_of_portfolio=0.0,
                gross_pct_of_portfolio=0.0,
            ),
            portfolio_pnl=_make_portfolio_pnl(),
            drawdown=_make_drawdown_state(),
            active_theses=(),
            recent_thesis_resolutions=(),
            cash_ledger=_make_cash_ledger(),
            pending_orders=(),
            risk_budget=_make_risk_budget(),
            active_risk_parameters=_make_active_risk_parameters(),
            intra_invocation_changelog=(),
            recent_pm_decision_log=(),
            position_modification_trail={},
            thesis_quality_aggregates=_make_thesis_quality(),
            brackets=(),
        )
        assert snap.open_positions == ()
        assert snap.pending_positions == ()
        assert snap.brackets == ()
        assert snap.position_modification_trail == {}
