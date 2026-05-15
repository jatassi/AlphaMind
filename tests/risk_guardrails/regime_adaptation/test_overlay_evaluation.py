"""Tests for ``alphamind.risk_guardrails.regime_adaptation.overlay_evaluation``.

These two helpers wrap the regime-adaptation overlay activators behind a
filesystem-/DB-loading shell: ``evaluate_pre_event_decision`` reads the
event calendar from a config dir and runs the pre-event activator;
``evaluate_stress_decision`` fetches the composite-alert state via a
sync-session bridge and runs the stress activator. They lived inline in
``scheduler/orchestrator.py`` until ALP-472.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


def _write_minimal_event_calendar(path: Path) -> None:
    """Write a minimal event_calendar.yaml the loader accepts."""
    path.write_text("events: []\nas_of: '2026-05-12T00:00:00Z'\n")


def _pre_event_overlay() -> Any:
    from alphamind.config.models.overlays import PreEventOverlay

    return PreEventOverlay.model_validate(
        {
            "activation": {"windows_before_event": 2, "events": ["fomc"]},
            "multipliers": {"position_max_size_pct": 0.8},
            "final_invocation_before_event": {"block_new_positions": True},
        }
    )


def _stress_overlay() -> Any:
    from alphamind.config.models.overlays import StressOverlay

    return StressOverlay.model_validate(
        {
            "activation": {"triggers": ["funding_stress_composite", "market_liquidity_score"]},
            "multipliers": {"position_max_size_pct": 0.5},
        }
    )


def _scheduler_config() -> Any:
    from alphamind.config.models.scheduler import SchedulerConfig

    return SchedulerConfig.model_validate(
        {
            "timezone": "US/Eastern",
            "max_instances": 1,
            "overlap_dedup_lookback_minutes": 30,
            "emergency_poll_interval_seconds": 5,
            "market_calendar_exchange": "XNYS",
            "supervisor_shutdown_timeout_seconds": 10,
            "triggers": {"market_hours_rolling": "30 9,11,13,15 * * mon-fri"},
        }
    )


def test_evaluate_pre_event_decision_loads_calendar_and_activator(tmp_path: Path) -> None:
    """``evaluate_pre_event_decision`` loads the calendar and returns the activator result."""
    from alphamind.config.models.overlays import Overlay
    from alphamind.risk_guardrails.regime_adaptation.overlay_evaluation import (
        evaluate_pre_event_decision,
    )

    _write_minimal_event_calendar(tmp_path / "event_calendar.yaml")

    overlays_map = {
        Overlay.pre_event: _pre_event_overlay(),
        Overlay.stress: _stress_overlay(),
    }

    decision = evaluate_pre_event_decision(
        now=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
        config_dir=tmp_path,
        overlays_map=overlays_map,
        scheduler_config=_scheduler_config(),
    )

    # Empty calendar → no pre-event activation
    assert decision.is_active is False


def test_evaluate_pre_event_decision_wrong_overlay_type_raises(tmp_path: Path) -> None:
    """Wrong overlay type in the map raises ``TypeError``."""
    from alphamind.config.models.overlays import Overlay
    from alphamind.risk_guardrails.regime_adaptation.overlay_evaluation import (
        evaluate_pre_event_decision,
    )

    _write_minimal_event_calendar(tmp_path / "event_calendar.yaml")

    overlays_map: dict[Overlay, Any] = {
        Overlay.pre_event: _stress_overlay(),  # wrong type
        Overlay.stress: _stress_overlay(),
    }

    with pytest.raises(TypeError, match="not a PreEventOverlay"):
        evaluate_pre_event_decision(
            now=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
            config_dir=tmp_path,
            overlays_map=overlays_map,
            scheduler_config=_scheduler_config(),
        )


async def test_evaluate_stress_decision_runs_against_session_factory() -> None:
    """``evaluate_stress_decision`` fetches state via sync bridge and runs activator."""
    from alphamind._kernel.calibration import CalibrationState
    from alphamind.config.models.overlays import Overlay
    from alphamind.risk_guardrails.regime_adaptation.overlay_evaluation import (
        evaluate_stress_decision,
    )
    from alphamind.risk_guardrails.regime_adaptation.types import CompositeAlertState

    overlays_map = {
        Overlay.pre_event: _pre_event_overlay(),
        Overlay.stress: _stress_overlay(),
    }

    stub_state = CompositeAlertState(
        funding_stress_alert_active=False,
        funding_stress_calibration_state=CalibrationState.CALIBRATED,
        market_liquidity_alert_active=False,
        market_liquidity_calibration_state=CalibrationState.CALIBRATED,
        funding_stress_as_of=None,
        market_liquidity_as_of=None,
    )

    class _StubSession:
        async def run_sync(self, fn: Any) -> Any:
            return stub_state

        async def __aenter__(self) -> _StubSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    def _factory() -> _StubSession:
        return _StubSession()

    decision = await evaluate_stress_decision(
        session_factory=_factory,  # type: ignore[arg-type]
        overlays_map=overlays_map,
    )

    assert decision.is_active is False


async def test_evaluate_stress_decision_wrong_overlay_type_raises() -> None:
    """Wrong overlay type in the map raises ``TypeError``."""
    from alphamind.config.models.overlays import Overlay
    from alphamind.risk_guardrails.regime_adaptation.overlay_evaluation import (
        evaluate_stress_decision,
    )

    overlays_map: dict[Overlay, Any] = {
        Overlay.pre_event: _pre_event_overlay(),
        Overlay.stress: _pre_event_overlay(),  # wrong
    }

    with pytest.raises(TypeError, match="not a StressOverlay"):
        await evaluate_stress_decision(
            session_factory=MagicMock(),
            overlays_map=overlays_map,
        )
