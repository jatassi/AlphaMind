"""Tests for the borrow_accrual_tick_local_time knob on ContinuousMonitorConfig (ALP-718).

Verifies:
- The YAML file contains the new knob with default "16:00".
- ContinuousMonitorConfig parses a payload containing the knob.
- ContinuousMonitorConfig defaults to "16:00" when the knob is absent.
- ContinuousMonitorConfig rejects invalid time formats with a ValidationError.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).parent.parent.parent
CONTINUOUS_MONITOR_YAML = REPO_ROOT / "config" / "continuous_monitor.yaml"


def _load_yaml() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(CONTINUOUS_MONITOR_YAML.read_text()))


def _base_payload() -> dict[str, Any]:
    """Minimal valid payload for ContinuousMonitorConfig (all required fields)."""
    return {
        "breach_evaluation_cadence_seconds": 60,
        "greeks_refresh_interval_minutes": 15,
        "greeks_refresh_underlying_move_threshold_pct": 2.0,
        "underlying_stream_provider": "alpaca-iex",
        "max_reconnect_attempts": 5,
        "supervisor_shutdown_timeout_seconds": 5,
    }


class TestBorrowAccrualTickLocalTimeYaml:
    """The continuous_monitor.yaml file ships the new knob."""

    def test_yaml_contains_borrow_accrual_tick_local_time(self) -> None:
        data = _load_yaml()
        assert "borrow_accrual_tick_local_time" in data

    def test_yaml_default_is_1600(self) -> None:
        data = _load_yaml()
        assert data["borrow_accrual_tick_local_time"] == "16:00"


class TestContinuousMonitorConfigBorrowAccrualKnob:
    """ContinuousMonitorConfig validates / defaults / rejects borrow_accrual_tick_local_time."""

    def test_parses_when_knob_present(self) -> None:
        from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig

        payload = {**_base_payload(), "borrow_accrual_tick_local_time": "16:00"}
        config = ContinuousMonitorConfig.model_validate(payload)
        assert config.borrow_accrual_tick_local_time == "16:00"

    def test_defaults_to_1600_when_absent(self) -> None:
        from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig

        config = ContinuousMonitorConfig.model_validate(_base_payload())
        assert config.borrow_accrual_tick_local_time == "16:00"

    def test_yaml_file_parses_cleanly_with_new_knob(self) -> None:
        """The live YAML file (with the new knob) parses without error."""
        from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig

        data = _load_yaml()
        config = ContinuousMonitorConfig.model_validate(data)
        assert config.borrow_accrual_tick_local_time == "16:00"

    def test_accepts_various_valid_times(self) -> None:
        from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig

        for valid_time in ["00:00", "09:30", "16:00", "23:59"]:
            payload = {**_base_payload(), "borrow_accrual_tick_local_time": valid_time}
            config = ContinuousMonitorConfig.model_validate(payload)
            assert config.borrow_accrual_tick_local_time == valid_time

    @pytest.mark.parametrize("invalid_time", ["25:00", "4pm", "16:0", "9:30", "1600", "16:60"])
    def test_rejects_invalid_time_formats(self, invalid_time: str) -> None:
        from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig

        payload = {**_base_payload(), "borrow_accrual_tick_local_time": invalid_time}
        with pytest.raises(ValidationError):
            ContinuousMonitorConfig.model_validate(payload)

    def test_invalid_time_diagnostic_matches_session_window_diagnostic(self) -> None:
        """ALP-715 review F6: the HH:MM error message is canonical across both
        ``borrow_accrual_tick_local_time`` and ``SessionWindow.open``/``.close``,
        so an operator sees the same message regardless of which knob mis-parsed.
        """
        from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
        from alphamind.config.models.venue import SessionWindow

        invalid = "9:30"
        payload = {**_base_payload(), "borrow_accrual_tick_local_time": invalid}
        with pytest.raises(ValidationError) as monitor_err:
            ContinuousMonitorConfig.model_validate(payload)
        with pytest.raises(ValidationError) as window_err:
            SessionWindow.model_validate({"open": invalid, "close": "16:00"})

        marker = f"Time {invalid!r} must match HH:MM in 24-hour clock"
        assert marker in str(monitor_err.value)
        assert marker in str(window_err.value)
