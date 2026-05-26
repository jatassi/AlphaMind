"""Production close-submitter — synthesizes engine envelopes for the force-close verb (ALP-665).

:func:`build_force_close_envelope` is the pure function that builds an
:class:`EngineEnvelope` conforming to ``engine-envelope-schema.md`` from the
operator-console inputs the ``POST /control/force_close_position`` verb
collects:

* ``envelope_id`` = ``MON.{monitor_session_id}.{trigger_id}``
* ``source_provenance`` = ``engine_guardrail``
* embedded ``CloseCommand`` with ``close_rationale_type="risk_management"``,
  ``risk_management_subtype="engine_guardrail"``, and ``quantity="all"``.
* ``guardrail_trigger_record.position_selection_rationale`` carries the
  operator-console-prefixed rationale.

:class:`OmsCloseSubmitter` is the production seam (implementing
:class:`CloseSubmitterProtocol` from ``verbs.py``) that synthesizes the
envelope via :func:`build_force_close_envelope` and routes it through the
caller-supplied ``submit_envelope`` callable — production wires
:func:`submit_engine_envelope` (via the breach-loop's
``make_submit_envelope`` substrate) so the cancel / close paths share the
same OMS write surface.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from alphamind._kernel.ids import (
    EnvelopeId,
    PositionId,
)
from alphamind._kernel.ids import (
    envelope_id as _envelope_id_constructor,
)
from alphamind.commands.command_models import CloseCommand
from alphamind.commands.engine_envelope import (
    BreachDetails,
    EngineEnvelope,
    GuardrailTriggerRecord,
)
from alphamind.execution.broker_adapter.order_options import PermanentRejectionError
from alphamind.execution.continuous_monitor.cascade_dispatch.trigger_ids import (
    TriggerIdGenerator,
)
from alphamind.execution.continuous_monitor.control.verbs import (
    BrokerErrorClose,
    ForceCloseOutcome,
)

SubmitEnvelopeCallable = Callable[[EngineEnvelope], Awaitable[Any]]


def build_force_close_envelope(
    *,
    monitor_session_id: str,
    trigger_id: int,
    position_id: str,
    position_selection_rationale: str,
    rule_breached: str,
    breach_details_current: float,
    breach_details_limit: float,
    trigger_timestamp: datetime,
) -> EngineEnvelope:
    """Build an :class:`EngineEnvelope` for an operator-console force close.

    The envelope's ``envelope_id`` is ``MON.{monitor_session_id}.{trigger_id}``;
    the embedded ``CloseCommand`` carries ``close_rationale_type="risk_management"``
    and ``risk_management_subtype="engine_guardrail"`` per the engine-envelope
    schema. ``quantity="all"`` because the operator-console verb requests a
    full close — no partial-quantity surface on the wire.
    """
    envelope_id_str = f"MON.{monitor_session_id}.{trigger_id}"
    envelope_id: EnvelopeId = _envelope_id_constructor(envelope_id_str)
    close = CloseCommand(
        command_type="close",
        position_id=PositionId(position_id),
        quantity="all",
        order_type="market",
        limit_price=None,
        close_rationale_type="risk_management",
        invalidation_reason=None,
        risk_management_subtype="engine_guardrail",
    )
    trigger_record = GuardrailTriggerRecord(
        rule_breached=rule_breached,
        trigger_timestamp=trigger_timestamp,
        breach_details=BreachDetails(
            current_value=breach_details_current,
            limit_value=breach_details_limit,
            overage=breach_details_current - breach_details_limit,
        ),
        position_selection_rationale=position_selection_rationale,
    )
    return EngineEnvelope(
        envelope_id=envelope_id,
        invocation_id=None,
        trigger_timestamp=trigger_timestamp,
        source_provenance="engine_guardrail",
        guardrail_trigger_record=trigger_record,
        commands=(close,),
    )


class OmsCloseSubmitter:
    """Production seam binding the force-close verb to the OMS write path.

    Implements :class:`CloseSubmitterProtocol` from ``verbs.py``. Constructed
    once per monitor session; reuses the shared ``TriggerIdGenerator`` from
    the breach-loop / cascade-dispatch wiring so engine envelopes synthesized
    by the operator-console verb participate in the same monotonic sequence.

    ``submit_envelope`` is wired in production by the daemon to the
    breach-loop substrate's ``make_submit_envelope`` closure — see
    :mod:`alphamind.execution.continuous_monitor.breach_loop.production_substrate`.
    """

    def __init__(
        self,
        *,
        monitor_session_id: str,
        trigger_ids: TriggerIdGenerator,
        submit_envelope: SubmitEnvelopeCallable,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if trigger_ids.session_id != monitor_session_id:
            msg = (
                f"trigger_ids.session_id {trigger_ids.session_id!r} does not match "
                f"monitor_session_id {monitor_session_id!r}"
            )
            raise ValueError(msg)
        self._monitor_session_id = monitor_session_id
        self._trigger_ids = trigger_ids
        self._submit_envelope = submit_envelope
        self._now = now

    async def submit_close(
        self,
        *,
        position_id: str,
        position_selection_rationale: str,
        rule_breached: str,
        breach_details_current: float,
        breach_details_limit: float,
    ) -> ForceCloseOutcome | BrokerErrorClose:
        trigger_id = self._trigger_ids.next()
        envelope = build_force_close_envelope(
            monitor_session_id=self._monitor_session_id,
            trigger_id=trigger_id,
            position_id=position_id,
            position_selection_rationale=position_selection_rationale,
            rule_breached=rule_breached,
            breach_details_current=breach_details_current,
            breach_details_limit=breach_details_limit,
            trigger_timestamp=self._now(),
        )
        try:
            await self._submit_envelope(envelope)
        except (PermanentRejectionError, httpx.HTTPError) as exc:
            # Narrow catch (F9): only genuine broker / HTTP transport errors
            # surface as ``BrokerErrorClose``. Structural ``ValueError``
            # (duplicate trigger_id, session mismatch, malformed envelope,
            # missing position) MUST propagate — those indicate bugs in the
            # caller's wiring, not a broker transient.
            return BrokerErrorClose(broker_message=str(exc))
        return ForceCloseOutcome(envelope_id=str(envelope.envelope_id))


__all__ = [
    "OmsCloseSubmitter",
    "SubmitEnvelopeCallable",
    "build_force_close_envelope",
]
