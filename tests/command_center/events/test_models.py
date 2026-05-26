"""Tests for the browser-facing event envelope (ALP-669, story 04b).

The envelope is the JSON the command-center backend sends to the
browser inside the SSE ``data:`` line. It carries three fields:

* ``source`` — ``"pipeline"`` or ``"monitor"``, identifying which
  upstream produced the event.
* ``event`` — the upstream event-type name (e.g. ``"invocation_started"``).
* ``data`` — the raw upstream payload, round-tripped unmodified per
  ALP-669 AC.

The model is Pydantic because this is the boundary — the SSE wire is
where the typed-internal world meets JSON-on-the-wire (P5: Pydantic at
boundaries only). Internal multiplexer state stays on the frozen
dataclasses from :mod:`alphamind.command_center._kernel.events`.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from alphamind.command_center.events.models import BrowserEventEnvelope


class TestBrowserEventEnvelope:
    def test_serializes_to_expected_json_shape(self) -> None:
        envelope = BrowserEventEnvelope(
            source="pipeline",
            event="invocation_started",
            data={"invocation_id": "inv-1", "run_type": "market_hours_rolling"},
        )

        payload = envelope.model_dump(mode="json")

        assert payload == {
            "source": "pipeline",
            "event": "invocation_started",
            "data": {"invocation_id": "inv-1", "run_type": "market_hours_rolling"},
        }

    def test_json_round_trips(self) -> None:
        original = BrowserEventEnvelope(
            source="monitor",
            event="fill_received",
            data={"order_id": "ord-1", "fill_qty": 100.0},
        )

        wire = original.model_dump_json()
        decoded = BrowserEventEnvelope.model_validate_json(wire)

        assert decoded == original
        # JSON itself decodes as expected.
        assert json.loads(wire) == original.model_dump(mode="json")

    def test_rejects_unknown_source_literal(self) -> None:
        with pytest.raises(ValidationError):
            BrowserEventEnvelope(
                source="alerts",  # type: ignore[arg-type]
                event="anything",
                data={},
            )

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            BrowserEventEnvelope.model_validate(
                {
                    "source": "pipeline",
                    "event": "heartbeat",
                    "data": {"timestamp": "2026-05-26T00:00:00+00:00"},
                    "extra": "boom",
                }
            )
