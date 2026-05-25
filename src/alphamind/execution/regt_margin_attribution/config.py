"""Configuration for the Reg T margin attribution module (OCC TIMS / FINRA 4210 baseline v1).

Per ``regt-margin-attribution.md`` § Portfolio-margin reference model, the
shock parameter table is a versioned snapshot of the OCC TIMS RBH/CPM User
Guide (the methodology FINRA Rule 4210(g) points to). Refresh is
operator-driven; ``pm_model_version`` pins the snapshot so aggregates over
time are filterable when methodology changes.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from alphamind.config.loaders import read_yaml_file

_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
_ShockPct = Annotated[float, Field(allow_inf_nan=False, gt=0.0, lt=1.0)]

_DEFAULT_CONFIG_PATH = pathlib.Path(__file__).parents[4] / "config" / "regt_margin_attribution.yaml"


class IvShockMultipliers(BaseModel):
    """IV-shock multipliers paired to the price-shock direction.

    On a down-shock, IV rises (``worst_down_multiplier >= 1.0``).
    On an up-shock, IV falls or holds (``worst_up_multiplier <= 1.0``).
    """

    model_config = ConfigDict(frozen=True)

    # IV must rise on a down-shock, so multiplier >= 1.0.
    worst_down_multiplier: Annotated[float, Field(allow_inf_nan=False, ge=1.0)]
    # IV must fall or hold on an up-shock, so multiplier <= 1.0 (and positive).
    worst_up_multiplier: Annotated[float, Field(allow_inf_nan=False, gt=0.0, le=1.0)]


class ShockParameters(BaseModel):
    """Shock percentages (decimal form) for the OCC TIMS baseline.

    Lookup is per-symbol-overrides first, falling through to
    ``unmapped_default``. All percentage values are in ``(0.0, 1.0)`` —
    e.g., ``0.15`` for ±15%. Per-symbol override keys are normalised to
    upper-case on construction. ``extra="forbid"`` so operator-config
    drift (retired asset-class taxonomy keys, typos, future-renamed
    fields) fails loudly at load time rather than silently no-op'ing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    per_symbol_overrides: Mapping[str, _ShockPct]
    unmapped_default: _ShockPct

    @model_validator(mode="before")
    @classmethod
    def _normalise_symbol_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and "per_symbol_overrides" in data:
            raw = data["per_symbol_overrides"]
            if isinstance(raw, dict):
                data = dict(data)
                data["per_symbol_overrides"] = {k.upper(): v for k, v in raw.items()}
        return data


class RegTMarginAttributionConfig(BaseModel):
    """Operator-configured snapshot for the Reg T margin attribution module.

    Frozen; all fields must be finite. Load via :func:`load_regt_margin_attribution_config`.
    """

    model_config = ConfigDict(frozen=True)

    pm_model_version: str
    shock_parameters: ShockParameters
    iv_shock: IvShockMultipliers
    risk_free_rate_annual: _FiniteFloat

    @field_validator("pm_model_version")
    @classmethod
    def _non_empty_version(cls, v: str) -> str:
        if not v.strip():
            msg = "pm_model_version must be a non-empty string"
            raise ValueError(msg)
        return v

    @field_validator("risk_free_rate_annual")
    @classmethod
    def _risk_free_rate_in_range(cls, v: float) -> float:
        if not (-0.05 <= v <= 0.20):
            msg = f"risk_free_rate_annual must be in [-0.05, 0.20], got {v}"
            raise ValueError(msg)
        return v


def load_regt_margin_attribution_config(
    path: pathlib.Path | str | None = None,
) -> RegTMarginAttributionConfig:
    """Parse ``path`` as YAML and return a validated :class:`RegTMarginAttributionConfig`.

    If ``path`` is ``None``, reads ``config/regt_margin_attribution.yaml`` relative
    to the repository root (resolved from this file's location).
    """
    resolved = pathlib.Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    return RegTMarginAttributionConfig.model_validate(read_yaml_file(resolved))
