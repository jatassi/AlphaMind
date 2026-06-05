"""Pure-classification tests for the account-activities lifecycle parser.

The classifier is the functional core of the poll: it takes the broker's flat
``ActivitySnapshot`` stream and returns typed, paired lifecycle events ready for
the handlers — an OTM expiry (``OPEXP``) stands alone; an assignment / exercise
(``OPASN`` / ``OPEXC``) is paired with the ``OPTRD`` that prices its equity leg.
No I/O, no DB, no broker — just data in, data out.
"""

from __future__ import annotations

import datetime as dt

from alphamind._kernel.money import price, signed_money
from alphamind.execution.account_activities.classify import classify_lifecycle_activities
from alphamind.execution.account_activities.records import (
    LifecycleActivityType,
)
from alphamind.execution.broker_adapter.queries import ActivitySnapshot

_TXN = dt.datetime(2026, 9, 18, 20, 0, 0, tzinfo=dt.UTC)


def _snapshot(
    *,
    activity_id: str,
    activity_type: str,
    symbol: str,
    qty: float | None = None,
    px: float | None = None,
    side: str | None = None,
    net_amount: float | None = None,
) -> ActivitySnapshot:
    return ActivitySnapshot(
        id=activity_id,
        activity_type=activity_type,
        transaction_time=_TXN,
        symbol=symbol,
        qty=qty,
        price=price(px) if px is not None else None,
        net_amount=signed_money(net_amount) if net_amount is not None else None,
        side=side,
        description=None,
        raw={"id": activity_id, "activity_type": activity_type, "symbol": symbol},
    )


def test_otm_expiry_classifies_as_standalone_expiry() -> None:
    """An ``OPEXP`` activity becomes one expiry lifecycle event with no pair."""
    snap = _snapshot(
        activity_id="act-exp-1",
        activity_type="OPEXP",
        symbol="AAPL250918C00150000",
        qty=5.0,
    )

    events = classify_lifecycle_activities((snap,))

    assert len(events) == 1
    event = events[0]
    assert event.activity_type is LifecycleActivityType.OPEXP
    assert event.occ_symbol == "AAPL250918C00150000"
    assert event.paired_trade is None


def test_assignment_pairs_with_optrd_on_same_underlying() -> None:
    """An ``OPASN`` is paired with the ``OPTRD`` that prices its equity leg."""
    assignment = _snapshot(
        activity_id="act-asn-1",
        activity_type="OPASN",
        symbol="AAPL250918C00150000",
        qty=5.0,
    )
    trade = _snapshot(
        activity_id="act-trd-1",
        activity_type="OPTRD",
        symbol="AAPL",
        qty=500.0,
        px=150.0,
        side="buy",
        net_amount=-75_000.0,
    )

    events = classify_lifecycle_activities((assignment, trade))

    # The OPTRD is consumed as the pair, not surfaced as its own event.
    assert len(events) == 1
    event = events[0]
    assert event.activity_type is LifecycleActivityType.OPASN
    assert event.paired_trade is not None
    assert event.paired_trade.equity_symbol == "AAPL"
    assert event.paired_trade.qty == 500.0
    assert event.paired_trade.strike_price == price(150.0)
    assert event.paired_trade.side == "buy"


def test_non_lifecycle_activities_are_dropped() -> None:
    """A ``FILL`` (or any non-lifecycle type) never becomes a lifecycle event."""
    fill = _snapshot(
        activity_id="act-fill-1",
        activity_type="FILL",
        symbol="AAPL",
        qty=100.0,
        px=149.0,
        side="buy",
    )

    assert classify_lifecycle_activities((fill,)) == ()
