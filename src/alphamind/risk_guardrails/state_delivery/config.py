"""Configuration for the state-delivery rendering layer."""

from __future__ import annotations

import pathlib
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from alphamind.config.loaders import read_yaml_file


class StateDeliveryConfig(BaseModel):
    """Operator-tunable knobs for the state-delivery renderers."""

    model_config = ConfigDict(frozen=True)

    recent_engine_actions_lookback_invocations: Annotated[int, Field(gt=0)]
    correlation_state_min_position_count: Annotated[int, Field(gt=0)]
    dependency_risk_flag_min_position_count: Annotated[int, Field(gt=0)]
    abandoned_window_lookback_invocations: Annotated[int, Field(gt=0)]


def _flatten_state_delivery_yaml(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten the nested YAML structure into the flat field-name keys."""
    sd: dict[str, Any] = raw["state_delivery"]
    rea: dict[str, Any] = sd["recent_engine_actions"]
    cs: dict[str, Any] = sd["correlation_state"]
    drf: dict[str, Any] = sd["dependency_risk_flag"]
    aw: dict[str, Any] = sd["abandoned_window"]
    return {
        "recent_engine_actions_lookback_invocations": rea["lookback_invocations"],
        "correlation_state_min_position_count": cs["min_position_count"],
        "dependency_risk_flag_min_position_count": drf["min_position_count"],
        "abandoned_window_lookback_invocations": aw["lookback_invocations"],
    }


def load_state_delivery_config(path: pathlib.Path) -> StateDeliveryConfig:
    """Parse *path* as YAML and return a validated :class:`StateDeliveryConfig`."""
    return StateDeliveryConfig.model_validate(_flatten_state_delivery_yaml(read_yaml_file(path)))
