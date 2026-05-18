import pathlib
from typing import Any

import pytest

from alphamind.portfolio_state import PortfolioStateConfig, load_portfolio_state_config

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "portfolio_state.yaml"


def test_yaml_parses_documented_defaults() -> None:
    cfg = load_portfolio_state_config(_CONFIG_PATH)
    assert cfg.pm_decision_log_sliding_window_invocations == 3
    assert cfg.thesis_resolutions_lookback_trading_days == 20
    assert cfg.thesis_quality_aggregates_trailing_windows_days == (5, 20)
    assert cfg.snapshot_freshness_max_phase1_to_snapshot_seconds == 30.0
    assert cfg.snapshot_freshness_max_price_age_seconds == 900.0
    assert cfg.snapshot_freshness_max_option_price_age_seconds == 2100.0


@pytest.mark.parametrize(
    "field,value",
    [
        ("pm_decision_log_sliding_window_invocations", -1),
        ("pm_decision_log_sliding_window_invocations", 0),
        ("thesis_resolutions_lookback_trading_days", -1),
        ("thesis_resolutions_lookback_trading_days", 0),
        ("snapshot_freshness_max_phase1_to_snapshot_seconds", -1.0),
        ("snapshot_freshness_max_phase1_to_snapshot_seconds", 0.0),
        ("snapshot_freshness_max_price_age_seconds", -1.0),
        ("snapshot_freshness_max_price_age_seconds", 0.0),
        ("snapshot_freshness_max_option_price_age_seconds", -1.0),
        ("snapshot_freshness_max_option_price_age_seconds", 0.0),
    ],
)
def test_non_positive_value_fails_with_field_path(field: str, value: int | float) -> None:
    valid: dict[str, Any] = {
        "pm_decision_log_sliding_window_invocations": 3,
        "thesis_resolutions_lookback_trading_days": 20,
        "thesis_quality_aggregates_trailing_windows_days": (5, 20),
        "snapshot_freshness_max_phase1_to_snapshot_seconds": 30.0,
        "snapshot_freshness_max_price_age_seconds": 900.0,
        "snapshot_freshness_max_option_price_age_seconds": 2100.0,
    }
    valid[field] = value
    with pytest.raises((ValueError, TypeError)) as exc_info:
        PortfolioStateConfig(**valid)
    error_text = str(exc_info.value)
    assert field in error_text


def test_empty_trailing_windows_days_fails() -> None:
    with pytest.raises((ValueError, TypeError)) as exc_info:
        PortfolioStateConfig(
            pm_decision_log_sliding_window_invocations=3,
            thesis_resolutions_lookback_trading_days=20,
            thesis_quality_aggregates_trailing_windows_days=(),
            snapshot_freshness_max_phase1_to_snapshot_seconds=30.0,
            snapshot_freshness_max_price_age_seconds=900.0,
            snapshot_freshness_max_option_price_age_seconds=2100.0,
        )
    assert "thesis_quality_aggregates_trailing_windows_days" in str(exc_info.value)


def test_negative_element_in_trailing_windows_days_fails() -> None:
    with pytest.raises((ValueError, TypeError)) as exc_info:
        PortfolioStateConfig(
            pm_decision_log_sliding_window_invocations=3,
            thesis_resolutions_lookback_trading_days=20,
            thesis_quality_aggregates_trailing_windows_days=(-1, 20),
            snapshot_freshness_max_phase1_to_snapshot_seconds=30.0,
            snapshot_freshness_max_price_age_seconds=900.0,
            snapshot_freshness_max_option_price_age_seconds=2100.0,
        )
    assert "thesis_quality_aggregates_trailing_windows_days" in str(exc_info.value)
