"""Tests for ``alphamind.execution.broker_adapter.recovery`` (story ALP-389).

Story 03d — disconnect-recovery primitive that calls
``AccountStateQueries.get_orders(status="all", since=ts)`` and translates each
yielded ``OrderSnapshot`` into one or more ``FillReport`` records the OMS
Phase 1 path can integrate alongside live websocket fills.

Tests use a synthetic ``AccountStateQueries`` substitute that yields canned
``OrderSnapshot`` records so we exercise the translation + ordering surface
without the alpaca-py / network round-trip.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from alphamind._kernel.ids import OrderId
from alphamind._kernel.money import price
from alphamind.execution.broker_adapter import (
    FillReport,
    OrderLegSnapshot,
    OrderSnapshot,
    recover_missed_fills_since,
)

# ---------------------------------------------------------------------------
# Helpers — synthetic OrderSnapshot records + fake AccountStateQueries
# ---------------------------------------------------------------------------


def _ts(minute: int = 0) -> datetime:
    """Build a tz-aware UTC datetime parameterized by the minute field."""
    return datetime(2026, 5, 9, 14, minute, tzinfo=UTC)


def _build_order_snapshot(
    *,
    order_id: str = "alp-1",
    client_order_id: str = "inv-snapshot-1",
    symbol: str = "AAPL",
    asset_class: str = "us_equity",
    qty: float = 100.0,
    filled_qty: float = 100.0,
    side: str = "buy",
    order_type: str = "market",
    time_in_force: str = "day",
    order_class: str = "simple",
    status: str = "filled",
    submitted_at: datetime | None = None,
    filled_at: datetime | None = None,
    canceled_at: datetime | None = None,
    expired_at: datetime | None = None,
    filled_avg_price: float | None = None,
    replaced_by: str | None = None,
    replaces: str | None = None,
    legs: Sequence[OrderLegSnapshot] | None = None,
) -> OrderSnapshot:
    """Build an ``OrderSnapshot`` with sensible defaults for tests."""
    return OrderSnapshot(
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        asset_class=asset_class,
        qty=qty,
        filled_qty=filled_qty,
        side=side,
        order_type=order_type,
        time_in_force=time_in_force,
        order_class=order_class,
        status=status,
        submitted_at=submitted_at or _ts(0),
        filled_at=filled_at,
        canceled_at=canceled_at,
        expired_at=expired_at,
        filled_avg_price=None if filled_avg_price is None else price(filled_avg_price),
        replaced_by=replaced_by,
        replaces=replaces,
        legs=tuple(legs) if legs is not None else None,
    )


class _FakeQueries:
    """Async-iterable substitute for ``AccountStateQueries.get_orders``.

    Mirrors the recovery module's ``_OrdersSource`` protocol so the test
    suite can pass it without subclassing the network-touching real class.
    Captures each call's kwargs so tests can assert on them.
    """

    def __init__(self, snapshots: Sequence[OrderSnapshot]) -> None:
        self._snapshots = list(snapshots)
        self.calls: list[dict[str, Any]] = []

    async def get_orders(
        self,
        *,
        status: Literal["open", "closed", "all"] = "all",
        since: datetime | None = None,
        until: datetime | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> AsyncIterator[OrderSnapshot]:
        self.calls.append({"status": status, "since": since, "until": until, "symbols": symbols})
        for snap in self._snapshots:
            yield snap


async def _collect(gen: AsyncIterator[FillReport]) -> list[FillReport]:
    out: list[FillReport] = []
    async for r in gen:
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# 1. Tracer — filled OrderSnapshot → single FillReport
# ---------------------------------------------------------------------------


class TestFilledOrder:
    async def test_yields_filled_fill_report(self) -> None:
        snap = _build_order_snapshot(
            client_order_id="inv-001",
            order_id=OrderId("alpaca-001"),
            qty=100.0,
            filled_qty=100.0,
            filled_avg_price=189.42,
            status="filled",
            filled_at=_ts(30),
        )
        queries = _FakeQueries([snap])

        reports = await _collect(recover_missed_fills_since(queries, since=_ts(0)))

        assert len(reports) == 1
        (report,) = reports
        assert report.event_type == "filled"
        assert report.client_order_id == "inv-001"
        assert report.alpaca_order_id == "alpaca-001"
        assert report.fill_price == pytest.approx(189.42)
        assert report.fill_quantity == pytest.approx(100.0)
        assert report.cumulative_filled_quantity == pytest.approx(100.0)
        assert report.remaining_quantity == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 2. Terminal status translation (canceled / expired / rejected / replaced)
# ---------------------------------------------------------------------------


class TestTerminalStatuses:
    async def test_canceled_yields_null_fill_fields(self) -> None:
        snap = _build_order_snapshot(
            qty=50.0,
            filled_qty=0.0,
            status="canceled",
            canceled_at=_ts(15),
            filled_at=None,
        )

        (report,) = await _collect(recover_missed_fills_since(_FakeQueries([snap]), since=_ts(0)))

        assert report.event_type == "canceled"
        assert report.fill_price is None
        assert report.fill_quantity is None

    async def test_expired_with_partial_fill_yields_one_expired_report(self) -> None:
        """An expired DAY order may have a partial fill; the recovery routine
        emits ONE FillReport with event_type=expired carrying the partial fill
        quantity reflected on cumulative_filled_quantity. The parallel
        partial-fill increment is NOT re-emitted — Alpaca's REST surface
        cannot reconstruct intermediate fills, and the OMS dedups via the
        fill_records ledger.
        """
        snap = _build_order_snapshot(
            qty=100.0,
            filled_qty=40.0,
            filled_avg_price=189.42,
            status="expired",
            expired_at=_ts(45),
            filled_at=None,
        )

        reports = await _collect(recover_missed_fills_since(_FakeQueries([snap]), since=_ts(0)))

        assert len(reports) == 1
        (report,) = reports
        assert report.event_type == "expired"
        assert report.fill_price is None
        assert report.fill_quantity is None
        assert report.cumulative_filled_quantity == pytest.approx(40.0)
        assert report.remaining_quantity == pytest.approx(60.0)

    async def test_rejected_yields_rejected_report(self) -> None:
        snap = _build_order_snapshot(
            status="rejected",
            qty=10.0,
            filled_qty=0.0,
            filled_at=None,
        )

        (report,) = await _collect(recover_missed_fills_since(_FakeQueries([snap]), since=_ts(0)))

        assert report.event_type == "rejected"
        assert report.fill_price is None
        assert report.fill_quantity is None

    async def test_replaced_uses_replaced_by_as_alpaca_order_id(self) -> None:
        snap = _build_order_snapshot(
            order_id=OrderId("alp-original"),
            replaced_by="alp-replacement",
            status="replaced",
            qty=10.0,
            filled_qty=0.0,
            filled_at=None,
        )

        (report,) = await _collect(recover_missed_fills_since(_FakeQueries([snap]), since=_ts(0)))

        assert report.event_type == "replaced"
        assert report.alpaca_order_id == "alp-replacement"

    async def test_partially_filled_yields_partially_filled_report(self) -> None:
        snap = _build_order_snapshot(
            qty=100.0,
            filled_qty=40.0,
            filled_avg_price=189.42,
            status="partially_filled",
            filled_at=_ts(10),
        )

        (report,) = await _collect(recover_missed_fills_since(_FakeQueries([snap]), since=_ts(0)))

        assert report.event_type == "partially_filled"
        assert report.fill_price == pytest.approx(189.42)
        assert report.fill_quantity == pytest.approx(40.0)
        assert report.cumulative_filled_quantity == pytest.approx(40.0)
        assert report.remaining_quantity == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# 3. Open-state translation
# ---------------------------------------------------------------------------


class TestOpenStatuses:
    @pytest.mark.parametrize("status", ["new", "accepted", "pending_new"])
    async def test_open_states_yield_new_event(self, status: str) -> None:
        snap = _build_order_snapshot(
            qty=100.0,
            filled_qty=0.0,
            status=status,
            filled_at=None,
        )

        (report,) = await _collect(recover_missed_fills_since(_FakeQueries([snap]), since=_ts(0)))

        assert report.event_type == "new"
        assert report.fill_price is None
        assert report.fill_quantity is None
        assert report.cumulative_filled_quantity == pytest.approx(0.0)
        assert report.remaining_quantity == pytest.approx(100.0)

    @pytest.mark.parametrize(
        "filtered",
        ["pending_cancel", "pending_replace", "pending_review", "held", "accepted_for_bidding"],
    )
    async def test_filtered_in_flight_states_yield_nothing(self, filtered: str) -> None:
        snap = _build_order_snapshot(status=filtered, filled_at=None)

        reports = await _collect(recover_missed_fills_since(_FakeQueries([snap]), since=_ts(0)))

        assert reports == []


# ---------------------------------------------------------------------------
# 4. Mleg parent + per-leg correlation
# ---------------------------------------------------------------------------


def _build_mleg_leg(
    *,
    order_id: str,
    occ_symbol: str,
    qty: float = 1.0,
    filled_qty: float = 1.0,
    side: str = "buy",
    position_intent: str = "buy_to_open",
    status: str = "filled",
    filled_avg_price: float | None = None,
) -> OrderLegSnapshot:
    return OrderLegSnapshot(
        order_id=order_id,
        symbol=occ_symbol,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=None if filled_avg_price is None else price(filled_avg_price),
        side=side,
        position_intent=position_intent,
        status=status,
    )


class TestMlegOrders:
    def _three_filled_legs(self) -> list[OrderLegSnapshot]:
        return [
            _build_mleg_leg(
                order_id=OrderId("leg-1-id"),
                occ_symbol="AAPL250620C00200000",
                position_intent="buy_to_open",
                side="buy",
                filled_avg_price=2.10,
            ),
            _build_mleg_leg(
                order_id=OrderId("leg-2-id"),
                occ_symbol="AAPL250620C00210000",
                position_intent="sell_to_open",
                side="sell",
                filled_avg_price=1.05,
            ),
            _build_mleg_leg(
                order_id=OrderId("leg-3-id"),
                occ_symbol="AAPL250620P00190000",
                position_intent="sell_to_open",
                side="sell",
                filled_avg_price=0.95,
            ),
        ]

    async def test_filled_mleg_yields_parent_plus_per_leg_children(self) -> None:
        legs = self._three_filled_legs()
        parent = _build_order_snapshot(
            client_order_id="strategy-1",
            order_id=OrderId("parent-id"),
            symbol="",
            asset_class="us_option",
            order_class="mleg",
            qty=1.0,
            filled_qty=1.0,
            status="filled",
            filled_at=_ts(20),
            legs=legs,
        )

        reports = await _collect(recover_missed_fills_since(_FakeQueries([parent]), since=_ts(0)))

        # 1 parent + 3 leg children = 4 reports
        assert len(reports) == 4
        parent_report, leg1, leg2, leg3 = reports

        assert parent_report.client_order_id == "strategy-1"
        assert parent_report.alpaca_order_id == "parent-id"
        assert parent_report.parent_client_order_id is None
        assert parent_report.parent_alpaca_order_id is None
        assert parent_report.occ_symbol is None
        assert parent_report.position_intent is None
        assert parent_report.event_type == "filled"

        for child in (leg1, leg2, leg3):
            assert child.parent_client_order_id == "strategy-1"
            assert child.parent_alpaca_order_id == "parent-id"
            assert child.occ_symbol is not None
            assert child.position_intent is not None
            assert child.event_type == "filled"

        assert leg1.occ_symbol == "AAPL250620C00200000"
        assert leg1.position_intent == "buy_to_open"
        assert leg2.position_intent == "sell_to_open"
        assert leg3.position_intent == "sell_to_open"

    async def test_mleg_with_mixed_leg_statuses_carries_per_leg_status(self) -> None:
        """Under thin liquidity Alpaca may report 3 of 4 legs filled and 1
        still open; each leg's report carries that leg's individual status."""
        legs = [
            _build_mleg_leg(
                order_id=OrderId("leg-1-id"),
                occ_symbol="AAPL250620C00200000",
                position_intent="buy_to_open",
                side="buy",
                status="filled",
                filled_avg_price=2.10,
                filled_qty=1.0,
                qty=1.0,
            ),
            _build_mleg_leg(
                order_id=OrderId("leg-2-id"),
                occ_symbol="AAPL250620C00210000",
                position_intent="sell_to_open",
                side="sell",
                status="new",
                filled_qty=0.0,
                qty=1.0,
            ),
        ]
        parent = _build_order_snapshot(
            client_order_id="strategy-mixed",
            order_id=OrderId("parent-id"),
            symbol="",
            asset_class="us_option",
            order_class="mleg",
            qty=1.0,
            filled_qty=0.5,
            status="partially_filled",
            filled_at=_ts(20),
            legs=legs,
        )

        parent_report, leg1_report, leg2_report = await _collect(
            recover_missed_fills_since(_FakeQueries([parent]), since=_ts(0))
        )

        assert parent_report.event_type == "partially_filled"
        assert leg1_report.event_type == "filled"
        assert leg2_report.event_type == "new"
        # The still-open leg carries no fill metrics
        assert leg2_report.fill_price is None
        assert leg2_report.fill_quantity is None


# ---------------------------------------------------------------------------
# 5. Multi-order ordering — fill_timestamp ascending, order_id tiebreak
# ---------------------------------------------------------------------------


class TestOrdering:
    async def test_reports_yielded_in_fill_timestamp_ascending_order(self) -> None:
        """Multiple orders touched in the disconnect window must surface in
        fill_timestamp ascending order even when AccountStateQueries hands them
        back in arbitrary cursor order."""
        late = _build_order_snapshot(
            client_order_id="inv-late",
            order_id=OrderId("alp-late"),
            qty=1.0,
            filled_qty=1.0,
            filled_avg_price=10.0,
            status="filled",
            filled_at=_ts(45),
        )
        early = _build_order_snapshot(
            client_order_id="inv-early",
            order_id=OrderId("alp-early"),
            qty=1.0,
            filled_qty=1.0,
            filled_avg_price=10.0,
            status="filled",
            filled_at=_ts(15),
        )
        middle = _build_order_snapshot(
            client_order_id="inv-mid",
            order_id=OrderId("alp-mid"),
            qty=1.0,
            filled_qty=0.0,
            status="canceled",
            canceled_at=_ts(30),
            filled_at=None,
        )
        # Yielded in jumbled order to confirm sorting happens here, not at the SDK
        queries = _FakeQueries([late, early, middle])

        reports = await _collect(recover_missed_fills_since(queries, since=_ts(0)))

        assert [r.client_order_id for r in reports] == ["inv-early", "inv-mid", "inv-late"]

    async def test_ties_broken_by_alpaca_order_id(self) -> None:
        """When two orders share a fill_timestamp, the alpaca_order_id (string
        ordering) breaks the tie deterministically."""
        same_ts = _ts(20)
        snap_b = _build_order_snapshot(
            client_order_id="inv-b",
            order_id=OrderId("alp-b"),
            qty=1.0,
            filled_qty=1.0,
            filled_avg_price=10.0,
            status="filled",
            filled_at=same_ts,
        )
        snap_a = _build_order_snapshot(
            client_order_id="inv-a",
            order_id=OrderId("alp-a"),
            qty=1.0,
            filled_qty=1.0,
            filled_avg_price=10.0,
            status="filled",
            filled_at=same_ts,
        )
        queries = _FakeQueries([snap_b, snap_a])

        reports = await _collect(recover_missed_fills_since(queries, since=_ts(0)))

        assert [r.alpaca_order_id for r in reports] == ["alp-a", "alp-b"]

    async def test_falls_back_to_submitted_at_when_no_terminal_timestamp(self) -> None:
        """An in-flight ``new`` order has no terminal timestamp; the routine
        uses ``submitted_at`` as the ordering key."""
        early_submitted = _build_order_snapshot(
            client_order_id="inv-early",
            order_id=OrderId("alp-early"),
            qty=1.0,
            filled_qty=0.0,
            status="new",
            submitted_at=_ts(5),
            filled_at=None,
        )
        late_submitted = _build_order_snapshot(
            client_order_id="inv-late",
            order_id=OrderId("alp-late"),
            qty=1.0,
            filled_qty=0.0,
            status="new",
            submitted_at=_ts(50),
            filled_at=None,
        )
        queries = _FakeQueries([late_submitted, early_submitted])

        reports = await _collect(recover_missed_fills_since(queries, since=_ts(0)))

        assert [r.client_order_id for r in reports] == ["inv-early", "inv-late"]


# ---------------------------------------------------------------------------
# 6. since / until parameter handling
# ---------------------------------------------------------------------------


class TestSinceUntilParameters:
    async def test_passes_since_to_get_orders(self) -> None:
        queries = _FakeQueries([])
        since = _ts(10)

        await _collect(recover_missed_fills_since(queries, since=since))

        assert len(queries.calls) == 1
        call = queries.calls[0]
        assert call["since"] == since
        assert call["status"] == "all"

    async def test_passes_until_when_supplied(self) -> None:
        queries = _FakeQueries([])
        since = _ts(10)
        until = _ts(40)

        await _collect(recover_missed_fills_since(queries, since=since, until=until))

        assert queries.calls[0]["until"] == until

    async def test_until_defaults_to_now_utc_when_omitted(self) -> None:
        queries = _FakeQueries([])
        before = datetime.now(UTC)

        await _collect(recover_missed_fills_since(queries, since=_ts(0)))

        after = datetime.now(UTC)
        until_used = queries.calls[0]["until"]
        assert isinstance(until_used, datetime)
        assert until_used.tzinfo is not None
        assert before <= until_used <= after

    async def test_excludes_orders_submitted_after_until(self) -> None:
        """A defensive guard: even if the SDK leaks an order whose submitted_at
        exceeds the explicit until cutoff, the recovery routine filters it
        before yielding."""
        in_window = _build_order_snapshot(
            client_order_id="inv-in",
            order_id=OrderId("alp-in"),
            qty=1.0,
            filled_qty=1.0,
            filled_avg_price=10.0,
            status="filled",
            submitted_at=_ts(20),
            filled_at=_ts(25),
        )
        leaked = _build_order_snapshot(
            client_order_id="inv-leaked",
            order_id=OrderId("alp-leaked"),
            qty=1.0,
            filled_qty=1.0,
            filled_avg_price=10.0,
            status="filled",
            submitted_at=_ts(50),  # after until=40
            filled_at=_ts(55),
        )
        queries = _FakeQueries([in_window, leaked])

        reports = await _collect(recover_missed_fills_since(queries, since=_ts(0), until=_ts(40)))

        assert [r.client_order_id for r in reports] == ["inv-in"]

    def test_since_is_required_keyword_argument(self) -> None:
        """Calling without ``since`` must raise ``TypeError`` so callers
        cannot accidentally fetch the entire order history."""
        queries = _FakeQueries([])
        with pytest.raises(TypeError, match="since"):
            recover_missed_fills_since(queries)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 7. Idempotency-friendly emission
# ---------------------------------------------------------------------------


class TestIdempotencyFriendlyEmission:
    async def test_emits_all_window_events_without_dedup(self) -> None:
        """The recovery routine does not dedup against the OMS's prior state;
        the OMS Phase 1 path is idempotent on (client_order_id, event_type)
        via its fill_records ledger. So even an order whose ``new`` event
        was already processed via the websocket re-emits during recovery."""
        snap_one = _build_order_snapshot(
            client_order_id="inv-1",
            order_id=OrderId("alp-1"),
            status="new",
            qty=1.0,
            filled_qty=0.0,
            submitted_at=_ts(10),
            filled_at=None,
        )
        snap_two = _build_order_snapshot(
            client_order_id="inv-2",
            order_id=OrderId("alp-2"),
            status="filled",
            qty=1.0,
            filled_qty=1.0,
            filled_avg_price=10.0,
            submitted_at=_ts(20),
            filled_at=_ts(25),
        )
        queries = _FakeQueries([snap_one, snap_two])

        reports = await _collect(recover_missed_fills_since(queries, since=_ts(0)))

        assert {r.event_type for r in reports} == {"new", "filled"}
        assert len(reports) == 2
