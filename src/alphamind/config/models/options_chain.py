"""Pydantic model for options_chain.yaml — chain-slice selection policy (ALP-948).

The operator-tunable knobs behind the decision layer's contract-level options
read: the strike band, expiration window, open-interest floor, and contract
cap the ``retrieve_options_chain`` tool's chain slice applies, plus the
premium drift tolerance the analyst validator's ``option_premium_staleness``
check enforces against the chain-snapshot NBBO midpoint. Starting values are
operator-tunable configuration, not contract.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OptionsChainConfig(BaseModel):
    """Operator-tunable chain-slice filters + premium validation tolerance."""

    model_config = ConfigDict(frozen=True, strict=True)

    strike_band_pct: float = Field(gt=0)
    min_days_to_expiration: int = Field(ge=0)
    max_days_to_expiration: int = Field(ge=1)
    min_open_interest: int = Field(ge=0)
    max_contracts_rendered: int = Field(ge=1)
    premium_staleness_tolerance_pct: float = Field(gt=0)

    @model_validator(mode="after")
    def _expiration_window_ordered(self) -> Self:
        # A max below the min selects an empty expiration window — every chain
        # slice would render no contracts while looking configured.
        if self.max_days_to_expiration < self.min_days_to_expiration:
            raise ValueError("max_days_to_expiration must be >= min_days_to_expiration")
        return self
