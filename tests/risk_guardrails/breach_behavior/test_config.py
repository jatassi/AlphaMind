import pathlib
from typing import Any

import pytest
import yaml

from alphamind.risk_guardrails.breach_behavior.config import (
    BreachBehaviorConfig,
    load_breach_behavior_config,
)

_CONFIG_PATH = pathlib.Path(__file__).parents[3] / "config" / "breach_behavior.yaml"


def _valid_flat_payload() -> dict[str, Any]:
    return {
        "forced_reduction_short_trim_target_pct_of_limit": 95.0,
        "forced_reduction_total_short_immediate_threshold_pct_of_limit": 110.0,
        "drawdown_velocity_window_minutes": 30,
        "drawdown_velocity_threshold_pct_of_daily_limit": 60.0,
        "multi_rule_breach_simultaneous_deferred_rules_count": 3,
        "cascade_max_steps": 8,
        "delta_buffer_secondary_check_buffer_factor": 1.0,
        "emergency_invocation_cooldown_minutes": 30,
    }


def test_yaml_parses_documented_defaults() -> None:
    cfg = load_breach_behavior_config(_CONFIG_PATH)
    assert cfg.forced_reduction_short_trim_target_pct_of_limit == 95
    assert cfg.forced_reduction_total_short_immediate_threshold_pct_of_limit == 110
    assert cfg.drawdown_velocity_window_minutes == 30
    assert cfg.drawdown_velocity_threshold_pct_of_daily_limit == 60
    assert cfg.multi_rule_breach_simultaneous_deferred_rules_count == 3
    assert cfg.cascade_max_steps == 8
    assert cfg.delta_buffer_secondary_check_buffer_factor == 1.0
    assert cfg.emergency_invocation_cooldown_minutes == 30


@pytest.mark.parametrize(
    "field,value",
    [
        ("forced_reduction_short_trim_target_pct_of_limit", 0.0),
        ("forced_reduction_short_trim_target_pct_of_limit", -1.0),
        ("drawdown_velocity_window_minutes", 0),
        ("drawdown_velocity_window_minutes", -1),
        ("drawdown_velocity_threshold_pct_of_daily_limit", 0.0),
        ("drawdown_velocity_threshold_pct_of_daily_limit", -1.0),
        ("cascade_max_steps", 0),
        ("cascade_max_steps", -1),
        ("delta_buffer_secondary_check_buffer_factor", 0.0),
        ("delta_buffer_secondary_check_buffer_factor", -1.0),
        ("emergency_invocation_cooldown_minutes", 0),
        ("emergency_invocation_cooldown_minutes", -1),
    ],
)
def test_non_positive_value_fails_with_field_path(field: str, value: float) -> None:
    payload = _valid_flat_payload()
    payload[field] = value
    with pytest.raises((ValueError, TypeError)) as exc_info:
        BreachBehaviorConfig.model_validate(payload)
    assert field in str(exc_info.value)


def test_total_short_immediate_threshold_at_exactly_100_fails() -> None:
    payload = _valid_flat_payload()
    payload["forced_reduction_total_short_immediate_threshold_pct_of_limit"] = 100.0
    with pytest.raises((ValueError, TypeError)) as exc_info:
        BreachBehaviorConfig.model_validate(payload)
    assert "forced_reduction_total_short_immediate_threshold_pct_of_limit" in str(exc_info.value)


def test_multi_rule_breach_simultaneous_deferred_rules_count_at_1_fails() -> None:
    payload = _valid_flat_payload()
    payload["multi_rule_breach_simultaneous_deferred_rules_count"] = 1
    with pytest.raises((ValueError, TypeError)) as exc_info:
        BreachBehaviorConfig.model_validate(payload)
    assert "multi_rule_breach_simultaneous_deferred_rules_count" in str(exc_info.value)


@pytest.mark.parametrize(
    "missing_block",
    [
        "forced_reduction",
        "drawdown_velocity",
        "multi_rule_breach",
        "cascade",
        "delta_buffer",
    ],
)
def test_missing_required_block_fails_with_field_path(
    missing_block: str, tmp_path: pathlib.Path
) -> None:
    raw = yaml.safe_load(_CONFIG_PATH.read_text())
    del raw["breach_behavior"][missing_block]
    broken = tmp_path / "breach_behavior.yaml"
    broken.write_text(yaml.safe_dump(raw))
    with pytest.raises(KeyError) as exc_info:
        load_breach_behavior_config(broken)
    assert missing_block in str(exc_info.value)
