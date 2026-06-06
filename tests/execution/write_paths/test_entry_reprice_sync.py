"""Tests for the ``ENTRY_REPRICED`` event builders (ALP-867).

``entry_reprice_sync`` is the W1c sibling of ``order_status_sync``: instead of the
monitor RMW'ing ``orders.limit_price`` / ``alpaca_order_id`` / ``modification_count``
on a reprice, it derives a deterministic ``broker_event_log`` ``event_key`` and
builds the append-only ``ENTRY_REPRICED`` record the pipeline projects. Here we pin
the pure builders; the append + no-RMW + projection integration is covered by the
repricer, wiring, and projection-rebuild tests.
"""

from __future__ import annotations

import json

from alphamind._kernel.money import price
from alphamind.execution.write_paths.entry_reprice_sync import (
    derive_entry_reprice_event_key,
    entry_reprice_event_record,
)
from alphamind.state.records_broker_event_log import BrokerEventType


def test_event_key_is_deterministic_and_reprice_prefixed() -> None:
    key = derive_entry_reprice_event_key("broker-uuid-1")
    assert key == derive_entry_reprice_event_key("broker-uuid-1")
    assert key.startswith("revt-")


def test_event_key_distinguishes_each_replace_id() -> None:
    # Each cancel-and-replace yields a fresh broker id → a distinct event key,
    # so two reprices of the same order collapse only on a re-append of the SAME id.
    a = derive_entry_reprice_event_key("broker-uuid-1")
    b = derive_entry_reprice_event_key("broker-uuid-2")
    assert a != b


def test_record_carries_payload_and_no_attribution_link() -> None:
    record = entry_reprice_event_record(
        entry_order_id="ord-entry-1",
        new_limit=price("101.25"),
        new_alpaca_order_id="broker-uuid-1",
        reason="entry_window_reprice",
    )
    assert record.event_type is BrokerEventType.ENTRY_REPRICED
    assert record.event_key == derive_entry_reprice_event_key("broker-uuid-1")
    # A reprice moves no money at the broker — it carries NO broker-carried link, so
    # it never enters any per-thesis PnL fold / provenance.
    assert record.thesis_id is None
    assert record.invocation_id is None
    assert record.position_id is None
    assert record.broker_timestamp is None
    # The payload self-identifies the target + the projection inputs.
    payload = json.loads(record.raw_payload_json)
    assert payload["entry_order_id"] == "ord-entry-1"
    assert payload["new_limit"] == "101.25"
    assert payload["new_alpaca_order_id"] == "broker-uuid-1"
    assert payload["reason"] == "entry_window_reprice"
