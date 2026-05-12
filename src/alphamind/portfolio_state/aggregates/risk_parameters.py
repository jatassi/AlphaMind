"""Active risk parameter set aggregate (Tier 3 — raw state category 4d).

Per ``state-persistence.md`` § Tier 3, ``ActiveRiskParameterSet`` is the
regime-multiplied parameter snapshot the OMS exposes to consumers, recomputable
from Tier 1 + Tier 2 data plus the regime adaptation layer.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alphamind._kernel.regime import RegimeLabel, RegimeTransitionState
from alphamind.portfolio_state.aggregates.risk_budget import _assert_unique_rule_ids

__all__ = [
    "ActiveRiskParameterEntry",
    "ActiveRiskParameterSet",
]

_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class ActiveRiskParameterEntry(BaseModel):
    """One rule's slot in the active risk parameter set."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    rule_label: str
    value: _FiniteFloat
    unit: str
    regime_multiplier_applied: float
    base_value: float


class ActiveRiskParameterSet(BaseModel):
    """Raw state 4d — active risk parameter set."""

    model_config = ConfigDict(frozen=True)

    regime_label: RegimeLabel
    transition_state: RegimeTransitionState
    transition_invocations_remaining: Annotated[int, Field(ge=0)]
    parameter_change_flag: bool
    entries: tuple[ActiveRiskParameterEntry, ...]
    active_overlays: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_stable_invocations(self) -> ActiveRiskParameterSet:
        if (
            self.transition_state == RegimeTransitionState.STABLE
            and self.transition_invocations_remaining != 0
        ):
            msg = "transition_invocations_remaining must be 0 when transition_state is STABLE"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_unique_entry_rule_ids(self) -> ActiveRiskParameterSet:
        _assert_unique_rule_ids(self.entries)
        return self
