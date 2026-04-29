import pathlib
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from alphamind.risk_guardrails.state_delivery.config import (
    StateDeliveryConfig,
    load_state_delivery_config,
)

_CONFIG_PATH = pathlib.Path(__file__).parents[3] / "config" / "state_delivery.yaml"


def test_yaml_parses_documented_defaults() -> None:
    cfg = load_state_delivery_config(_CONFIG_PATH)
    assert cfg.recent_engine_actions_lookback_invocations == 1
    assert cfg.correlation_state_min_position_count == 3
    assert cfg.dependency_risk_flag_min_position_count == 3
    assert cfg.abandoned_window_lookback_invocations == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("recent_engine_actions_lookback_invocations", -1),
        ("recent_engine_actions_lookback_invocations", 0),
        ("correlation_state_min_position_count", -1),
        ("correlation_state_min_position_count", 0),
        ("dependency_risk_flag_min_position_count", -1),
        ("dependency_risk_flag_min_position_count", 0),
        ("abandoned_window_lookback_invocations", -1),
        ("abandoned_window_lookback_invocations", 0),
    ],
)
def test_non_positive_value_fails_with_field_path(field: str, value: int) -> None:
    valid: dict[str, Any] = {
        "recent_engine_actions_lookback_invocations": 1,
        "correlation_state_min_position_count": 3,
        "dependency_risk_flag_min_position_count": 3,
        "abandoned_window_lookback_invocations": 1,
    }
    valid[field] = value
    with pytest.raises(ValidationError) as exc_info:
        StateDeliveryConfig.model_validate(valid)
    assert field in str(exc_info.value)


@pytest.mark.parametrize(
    "missing_block",
    [
        "recent_engine_actions",
        "correlation_state",
        "dependency_risk_flag",
        "abandoned_window",
    ],
)
def test_missing_required_block_fails_with_field_path(
    missing_block: str, tmp_path: pathlib.Path
) -> None:
    raw = yaml.safe_load(_CONFIG_PATH.read_text())
    del raw["state_delivery"][missing_block]
    broken = tmp_path / "state_delivery.yaml"
    broken.write_text(yaml.safe_dump(raw))
    with pytest.raises(KeyError) as exc_info:
        load_state_delivery_config(broken)
    assert missing_block in str(exc_info.value)
