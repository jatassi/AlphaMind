"""Entry-window reprice → broker-event log (ALP-867 / W1c sibling).

The continuous monitor's entry-window watcher escalates a still-resting
equity-limit entry toward the market (ALP-740) via an Alpaca cancel-and-replace.
**This used to be an in-place read-modify-write** on ``orders.limit_price`` /
``alpaca_order_id`` / ``modification_count`` + the capital reservation, performed
by the monitor on a fresh ``InvocationHandle`` — the residual half of the
single-writer-by-construction leak (ADR-0005, invariant 1; the cancel half was
relocated in ALP-863). It is now an **append** to the append-only
``broker_event_log`` (ADR-0002 W1c): a confirmed reprice lands as one immutable
``ENTRY_REPRICED`` row, idempotent on the ``event_key`` PK. The order row's
``limit_price`` / ``alpaca_order_id`` / ``modification_count`` and the reservation
become a projection derived from the log, written only by the single (pipeline)
writer in ``projection_rebuild.py`` — the monitor no longer RMWs them.

This module owns the deterministic ``event_key`` derivation and the record
builder, mirroring :mod:`order_status_sync`; the actual insert goes through the
canonical idempotent helper
:func:`alphamind.execution.write_paths.broker_event_persistence.append_broker_event`.

The event carries **no** broker-carried link (``thesis_id`` / ``invocation_id`` /
``position_id`` are ``None``): a reprice moves no money at the broker, so it
contributes nothing to realized PnL and must stay out of any per-thesis
derivation / provenance (``derive_thesis_pnl`` folds by ``thesis_id`` alone). The
projection resolves the target order — and its attribution for the activity-log
entry — from the ``entry_order_id`` carried on the payload.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from alphamind._kernel.money import Price
from alphamind.state.records_broker_event_log import (
    BrokerEventRecord,
    BrokerEventType,
    serialize_event_payload,
)


def derive_entry_reprice_event_key(new_alpaca_order_id: str) -> str:
    """Deterministic ``broker_event_log.event_key`` for an ``ENTRY_REPRICED`` event.

    Identity is the **new** ``alpaca_order_id`` the cancel-and-replace produced —
    the broker assigns a fresh id per replace, so each reprice is a distinct event
    and a re-append of the *same* confirmed replace (a watcher retry that re-reads
    the same broker id) collapses onto one ``broker_event_log`` row via the
    ``event_key`` PK (idempotency, ADR-0002). The ``revt-`` prefix distinguishes a
    reprice event key from the ``tevt-`` terminal-status and ``fevt-`` fill keys.
    """
    h = hashlib.sha256(new_alpaca_order_id.encode())
    return f"revt-{h.hexdigest()[:16]}"


def entry_reprice_event_record(
    *,
    entry_order_id: str,
    new_limit: Price,
    new_alpaca_order_id: str,
    reason: str,
) -> BrokerEventRecord:
    """Project a confirmed entry-window reprice into its event-log record.

    ``raw_payload_json`` carries everything the pipeline projection needs to drive
    :func:`persist_entry_window_reprice` from the log alone: the OMS
    ``entry_order_id`` (which the projection resolves the order row + its
    attribution from), the new marketable ``limit`` (serialized as a Decimal
    string), the new broker ``alpaca_order_id``, and the reprice ``reason``. The
    link columns are ``None`` — see the module docstring.
    """
    payload = {
        "entry_order_id": entry_order_id,
        "new_limit": str(new_limit),
        "new_alpaca_order_id": new_alpaca_order_id,
        "reason": reason,
    }
    return BrokerEventRecord(
        event_key=derive_entry_reprice_event_key(new_alpaca_order_id),
        event_type=BrokerEventType.ENTRY_REPRICED,
        thesis_id=None,
        invocation_id=None,
        position_id=None,
        raw_payload_json=serialize_event_payload(payload),
        broker_timestamp=None,
        captured_at=datetime.now(UTC),
    )


__all__ = ["derive_entry_reprice_event_key", "entry_reprice_event_record"]
