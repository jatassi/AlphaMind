"""Configuration for the breach-behavior primitives."""

from __future__ import annotations

import pathlib
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class BreachBehaviorConfig(BaseModel):
    """Operator-tunable knobs for the breach-behavior primitives."""

    model_config = ConfigDict(frozen=True)

    forced_reduction_short_trim_target_pct_of_limit: Annotated[float, Field(gt=0.0, le=100.0)]
    forced_reduction_total_short_immediate_threshold_pct_of_limit: Annotated[float, Field(gt=100.0)]
    drawdown_velocity_window_minutes: Annotated[int, Field(gt=0)]
    drawdown_velocity_threshold_pct_of_daily_limit: Annotated[float, Field(gt=0.0, le=100.0)]
    multi_rule_breach_simultaneous_deferred_rules_count: Annotated[int, Field(ge=2)]
    cascade_max_steps: Annotated[int, Field(gt=0)]
    delta_buffer_secondary_check_buffer_factor: Annotated[float, Field(gt=0.0)]


def _flatten_breach_behavior_yaml(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten the nested YAML structure into the flat field-name keys."""
    bb: dict[str, Any] = raw["breach_behavior"]
    fr: dict[str, Any] = bb["forced_reduction"]
    dv: dict[str, Any] = bb["drawdown_velocity"]
    mrb: dict[str, Any] = bb["multi_rule_breach"]
    cas: dict[str, Any] = bb["cascade"]
    db: dict[str, Any] = bb["delta_buffer"]
    return {
        "forced_reduction_short_trim_target_pct_of_limit": fr["short_trim_target_pct_of_limit"],
        "forced_reduction_total_short_immediate_threshold_pct_of_limit": fr[
            "total_short_immediate_threshold_pct_of_limit"
        ],
        "drawdown_velocity_window_minutes": dv["window_minutes"],
        "drawdown_velocity_threshold_pct_of_daily_limit": dv["threshold_pct_of_daily_limit"],
        "multi_rule_breach_simultaneous_deferred_rules_count": mrb[
            "simultaneous_deferred_rules_count"
        ],
        "cascade_max_steps": cas["max_steps"],
        "delta_buffer_secondary_check_buffer_factor": db["secondary_check_buffer_factor"],
    }


def load_breach_behavior_config(path: pathlib.Path) -> BreachBehaviorConfig:
    """Parse *path* as YAML and return a validated :class:`BreachBehaviorConfig`."""
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    return BreachBehaviorConfig.model_validate(_flatten_breach_behavior_yaml(raw))
