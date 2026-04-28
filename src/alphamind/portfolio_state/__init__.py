"""Portfolio state package — raw portfolio data assembled per invocation."""

from __future__ import annotations

import pathlib
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class PortfolioStateConfig(BaseModel):
    """Configuration for the portfolio-state snapshot assembler."""

    model_config = ConfigDict(frozen=True)

    pm_decision_log_sliding_window_invocations: Annotated[int, Field(gt=0)]
    thesis_resolutions_lookback_trading_days: Annotated[int, Field(gt=0)]
    thesis_quality_aggregates_trailing_windows_days: tuple[int, ...]
    snapshot_freshness_max_phase1_to_snapshot_seconds: Annotated[float, Field(gt=0)]
    snapshot_freshness_max_price_age_seconds: Annotated[float, Field(gt=0)]

    @field_validator("thesis_quality_aggregates_trailing_windows_days")
    @classmethod
    def _validate_trailing_windows(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if not v:
            msg = "trailing_windows_days must be non-empty"
            raise ValueError(msg)
        if any(d <= 0 for d in v):
            msg = "all trailing_windows_days elements must be positive"
            raise ValueError(msg)
        return v


def _flatten_portfolio_state_yaml(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten the nested YAML structure into the flat field-name keys."""
    ps: dict[str, Any] = raw["portfolio_state"]
    pm: dict[str, Any] = ps["pm_decision_log"]
    tr: dict[str, Any] = ps["thesis_resolutions"]
    tq: dict[str, Any] = ps["thesis_quality_aggregates"]
    sf: dict[str, Any] = ps["snapshot_freshness"]
    return {
        "pm_decision_log_sliding_window_invocations": pm["sliding_window_invocations"],
        "thesis_resolutions_lookback_trading_days": tr["lookback_trading_days"],
        "thesis_quality_aggregates_trailing_windows_days": tuple(tq["trailing_windows_days"]),
        "snapshot_freshness_max_phase1_to_snapshot_seconds": sf["max_phase1_to_snapshot_seconds"],
        "snapshot_freshness_max_price_age_seconds": sf["max_price_age_seconds"],
    }


def load_portfolio_state_config(path: pathlib.Path) -> PortfolioStateConfig:
    """Parse *path* as YAML and return a validated :class:`PortfolioStateConfig`."""
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    return PortfolioStateConfig.model_validate(_flatten_portfolio_state_yaml(raw))
