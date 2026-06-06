"""Typed record for the append-only ``broker_event_log`` (ALP-843 / W0a).

The broker-event log is the sole substrate for realized PnL (ADR-0002): the
complete stream of broker→local events that change a Broker-Owned Fact —
**fills + account-activities + corporate-actions + terminal order status**.
It is append-only by construction (ADR-0005): no update path, every row is
captured once. ``event_key`` is the idempotency key — the websocket delivery
and a later REST recovery sweep of the *same* event collapse to one row.

The broker-carried link fields (``thesis_id`` / ``invocation_id`` /
``position_id``) are the decoded FK that rides ``client_order_id`` (ADR-0002).
This story *stores* them; story 01b owns parsing the id format. ``position_id``
is nullable — a position-lifecycle event (expiry / assignment) carries no
``client_order_id`` and attributes via the position→thesis edge instead.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from alphamind._kernel.ids import InvocationId, PositionId, ThesisId


def serialize_event_payload(payload: object) -> str:
    """Deterministically serialize a ``raw_payload`` for a ``broker_event_log`` row.

    The single canonical serialization that feeds ``raw_payload_json`` for every
    event producer (account-activities, corporate-actions, terminal order status).
    ``sort_keys=True`` + ``default=str`` makes the JSON byte-stable across runs and
    Python sessions, so the sha256-derived ``event_key`` idempotency stays exact —
    a re-delivered event hashes to the same key regardless of dict-ordering.
    """
    return json.dumps(payload, default=str, sort_keys=True)


class BrokerEventType(StrEnum):
    """The full event-type vocabulary the log discriminates on.

    Authored complete up front (the migration CHECK-vocab trap): a CHECK
    constraint built from a partial enum retroactively differs on a fresh DB
    when a member is added later. The four families:

    * ``FILL`` — a ``trade_updates`` execution event.
    * Account-activity option-lifecycle types — ``OPEXP`` (expiration),
      ``OPEXC`` (exercise), ``OPASN`` (assignment), ``OPTRD`` (the paired
      option-trade leg that prices an assignment / exercise).
    * Corporate-action types — the Alpaca CA-event vocabulary.
    * ``TERMINAL_ORDER_STATUS`` — a zero-fill terminal order-status event
      (canceled / rejected / expired) routed through the log instead of an
      in-place RMW on ``orders.status``.
    * ``ENTRY_REPRICED`` — a confirmed entry-window cancel-and-replace
      (ALP-867): the monitor escalates a resting equity-limit entry toward the
      market and appends this instead of RMW'ing ``orders.limit_price`` /
      ``alpaca_order_id`` / ``modification_count``; the pipeline projects the
      latest per order onto the order cache + reservation.
    """

    FILL = "FILL"
    # Account-activities option-lifecycle types.
    OPEXP = "OPEXP"
    OPEXC = "OPEXC"
    OPASN = "OPASN"
    OPTRD = "OPTRD"
    # Corporate-action types (Alpaca CA-event vocabulary).
    CA_CASH_DIVIDEND = "CA_CASH_DIVIDEND"
    CA_STOCK_DIVIDEND = "CA_STOCK_DIVIDEND"
    CA_SPLIT = "CA_SPLIT"
    CA_REVERSE_SPLIT = "CA_REVERSE_SPLIT"
    CA_UNIT_SPLIT = "CA_UNIT_SPLIT"
    CA_MERGER = "CA_MERGER"
    CA_SPINOFF = "CA_SPINOFF"
    CA_NAME_CHANGE = "CA_NAME_CHANGE"
    CA_SYMBOL_CHANGE = "CA_SYMBOL_CHANGE"
    CA_WORTHLESS_REMOVAL = "CA_WORTHLESS_REMOVAL"
    CA_REDEMPTION = "CA_REDEMPTION"
    # Zero-fill terminal order-status event.
    TERMINAL_ORDER_STATUS = "TERMINAL_ORDER_STATUS"
    # Entry-window cancel-and-replace (reprice toward the market).
    ENTRY_REPRICED = "ENTRY_REPRICED"


class BrokerEventRecord(BaseModel):
    """Frozen typed handle for one ``broker_event_log`` row.

    Append-only: a record is captured once and never mutated. The
    ``event_key`` is the broker-derived idempotency identity; the FK link
    fields carry the decoded broker-carried link (ADR-0002).
    """

    model_config = ConfigDict(frozen=True)

    event_key: str
    event_type: BrokerEventType
    thesis_id: ThesisId | None
    invocation_id: InvocationId | None
    position_id: PositionId | None
    raw_payload_json: str
    broker_timestamp: datetime | None
    captured_at: datetime


__all__ = ["BrokerEventRecord", "BrokerEventType", "serialize_event_payload"]
