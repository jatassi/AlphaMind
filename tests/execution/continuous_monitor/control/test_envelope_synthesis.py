"""Tests for the production close-submitter's engine-envelope synthesis (ALP-665).

These tests assert that the synthesized :class:`EngineEnvelope` produced by
:class:`OmsCloseSubmitter` conforms to ``engine-envelope-schema.md``:

* ``envelope_id`` matches ``MON.{monitor_session_id}.{trigger_id}``.
* ``source_provenance == "engine_guardrail"``.
* embedded CLOSE carries ``close_rationale_type="risk_management"``,
  ``risk_management_subtype="engine_guardrail"``.
* ``guardrail_trigger_record.position_selection_rationale`` carries the
  operator-console-prefixed rationale.
* The trigger-id generator's next id matches the envelope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from alphamind.execution.broker_adapter.errors import PermanentRejection
from alphamind.execution.broker_adapter.order_options import PermanentRejectionError
from alphamind.execution.continuous_monitor.cascade_dispatch.trigger_ids import (
    TriggerIdGenerator,
)
from alphamind.execution.continuous_monitor.control.envelope_synthesis import (
    OmsCloseSubmitter,
    build_force_close_envelope,
)
from alphamind.execution.continuous_monitor.control.verbs import (
    BrokerErrorClose,
    ForceCloseOutcome,
)


class TestBuildForceCloseEnvelope:
    def test_envelope_id_uses_session_and_trigger(self) -> None:
        envelope = build_force_close_envelope(
            monitor_session_id="session-abc",
            trigger_id=42,
            position_id="pos-1",
            position_selection_rationale="operator_console: vega high",
            rule_breached="operator_console_force_close",
            breach_details_current=0.0,
            breach_details_limit=0.0,
            trigger_timestamp=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        assert envelope.envelope_id == "MON.session-abc.42"

    def test_envelope_provenance_is_engine_guardrail(self) -> None:
        envelope = build_force_close_envelope(
            monitor_session_id="session-abc",
            trigger_id=1,
            position_id="pos-1",
            position_selection_rationale="operator_console: x",
            rule_breached="operator_console_force_close",
            breach_details_current=0.0,
            breach_details_limit=0.0,
            trigger_timestamp=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        assert envelope.source_provenance == "engine_guardrail"

    def test_embedded_close_has_documented_rationale_and_subtype(self) -> None:
        envelope = build_force_close_envelope(
            monitor_session_id="session-abc",
            trigger_id=1,
            position_id="pos-1",
            position_selection_rationale="operator_console: x",
            rule_breached="operator_console_force_close",
            breach_details_current=0.0,
            breach_details_limit=0.0,
            trigger_timestamp=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        close = envelope.commands[0]
        assert close.command_type == "close"
        assert close.close_rationale_type == "risk_management"
        assert close.risk_management_subtype == "engine_guardrail"
        assert close.position_id == "pos-1"

    def test_position_selection_rationale_propagates(self) -> None:
        envelope = build_force_close_envelope(
            monitor_session_id="session-abc",
            trigger_id=1,
            position_id="pos-1",
            position_selection_rationale="operator_console: deliberate exit",
            rule_breached="operator_console_force_close",
            breach_details_current=0.0,
            breach_details_limit=0.0,
            trigger_timestamp=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        assert (
            envelope.guardrail_trigger_record.position_selection_rationale
            == "operator_console: deliberate exit"
        )


@pytest.mark.asyncio
class TestOmsCloseSubmitter:
    async def test_envelope_id_returned_matches_synthesized(self) -> None:
        captured_envelopes: list[Any] = []

        async def fake_submit(envelope: Any) -> None:
            captured_envelopes.append(envelope)

        submitter = OmsCloseSubmitter(
            monitor_session_id="session-abc",
            trigger_ids=TriggerIdGenerator(session_id="session-abc"),
            submit_envelope=fake_submit,
            now=lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        outcome = await submitter.submit_close(
            position_id="pos-1",
            position_selection_rationale="operator_console: vega high",
            rule_breached="operator_console_force_close",
            breach_details_current=0.0,
            breach_details_limit=0.0,
        )
        assert isinstance(outcome, ForceCloseOutcome)
        assert outcome.envelope_id == "MON.session-abc.1"
        assert captured_envelopes[0].envelope_id == outcome.envelope_id

    async def test_trigger_ids_advance_between_calls(self) -> None:
        async def fake_submit(envelope: Any) -> None:
            del envelope

        submitter = OmsCloseSubmitter(
            monitor_session_id="session-abc",
            trigger_ids=TriggerIdGenerator(session_id="session-abc"),
            submit_envelope=fake_submit,
            now=lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        o1 = await submitter.submit_close(
            position_id="pos-1",
            position_selection_rationale="operator_console: a",
            rule_breached="operator_console_force_close",
            breach_details_current=0.0,
            breach_details_limit=0.0,
        )
        o2 = await submitter.submit_close(
            position_id="pos-2",
            position_selection_rationale="operator_console: b",
            rule_breached="operator_console_force_close",
            breach_details_current=0.0,
            breach_details_limit=0.0,
        )
        assert isinstance(o1, ForceCloseOutcome)
        assert isinstance(o2, ForceCloseOutcome)
        assert o1.envelope_id == "MON.session-abc.1"
        assert o2.envelope_id == "MON.session-abc.2"

    async def test_permanent_rejection_returns_broker_error(self) -> None:
        """A genuine broker rejection (Alpaca permanent rejection) returns
        BrokerErrorClose rather than raising (F9).
        """

        async def fake_submit(envelope: Any) -> None:
            del envelope
            raise PermanentRejectionError(
                PermanentRejection(
                    code="other_permanent",
                    http_status=422,
                    alpaca_message="contract invalid",
                )
            )

        submitter = OmsCloseSubmitter(
            monitor_session_id="session-abc",
            trigger_ids=TriggerIdGenerator(session_id="session-abc"),
            submit_envelope=fake_submit,
            now=lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        outcome = await submitter.submit_close(
            position_id="pos-1",
            position_selection_rationale="operator_console: x",
            rule_breached="operator_console_force_close",
            breach_details_current=0.0,
            breach_details_limit=0.0,
        )
        assert isinstance(outcome, BrokerErrorClose)
        assert "contract invalid" in outcome.broker_message

    async def test_httpx_transport_error_returns_broker_error(self) -> None:
        """A network-layer httpx error is treated as a broker transient (F9)."""

        async def fake_submit(envelope: Any) -> None:
            del envelope
            raise httpx.ConnectError("dns failure")

        submitter = OmsCloseSubmitter(
            monitor_session_id="session-abc",
            trigger_ids=TriggerIdGenerator(session_id="session-abc"),
            submit_envelope=fake_submit,
            now=lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        outcome = await submitter.submit_close(
            position_id="pos-1",
            position_selection_rationale="operator_console: x",
            rule_breached="operator_console_force_close",
            breach_details_current=0.0,
            breach_details_limit=0.0,
        )
        assert isinstance(outcome, BrokerErrorClose)
        assert "dns failure" in outcome.broker_message

    async def test_structural_value_error_propagates(self) -> None:
        """A ValueError from the OMS write surface (duplicate trigger_id,
        session mismatch, missing position) is a structural bug and MUST
        propagate rather than be classified as a broker error (F9).
        """

        async def fake_submit(envelope: Any) -> None:
            del envelope
            raise ValueError("duplicate trigger_id")

        submitter = OmsCloseSubmitter(
            monitor_session_id="session-abc",
            trigger_ids=TriggerIdGenerator(session_id="session-abc"),
            submit_envelope=fake_submit,
            now=lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="duplicate trigger_id"):
            await submitter.submit_close(
                position_id="pos-1",
                position_selection_rationale="operator_console: x",
                rule_breached="operator_console_force_close",
                breach_details_current=0.0,
                breach_details_limit=0.0,
            )
