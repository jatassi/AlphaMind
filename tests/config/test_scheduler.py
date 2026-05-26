"""Tests for ``SchedulerConfig`` runtime-knob fields (story 01 — ALP-442).

Story 01 adds three scheduler-runtime knobs on the existing
``SchedulerConfig`` model (per parent decision (D)):

* ``emergency_poll_interval_seconds`` (>=1) — story 04b polls activity-log
  for emergency-invocation requests at this cadence.
* ``market_calendar_exchange`` (non-empty) — story 04a consults
  ``exchange-calendars`` for the configured exchange.
* ``supervisor_shutdown_timeout_seconds`` (>=1) — story 01 / ``run()``
  per-task cancellation budget.

These tests pin the model's validation surface only. Resolver / loader
integration is already covered by ``test_loader.py`` /
``test_config_models.py``; this file owns the new fields' validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from alphamind.config.models.scheduler import SchedulerConfig

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _valid_raw() -> dict[str, Any]:
    return {
        "timezone": "US/Eastern",
        "max_instances": 1,
        "overlap_dedup_lookback_minutes": 30,
        "emergency_poll_interval_seconds": 5,
        "market_calendar_exchange": "XNYS",
        "supervisor_shutdown_timeout_seconds": 10,
        "triggers": {"pre_open": "0 9 * * mon-fri"},
    }


class TestNewFieldsParse:
    def test_round_trips_with_all_three_runtime_fields(self) -> None:
        config = SchedulerConfig.model_validate(_valid_raw())
        assert config.emergency_poll_interval_seconds == 5
        assert config.market_calendar_exchange == "XNYS"
        assert config.supervisor_shutdown_timeout_seconds == 10

    def test_shipped_scheduler_yaml_parses_with_new_fields(self) -> None:
        raw = cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / "scheduler.yaml").read_text()))
        config = SchedulerConfig.model_validate(raw)
        assert config.emergency_poll_interval_seconds >= 1
        assert config.market_calendar_exchange
        assert config.supervisor_shutdown_timeout_seconds >= 1


class TestNewFieldValidation:
    def test_rejects_emergency_poll_interval_zero(self) -> None:
        raw = _valid_raw()
        raw["emergency_poll_interval_seconds"] = 0
        with pytest.raises((ValueError, TypeError)):
            SchedulerConfig.model_validate(raw)

    def test_rejects_emergency_poll_interval_negative(self) -> None:
        raw = _valid_raw()
        raw["emergency_poll_interval_seconds"] = -3
        with pytest.raises((ValueError, TypeError)):
            SchedulerConfig.model_validate(raw)

    def test_rejects_empty_market_calendar_exchange(self) -> None:
        raw = _valid_raw()
        raw["market_calendar_exchange"] = ""
        with pytest.raises((ValueError, TypeError)):
            SchedulerConfig.model_validate(raw)

    def test_rejects_supervisor_shutdown_timeout_zero(self) -> None:
        raw = _valid_raw()
        raw["supervisor_shutdown_timeout_seconds"] = 0
        with pytest.raises((ValueError, TypeError)):
            SchedulerConfig.model_validate(raw)

    def test_rejects_supervisor_shutdown_timeout_negative(self) -> None:
        raw = _valid_raw()
        raw["supervisor_shutdown_timeout_seconds"] = -1
        with pytest.raises((ValueError, TypeError)):
            SchedulerConfig.model_validate(raw)


class TestControlPort:
    """ALP-664 — ``control_port`` knob backing the pipeline /control + /events surface."""

    def test_control_port_defaults_to_8765(self) -> None:
        config = SchedulerConfig.model_validate(_valid_raw())
        assert config.control_port == 8765

    def test_control_port_round_trips_explicit_value(self) -> None:
        raw = _valid_raw()
        raw["control_port"] = 9000
        config = SchedulerConfig.model_validate(raw)
        assert config.control_port == 9000

    def test_rejects_zero_control_port(self) -> None:
        raw = _valid_raw()
        raw["control_port"] = 0
        with pytest.raises((ValueError, TypeError)):
            SchedulerConfig.model_validate(raw)

    def test_rejects_control_port_above_max_tcp(self) -> None:
        raw = _valid_raw()
        raw["control_port"] = 70000
        with pytest.raises((ValueError, TypeError)):
            SchedulerConfig.model_validate(raw)
