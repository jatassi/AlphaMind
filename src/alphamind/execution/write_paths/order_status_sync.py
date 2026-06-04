"""Terminal non-fill order-status → broker-event log (ALP-739 / ALP-849 W1c).

The fill-stream consumer observes broker ``canceled`` / ``expired`` trade-update
events, but :func:`fill_report_to_fill_record` drops them — they append no fill.
Their terminal disposition still has to reach local state, or an accepted entry
that expires / cancels unfilled stays ``PENDING`` (the ZS symptom in ALP-739) and
the ``entry_no_fill`` alert never sees it.

**This used to be an in-place read-modify-write on ``orders.status`` by the
continuous monitor** — a second writer on a shared mutable row, the named
violation of single-writer-by-construction (ADR-0005, invariant 1). It is now an
**append** to the append-only ``broker_event_log`` (ADR-0002 W1c): a zero-fill
terminal event lands as one immutable ``TERMINAL_ORDER_STATUS`` row, idempotent
on the ``event_key`` PK. The ``orders.status`` row becomes a projection derived
from the log, written only by the single (pipeline) writer — the monitor no
longer RMWs it.

This module owns the deterministic ``event_key`` derivation and the record
builder; the actual insert goes through the canonical idempotent helper
:func:`alphamind.execution.write_paths.broker_event_persistence.append_broker_event`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from alphamind._kernel.ids import InvocationId, PositionId, ThesisId
from alphamind.execution.broker_adapter import FillReport
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.state.records_broker_event_log import (
    BrokerEventRecord,
    BrokerEventType,
    serialize_event_payload,
)


def derive_terminal_event_key(report: FillReport, terminal_status: OrderStatus) -> str:
    """Deterministic ``broker_event_log.event_key`` for a terminal non-fill event.

    Identity is ``(alpaca_order_id, terminal_status)`` — the broker-authoritative
    terminal disposition of the order, the same fact whether it arrives on the live
    websocket or a later REST recovery replay, so both collapse onto one
    ``broker_event_log`` row via the ``event_key`` PK (idempotency, ADR-0002). The
    ``tevt-`` prefix distinguishes a terminal-status event key from the ``fevt-``
    fill key, which shares the underlying ``alpaca_order_id``.
    """
    h = hashlib.sha256(f"{report.alpaca_order_id}|{terminal_status.value}".encode())
    return f"tevt-{h.hexdigest()[:16]}"


def terminal_status_event_record(
    report: FillReport,
    terminal_status: OrderStatus,
    *,
    thesis_id: ThesisId | None,
    invocation_id: InvocationId | None,
    position_id: PositionId | None,
) -> BrokerEventRecord:
    """Project a zero-fill terminal non-fill event into its event-log record.

    Carries the resolved broker-carried link (``thesis_id`` / ``invocation_id`` /
    ``position_id``) on the INITIAL insert — append-only, never enriched (ADR-0005),
    so a read-time consumer (the no-fill alert, 03c's per-thesis derivation) reads
    the attribution straight off the row. ``raw_payload_json`` preserves the full
    broker report for replay / audit. The terminal disposition the order reached
    (``CANCELLED`` / ``EXPIRED``) is recorded under ``terminal_status`` in the
    payload so the projection rebuild (04a) can derive ``orders.status`` from the log.
    """
    payload = report.model_dump(mode="json")
    payload["terminal_status"] = terminal_status.value
    return BrokerEventRecord(
        event_key=derive_terminal_event_key(report, terminal_status),
        event_type=BrokerEventType.TERMINAL_ORDER_STATUS,
        thesis_id=thesis_id,
        invocation_id=invocation_id,
        position_id=position_id,
        raw_payload_json=serialize_event_payload(payload),
        broker_timestamp=report.fill_timestamp,
        captured_at=datetime.now(UTC),
    )


__all__ = ["derive_terminal_event_key", "terminal_status_event_record"]
