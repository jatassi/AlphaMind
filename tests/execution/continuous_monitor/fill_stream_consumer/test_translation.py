"""Tests for ``fill_report_to_fill_record`` (story ALP-435 / 02c).

The translator converts an OMS-facing :class:`FillReport` (from
``alphamind.execution.broker_adapter``) into the persistence-layer
:class:`FillRecord` shape that ``append_fill_record`` expects. Tests live
here rather than alongside the broker adapter because the translation is the
continuous monitor's responsibility: the adapter ships the raw event projection
and the monitor decides which projections to durably persist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.execution.broker_adapter import FillReport
from alphamind.execution.continuous_monitor.fill_stream_consumer import (
    fill_report_to_fill_record,
)
from alphamind.execution.state_persistence.write_paths.records import (
    FillProcessingStatus,
)
from alphamind.portfolio_state.records.orders import OrderStatus


def _ts(minute: int = 0) -> datetime:
    return datetime(2026, 5, 9, 14, minute, tzinfo=UTC)


def _equity_fill_report(
    *,
    client_order_id: str = "oms-order-1",
    alpaca_order_id: str = "alp-1",
    event_type: Any = "filled",
    fill_price: float | None = 189.42,
    fill_quantity: float | None = 100.0,
    cumulative: float = 100.0,
    remaining: float = 0.0,
    occ_symbol: str | None = None,
    parent_client_order_id: str | None = None,
    parent_alpaca_order_id: str | None = None,
    timestamp: datetime | None = None,
) -> FillReport:
    return FillReport(
        client_order_id=client_order_id,
        alpaca_order_id=alpaca_order_id,
        parent_client_order_id=parent_client_order_id,
        parent_alpaca_order_id=parent_alpaca_order_id,
        event_type=event_type,
        fill_timestamp=timestamp or _ts(30),
        fill_price=fill_price,
        fill_quantity=fill_quantity,
        cumulative_filled_quantity=cumulative,
        remaining_quantity=remaining,
        execution_venue=None,
        occ_symbol=occ_symbol,
        position_intent=None,
        raw_event_payload={},
    )


class TestEquityFillEvent:
    def test_filled_event_translates_to_fill_record(self) -> None:
        report = _equity_fill_report(
            client_order_id="oms-order-1",
            alpaca_order_id="alp-abc",
            event_type="filled",
            fill_price=189.42,
            fill_quantity=100.0,
        )

        record = fill_report_to_fill_record(report)

        assert record is not None
        assert record.order_id == "oms-order-1"
        assert record.fill_timestamp == report.fill_timestamp
        assert record.fill_price == pytest.approx(189.42)
        assert record.fill_quantity == pytest.approx(100.0)
        assert record.remaining_quantity_after == pytest.approx(0.0)
        assert record.order_status_after is OrderStatus.FILLED
        assert record.gateway_reference == "alp-abc"
        assert record.processing_status is FillProcessingStatus.UNPROCESSED
        assert record.processing_invocation_id is None
        assert record.processing_timestamp is None
        assert record.regt_attribution is None
        assert record.live_execution_estimate is None


class TestNonFillEvents:
    @pytest.mark.parametrize(
        "event_type",
        [
            "new",
            "canceled",
            "expired",
            "replaced",
            "replace_rejected",
            "rejected",
            "done_for_day",
        ],
    )
    def test_non_fill_event_returns_none(self, event_type: str) -> None:
        report = _equity_fill_report(event_type=event_type, fill_price=None, fill_quantity=None)
        assert fill_report_to_fill_record(report) is None


class TestPartialFillEvent:
    def test_partial_fill_translates_with_partially_filled_status(self) -> None:
        report = _equity_fill_report(
            event_type="partially_filled",
            fill_price=99.5,
            fill_quantity=40.0,
            cumulative=40.0,
            remaining=60.0,
        )

        record = fill_report_to_fill_record(report)

        assert record is not None
        assert record.order_status_after is OrderStatus.PARTIALLY_FILLED
        assert record.fill_quantity == pytest.approx(40.0)
        assert record.remaining_quantity_after == pytest.approx(60.0)


class TestSingleLegOptionsEvent:
    def test_options_partial_fill_preserves_occ_symbol(self) -> None:
        occ = "AAPL250620C00200000"
        report = _equity_fill_report(
            event_type="partially_filled",
            fill_price=3.45,
            fill_quantity=2.0,
            cumulative=2.0,
            remaining=3.0,
            occ_symbol=occ,
        )

        record = fill_report_to_fill_record(report)

        # The persistence row carries no OCC field — the OCC lives on the
        # parent order's instrument_spec, which Phase 1 resolves at
        # integration time. The translator's only obligation for single-leg
        # options is to produce a fill record with a populated price + qty;
        # the broker-adapter's projection of ``occ_symbol`` does not need to
        # round-trip into the persistence row.
        assert record is not None
        assert record.order_status_after is OrderStatus.PARTIALLY_FILLED
        assert record.fill_price == pytest.approx(3.45)
        assert record.fill_quantity == pytest.approx(2.0)


class TestMlegEvents:
    def _mleg_parent(self, **overrides: Any) -> FillReport:
        # Parent reports leave ``position_intent`` / ``occ_symbol`` unset and
        # carry the mleg ``order.legs`` list on ``raw_event_payload``. Mirror
        # the broker-adapter's translator output shape.
        defaults: dict[str, Any] = {
            "client_order_id": "strategy-1",
            "alpaca_order_id": "alp-parent",
            "parent_client_order_id": None,
            "parent_alpaca_order_id": None,
            "event_type": "filled",
            "fill_timestamp": _ts(30),
            "fill_price": 2.10,
            "fill_quantity": 1.0,
            "cumulative_filled_quantity": 1.0,
            "remaining_quantity": 0.0,
            "execution_venue": None,
            "occ_symbol": None,
            "position_intent": None,
            "raw_event_payload": {"order": {"legs": [{"id": "leg-1"}, {"id": "leg-2"}]}},
        }
        defaults.update(overrides)
        return FillReport(**defaults)

    def _mleg_leg_child(self, **overrides: Any) -> FillReport:
        defaults: dict[str, Any] = {
            "client_order_id": "strategy-1-leg-1",
            "alpaca_order_id": "alp-leg-1",
            "parent_client_order_id": "strategy-1",
            "parent_alpaca_order_id": "alp-parent",
            "event_type": "filled",
            "fill_timestamp": _ts(30),
            "fill_price": 2.10,
            "fill_quantity": 1.0,
            "cumulative_filled_quantity": 1.0,
            "remaining_quantity": 0.0,
            "execution_venue": None,
            "occ_symbol": "AAPL250620C00200000",
            "position_intent": "buy_to_open",
            "raw_event_payload": {},
        }
        defaults.update(overrides)
        return FillReport(**defaults)

    def test_mleg_parent_returns_none(self) -> None:
        assert fill_report_to_fill_record(self._mleg_parent()) is None

    def test_recovery_shape_mleg_parent_returns_none(self) -> None:
        """Recovery-path raw payloads carry ``legs`` at top level (not nested).

        ``recovery.order_snapshot_to_fill_reports`` dumps the ``OrderSnapshot``
        whose schema has ``legs`` as a top-level field, while live stream
        dumps the alpaca-py ``TradeUpdate`` whose ``order.legs`` is nested.
        Both shapes must be recognized as an mleg parent.
        """
        recovery_parent = self._mleg_parent(
            raw_event_payload={"legs": [{"order_id": "leg-1"}, {"order_id": "leg-2"}]},
        )
        assert fill_report_to_fill_record(recovery_parent) is None

    def test_mleg_leg_child_translates_to_fill_record_addressing_parent(self) -> None:
        """Per the story-spec mapping table: mleg per-leg children resolve
        their persistence ``order_id`` to the parent strategy's
        ``client_order_id``.

        The OMS persists mleg orders as a single :class:`OrderRecord` with
        ``order_class=MLEG`` and the legs encoded on ``instrument_spec`` —
        the legs themselves do not own rows in the ``orders`` table, so the
        fill_records FK ``order_id`` must point at the parent strategy.
        """
        record = fill_report_to_fill_record(
            self._mleg_leg_child(
                client_order_id="strategy-1-leg-1",
                alpaca_order_id="alp-leg-1",
                parent_client_order_id="strategy-1",
                parent_alpaca_order_id="alp-parent",
            )
        )

        assert record is not None
        assert record.order_id == "strategy-1"
        # ``gateway_reference`` carries the alpaca-side leg id so EOD
        # reconciliation can resolve back to the exact execution.
        assert record.gateway_reference == "alp-leg-1"
        assert record.order_status_after is OrderStatus.FILLED


class TestIdempotency:
    def test_same_report_produces_same_fill_id(self) -> None:
        """The dedupe-driven ``fill_id`` derivation must be deterministic so
        a fill delivered twice (live stream + recovery overlap) maps to the
        same primary-key row. ``append_fill_record`` separately enforces
        idempotency on the natural-key UNIQUE constraint, but matching
        ``fill_id`` keeps the activity-log audit trail clean.
        """
        report_a = _equity_fill_report()
        report_b = _equity_fill_report()
        rec_a = fill_report_to_fill_record(report_a)
        rec_b = fill_report_to_fill_record(report_b)
        assert rec_a is not None and rec_b is not None
        assert rec_a.fill_id == rec_b.fill_id
