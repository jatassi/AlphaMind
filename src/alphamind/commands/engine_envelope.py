"""Canonical Pydantic models for engine-originated command envelopes — ALP-372 / story 02a.

Translates ``docs/design/05-execution-layer/engine-envelope-schema.md`` (JSON
Schema Draft 2020-12) into typed Pydantic shapes — :class:`EngineEnvelope`,
:class:`GuardrailTriggerRecord`, :class:`BreachDetails`,
:class:`SecondaryBreachCheckResult` — with ``model_validator``s for the
cross-field invariants the JSON Schema expresses via ``allOf`` and the prose
"Notes on cross-field invariants" section.

The schema is the contract; this module is its Python counterpart. Every
``$defs`` entry in the schema has a class here. Field-level constraints
(``envelope_id`` regex, ``commands`` ``minItems: 1, maxItems: 1``,
``source_provenance`` const) are enforced via ``Field(...)``; structural
invariants that span fields (embedded CLOSE rationale/subtype constraints,
top-level vs. nested ``trigger_timestamp`` equality, ``invocation_id`` always
``None``) are enforced by ``model_validator``.

The continuous monitor (ALP-123) is the producer; story 04
(``submit_engine_envelope``) is the consumer that validates incoming
envelopes against this contract on receipt.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from alphamind._kernel.ids import EnvelopeId
from alphamind._kernel.ids import (
    envelope_id as _envelope_id_constructor,
)
from alphamind.commands.command_models import CloseCommand

__all__ = [
    "BreachDetails",
    "EngineEnvelope",
    "GuardrailTriggerRecord",
    "SecondaryBreachCheckResult",
    "SecondaryBreachResult",
    "SourceProvenance",
    "engine_envelope_schema",
]


# ---------------------------------------------------------------------------
# Vocabulary literal aliases — wire-format Literal types per the JSON Schema.
# ---------------------------------------------------------------------------


SourceProvenance = Literal["engine_guardrail"]
SecondaryBreachResult = Literal[
    "no_secondary_breach",
    "secondary_breach_avoided",
    "deferred_to_pm",
]


# ---------------------------------------------------------------------------
# Sub-records
# ---------------------------------------------------------------------------


class BreachDetails(BaseModel):
    """The numeric breach context — current value, limit, signed overage.

    Per design, ``overage`` is the signed magnitude of the breach (positive
    for overages, negative for deficit-direction cases where the sign is
    clearer that way). ``unit`` and ``regime_at_breach`` are optional
    annotations populated by the continuous monitor when known.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    current_value: float
    limit_value: float
    overage: float
    unit: str | None = None
    regime_at_breach: str | None = None


class SecondaryBreachCheckResult(BaseModel):
    """The outcome of a secondary-breach check during position selection.

    Populated by the continuous monitor when its position selection required
    a secondary-breach check per ``oms-commands.md § Command origins``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: SecondaryBreachResult
    notes: str | None = None


class GuardrailTriggerRecord(BaseModel):
    """The breach context that motivated this protective CLOSE.

    Populated by the continuous monitor at trigger time. ``cascade_id`` is
    set when the trigger is part of a cascade (margin call + forced
    reduction, primary breach + secondary breach); ``secondary_breach_check_result``
    is set when position selection required one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_breached: str = Field(min_length=1)
    trigger_timestamp: datetime
    breach_details: BreachDetails
    position_selection_rationale: str = Field(min_length=1)
    cascade_id: str | None = None
    secondary_breach_check_result: SecondaryBreachCheckResult | None = None


# ---------------------------------------------------------------------------
# Engine envelope
# ---------------------------------------------------------------------------


class EngineEnvelope(BaseModel):
    """One engine-originated command envelope from the continuous monitor.

    Each envelope carries exactly one CLOSE command with
    ``close_rationale_type == "risk_management"`` and
    ``risk_management_subtype == "engine_guardrail"``. Cascades produce
    multiple envelopes linked by a shared ``guardrail_trigger_record.cascade_id``,
    not multiple commands per envelope.

    ``invocation_id`` is always ``None`` for engine-originated envelopes —
    ``trigger_timestamp`` is the time anchor. ``trigger_timestamp`` at the
    top level and inside ``guardrail_trigger_record`` are denormalized for
    OMS intake convenience; equality is a structural invariant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    envelope_id: EnvelopeId = Field(pattern=r"^MON\.[^.]+\.[0-9]+$")
    invocation_id: None = None
    trigger_timestamp: datetime
    source_provenance: SourceProvenance
    guardrail_trigger_record: GuardrailTriggerRecord
    commands: tuple[CloseCommand, ...] = Field(min_length=1, max_length=1)

    @field_validator("envelope_id", mode="after")
    @classmethod
    def _construct_envelope_id(cls, value: str) -> EnvelopeId:
        return _envelope_id_constructor(value)

    @model_validator(mode="after")
    def _validate_invariants(self) -> EngineEnvelope:
        if self.invocation_id is not None:
            raise ValueError(
                "EngineEnvelope invocation_id must be None for engine-originated envelopes"
            )
        if self.trigger_timestamp != self.guardrail_trigger_record.trigger_timestamp:
            raise ValueError(
                "EngineEnvelope.trigger_timestamp must equal "
                "guardrail_trigger_record.trigger_timestamp (denormalized; equality required)"
            )
        embedded = self.commands[0]
        if embedded.close_rationale_type != "risk_management":
            raise ValueError(
                'EngineEnvelope embedded CLOSE must have close_rationale_type="risk_management"'
            )
        if embedded.risk_management_subtype != "engine_guardrail":
            raise ValueError(
                'EngineEnvelope embedded CLOSE must have risk_management_subtype="engine_guardrail"'
            )
        return self


# ---------------------------------------------------------------------------
# Schema export accessor
# ---------------------------------------------------------------------------


_ENGINE_ENVELOPE_ADAPTER: TypeAdapter[EngineEnvelope] = TypeAdapter(EngineEnvelope)


def engine_envelope_schema() -> dict[str, Any]:
    """Return the JSON Schema for the engine-originated envelope."""
    schema = dict(_ENGINE_ENVELOPE_ADAPTER.json_schema())
    schema["title"] = "EngineEnvelope"
    return schema
