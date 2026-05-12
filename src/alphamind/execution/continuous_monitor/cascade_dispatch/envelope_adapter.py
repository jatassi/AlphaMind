"""Translate breach-behavior engine envelopes into OMS engine envelopes (ALP-438).

The breach-behavior package owns :func:`compose_engine_envelope`, which
returns a :class:`alphamind.risk_guardrails.breach_behavior.types.EngineEnvelope`
with a single embedded ``EngineCloseCommand``. The OMS submission path
(:func:`alphamind.execution.oms.submit_engine_envelope`) consumes a
structurally-equivalent but distinct Pydantic model:
:class:`alphamind.execution.oms.engine_envelope.EngineEnvelope` with a
``commands`` tuple of OMS :class:`CloseCommand` instances.

This module bridges the two — a thin, pure translator that:

* Maps every field by name (envelope_id, trigger_timestamp, source_provenance,
  guardrail_trigger_record fields).
* Drops the breach-behavior command_id (which uses ordinal ``1``) so the OMS
  side derives the canonical ``MON.{session}.{trigger}.0`` form per
  ``oms-command-ids.md`` (the engine-originated convention is ordinal ``0``
  for the single embedded CLOSE).
* Re-validates every output through Pydantic so the structural invariants the
  OMS schema enforces (close_rationale_type, risk_management_subtype, single
  command) catch any drift during translation.
"""

from __future__ import annotations

from alphamind.execution.oms.command_models import CloseCommand
from alphamind.execution.oms.engine_envelope import (
    BreachDetails as OmsBreachDetails,
)
from alphamind.execution.oms.engine_envelope import (
    EngineEnvelope as OmsEngineEnvelope,
)
from alphamind.execution.oms.engine_envelope import (
    GuardrailTriggerRecord as OmsGuardrailTriggerRecord,
)
from alphamind.execution.oms.engine_envelope import (
    SecondaryBreachCheckResult as OmsSecondaryBreachCheckResult,
)
from alphamind.risk_guardrails.breach_behavior import (
    EngineCloseCommand,
    EngineEnvelope,
    EngineGuardrailTriggerRecord,
    SecondaryBreachCheckResult,
)
from alphamind.risk_guardrails.breach_behavior.types import (
    BreachDetails,
)


def to_oms_engine_envelope(envelope: EngineEnvelope) -> OmsEngineEnvelope:
    """Translate a breach-behavior :class:`EngineEnvelope` into the OMS shape.

    The OMS-side embedded CLOSE has ``command_id=None`` so the canonical
    ``MON.{session}.{trigger}.0`` form is derived by
    :func:`submit_engine_envelope` at submission time — the breach-behavior
    composer's ordinal-``1`` id is *not* propagated.
    """
    oms_close = _close_command_from(envelope.command)
    oms_trigger = _trigger_record_from(envelope.guardrail_trigger_record)
    return OmsEngineEnvelope(
        envelope_id=envelope.envelope_id,
        invocation_id=None,
        trigger_timestamp=envelope.trigger_timestamp,
        source_provenance=envelope.source_provenance,
        guardrail_trigger_record=oms_trigger,
        commands=(oms_close,),
    )


def _close_command_from(command: EngineCloseCommand) -> CloseCommand:
    """Map :class:`EngineCloseCommand` → OMS :class:`CloseCommand`.

    ``command_id`` is dropped so the OMS path derives ordinal ``0`` per
    ``oms-command-ids.md``.
    """
    return CloseCommand(
        command_id=None,
        command_type="close",
        position_id=command.position_id,
        quantity=command.quantity_or_all,
        order_type=command.execution_method,
        limit_price=command.limit_price,
        close_rationale_type=command.close_rationale_type.value,
        risk_management_subtype=command.risk_management_subtype.value,
    )


def _trigger_record_from(
    record: EngineGuardrailTriggerRecord,
) -> OmsGuardrailTriggerRecord:
    return OmsGuardrailTriggerRecord(
        rule_breached=record.rule_breached,
        trigger_timestamp=record.trigger_timestamp,
        breach_details=_breach_details_from(record.breach_details),
        position_selection_rationale=record.position_selection_rationale,
        cascade_id=record.cascade_id,
        secondary_breach_check_result=(
            _secondary_breach_result_from(record.secondary_breach_check_result)
            if record.secondary_breach_check_result is not None
            else None
        ),
    )


def _breach_details_from(details: BreachDetails) -> OmsBreachDetails:
    regime_at_breach = (
        details.regime_at_breach.value if details.regime_at_breach is not None else None
    )
    return OmsBreachDetails(
        current_value=details.current_value,
        limit_value=details.limit_value,
        overage=details.overage,
        unit=details.unit,
        regime_at_breach=regime_at_breach,
    )


def _secondary_breach_result_from(
    result: SecondaryBreachCheckResult,
) -> OmsSecondaryBreachCheckResult:
    return OmsSecondaryBreachCheckResult(
        result=result.result.value,
        notes=result.notes,
    )


__all__ = [
    "to_oms_engine_envelope",
]
