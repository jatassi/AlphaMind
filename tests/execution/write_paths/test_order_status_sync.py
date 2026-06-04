"""Tests for the terminal-status event builders (ALP-739 / ALP-849 W1c).

``order_status_sync`` no longer RMWs ``orders.status`` — the monitor is no
longer a second writer on the shared ``orders`` row (ADR-0005 invariant 1). It
now derives the deterministic ``broker_event_log`` ``event_key`` and builds the
``TERMINAL_ORDER_STATUS`` event record carrying the resolved broker-carried link.
The append + no-RMW + attribution + zero-fill-gating integration behavior is
covered by ``tests/execution/continuous_monitor/fill_stream_consumer/`` against
the real DB; here we pin the pure builders.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from alphamind._kernel.ids import (
    AlpacaOrderId,
    ClientOrderId,
    InvocationId,
    PositionId,
    ThesisId,
)
from alphamind.execution.broker_adapter import FillReport
from alphamind.execution.broker_adapter.fill_stream import OrderStatus as FillEventStatus
from alphamind.execution.write_paths.order_status_sync import (
    derive_terminal_event_key,
    terminal_status_event_record,
)
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.state.records_broker_event_log import BrokerEventType

_FILL_TS = datetime(2026, 5, 28, 20, 30, tzinfo=UTC)


def _report(
    *, alpaca_order_id: str = "uuid-1", event_type: FillEventStatus = "canceled"
) -> FillReport:
    return FillReport(
        client_order_id=ClientOrderId("inv-x.ENV-1.0.0"),
        alpaca_order_id=AlpacaOrderId(alpaca_order_id),
        parent_client_order_id=None,
        parent_alpaca_order_id=None,
        event_type=event_type,
        fill_timestamp=_FILL_TS,
        fill_price=None,
        fill_quantity=None,
        cumulative_filled_quantity=0.0,
        remaining_quantity=1.0,
        execution_venue=None,
        occ_symbol=None,
        position_intent=None,
        raw_event_payload={"order": {"status": "canceled"}},
    )


def test_event_key_is_deterministic_and_terminal_prefixed() -> None:
    report = _report(alpaca_order_id="uuid-A")
    key = derive_terminal_event_key(report, OrderStatus.CANCELLED)
    assert key == derive_terminal_event_key(report, OrderStatus.CANCELLED)
    assert key.startswith("tevt-")


def test_event_key_distinguishes_order_and_terminal_status() -> None:
    # Two different orders → distinct keys; same order under different terminal
    # dispositions → distinct keys (the idempotency identity is the pair).
    a = derive_terminal_event_key(_report(alpaca_order_id="uuid-A"), OrderStatus.CANCELLED)
    b = derive_terminal_event_key(_report(alpaca_order_id="uuid-B"), OrderStatus.CANCELLED)
    c = derive_terminal_event_key(_report(alpaca_order_id="uuid-A"), OrderStatus.EXPIRED)
    assert len({a, b, c}) == 3


def test_record_carries_link_and_terminal_status_in_payload() -> None:
    report = _report(alpaca_order_id="uuid-A", event_type="expired")
    record = terminal_status_event_record(
        report,
        OrderStatus.EXPIRED,
        thesis_id=ThesisId("the-1"),
        invocation_id=InvocationId("inv-1"),
        position_id=PositionId("pos-1"),
    )
    assert record.event_type is BrokerEventType.TERMINAL_ORDER_STATUS
    assert record.event_key == derive_terminal_event_key(report, OrderStatus.EXPIRED)
    assert record.thesis_id == "the-1"
    assert record.invocation_id == "inv-1"
    assert record.position_id == "pos-1"
    assert record.broker_timestamp == _FILL_TS
    # The terminal disposition the order reached is recorded for the projection
    # rebuild (04a) to derive orders.status from the log.
    payload = json.loads(record.raw_payload_json)
    assert payload["terminal_status"] == "EXPIRED"


def test_record_link_is_nullable() -> None:
    # An OCO sibling-cancel whose order edge is not position-linked carries a
    # NULL link on the initial insert (append-only, never enriched).
    record = terminal_status_event_record(
        _report(),
        OrderStatus.CANCELLED,
        thesis_id=None,
        invocation_id=None,
        position_id=None,
    )
    assert record.thesis_id is None
    assert record.invocation_id is None
    assert record.position_id is None
