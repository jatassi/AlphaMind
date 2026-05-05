"""Minimal Pydantic OMS command models — ALP-323 (transitional).

This module is **transitional**. It models only the fields the Layer-2/3
PM-envelope validator reads — not the full broker-grade OMS command shape.
When `ALP-120 <https://linear.app/alphamind-jatassi/issue/ALP-120>`_ ships
full Pydantic OMS command models, those replace these inline shapes and the
PM imports from the canonical home in a coordinated edit.

Per parent issue ALP-117 pre-resolved decision (C). The discriminator field is
``command_type`` over the five command variants (open / close / adjust / cancel
/ add). Sub-records ``OMSInstrument`` and ``OMSPositionSize`` carry only the
fields the validator inspects (``instrument.asset_type`` + ``direction`` +
``underlying``; ``position_size.sector`` from the risk-side 4-way taxonomy).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, model_validator

from alphamind.decision.analyst.models import Sector

__all__ = [
    "AddCommand",
    "AdjustCommand",
    "CancelCommand",
    "CloseCommand",
    "OMSCommand",
    "OMSInstrument",
    "OMSPositionSize",
    "OpenCommand",
]


# ---------------------------------------------------------------------------
# Sub-records — minimal shape for validator consumption
# ---------------------------------------------------------------------------


class OMSInstrument(BaseModel):
    """Minimal instrument shape — only fields the validator inspects."""

    model_config = ConfigDict(frozen=True)

    asset_type: Literal["equity", "option", "strategy"]
    direction: Literal["long", "short"]
    underlying: str = Field(min_length=1)


class OMSPositionSize(BaseModel):
    """Minimal position-size shape — only the sector field the validator inspects."""

    model_config = ConfigDict(frozen=True)

    sector: Sector


# ---------------------------------------------------------------------------
# Command variants — discriminated on ``command_type``
# ---------------------------------------------------------------------------


class OpenCommand(BaseModel):
    """OPEN command — minimal shape for structural detection.

    Full shape (entry_order / target / invalidation_legs / thesis) lives in
    the broker-grade OMS command model (ALP-120). The validator reads only
    the instrument + position_size fields modeled here.
    """

    model_config = ConfigDict(frozen=True)

    command_type: Literal["open"]
    instrument: OMSInstrument
    position_size: OMSPositionSize


class CloseCommand(BaseModel):
    """CLOSE command — minimal shape with risk-management subtype invariant.

    The validator enforces that ``close_rationale_type == "risk_management"``
    requires ``risk_management_subtype`` to be set; PM-originated CLOSEs
    additionally use ``pm_directed`` per the envelope schema's conditional.
    """

    model_config = ConfigDict(frozen=True)

    command_type: Literal["close"]
    position_id: str = Field(min_length=1)
    close_rationale_type: Literal[
        "thesis_invalidated",
        "target_reached",
        "risk_management",
        "tactical_exit",
    ]
    risk_management_subtype: Literal["pm_directed", "engine_guardrail"] | None = None

    @model_validator(mode="after")
    def _validate_risk_management_subtype_required(self) -> CloseCommand:
        if self.close_rationale_type == "risk_management" and self.risk_management_subtype is None:
            raise ValueError(
                "CloseCommand close_rationale_type=risk_management requires risk_management_subtype"
            )
        return self


class AdjustCommand(BaseModel):
    """ADJUST command — minimal shape (validator reads only the position id)."""

    model_config = ConfigDict(frozen=True)

    command_type: Literal["adjust"]
    position_id: str = Field(min_length=1)


class CancelCommand(BaseModel):
    """CANCEL command — minimal shape (validator reads only the order id)."""

    model_config = ConfigDict(frozen=True)

    command_type: Literal["cancel"]
    order_id: str = Field(min_length=1)
    cancel_reason: str | None = None


class AddCommand(BaseModel):
    """ADD command — minimal shape mirroring OpenCommand.

    Same minimal modeling as :class:`OpenCommand`; the broker-grade fields
    (entry_order / bracket_adjustment / thesis_component_updates) belong in
    the full OMS command model (ALP-120).
    """

    model_config = ConfigDict(frozen=True)

    command_type: Literal["add"]
    position_id: str = Field(min_length=1)
    instrument: OMSInstrument
    position_size: OMSPositionSize


# ---------------------------------------------------------------------------
# Discriminated union over the five command variants
# ---------------------------------------------------------------------------


OMSCommand = Annotated[
    OpenCommand | CloseCommand | AdjustCommand | CancelCommand | AddCommand,
    Discriminator("command_type"),
]
