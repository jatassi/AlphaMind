"""Unit tests for the pre_close projected-completion timing guard (ALP-774).

The guard ``_warn_if_pre_close_projected_late`` emits a WARNING when a
pre_close invocation's rolling-average projected completion falls after the
NYSE session close. Tests use a real in-memory SQLite DB (same FK topology
as production) and synthetic latency rows to drive the projection arithmetic.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

import alphamind.state.tables  # noqa: F401 — registers FK target tables
from alphamind.config.models.run_types import RunType
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.scheduler.orchestrator import _warn_if_pre_close_projected_late
from alphamind.state.invocation_context.records import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.tables.invocations import InvocationRow

# 2026-06-02 is a Tuesday — a normal NYSE trading day (close 16:00 ET = 20:00 UTC).
_TRADING_DAY = datetime(2026, 6, 2, tzinfo=UTC)
_SESSION_CLOSE_UTC = datetime(2026, 6, 2, 20, 0, 0, tzinfo=UTC)
_XNYS = "XNYS"


def _make_scheduler_config() -> SchedulerConfig:
    return SchedulerConfig.model_validate(
        {
            "timezone": "US/Eastern",
            "max_instances": 1,
            "overlap_dedup_lookback_minutes": 30,
            "emergency_poll_interval_seconds": 5,
            "market_calendar_exchange": _XNYS,
            "supervisor_shutdown_timeout_seconds": 10,
            "control_port": 8765,
            "triggers": {
                "pre_open": "0 9 * * mon-fri",
                "market_hours_rolling": "0 13 * * mon-fri",
                "pre_close": "0 15 * * mon-fri",
                "weekend_sunday": "0 18 * * sun",
            },
        }
    )


def _make_sync_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "test.db"
    engine = make_engine(str(db_path))
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as sess:
        sess.add(
            process_lifetime_record_to_row(
                ProcessLifetimeRecord(
                    process_lifetime_id="proc-test-timing",
                    process_role="pipeline",
                    process_start_at="2026-06-02T15:00:00Z",
                    process_pid=1,
                    hostname="test-host",
                    git_sha="a" * 40,
                    git_branch="main",
                    git_dirty=False,
                    python_version="3.13.1",
                    pip_freeze_hash="0" * 64,
                    pip_freeze_snapshot_path="/tmp/pip.txt",
                    anthropic_sdk_version="0.40.0",
                    claude_agent_sdk_version="0.1.69",
                    os_release="Linux",
                )
            )
        )
        sess.commit()
    return make_session_factory(engine)


def _insert_invocation(
    factory: sessionmaker[Session],
    *,
    invocation_id: str,
    start_at: datetime,
    duration_seconds: float,
) -> None:
    """Insert a completed invocation row with the given start time and duration."""
    phase2_at = start_at + timedelta(seconds=duration_seconds)
    row = InvocationRow(
        invocation_id=invocation_id,
        process_lifetime_id="proc-test-timing",
        start_at=start_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        phase2_completed_at=phase2_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        trigger_type="scheduled",
        trigger_source="pre_close",
        trigger_reason="0 15 * * mon-fri",
        git_sha_at_invocation="a" * 40,
        active_profile="base",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="{}",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/snap.yaml",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/cal.json",
        data_source_freshness_json="{}",
    )
    with factory() as sess:
        sess.add(row)
        sess.commit()


class TestPreCloseTimingGuard:
    def test_non_pre_close_run_type_is_noop(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        factory = _make_sync_factory(tmp_path)
        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.orchestrator"):
            _warn_if_pre_close_projected_late(
                firing_run_type=RunType.pre_open,
                now=_TRADING_DAY.replace(hour=9),
                sync_session_factory=factory,
                scheduler_config=_make_scheduler_config(),
            )
        assert not any("pre_close timing" in r.message for r in caplog.records)

    def test_no_prior_invocations_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        factory = _make_sync_factory(tmp_path)
        # Empty DB — no completed invocations yet.
        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.orchestrator"):
            _warn_if_pre_close_projected_late(
                firing_run_type=RunType.pre_close,
                now=_TRADING_DAY.replace(hour=19),  # 15:00 ET
                sync_session_factory=factory,
                scheduler_config=_make_scheduler_config(),
            )
        assert not any("pre_close timing" in r.message for r in caplog.records)

    def test_on_time_projection_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        factory = _make_sync_factory(tmp_path)
        # Three prior invocations each taking 39 minutes (2340 s).
        for i in range(3):
            _insert_invocation(
                factory,
                invocation_id=f"inv-on-time-{i}",
                start_at=_TRADING_DAY.replace(hour=19),
                duration_seconds=2340.0,
            )
        # Fire at 15:00 ET (19:00 UTC); avg duration 39 min → projected end 19:39 UTC < 20:00 UTC.
        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.orchestrator"):
            _warn_if_pre_close_projected_late(
                firing_run_type=RunType.pre_close,
                now=_TRADING_DAY.replace(hour=19),
                sync_session_factory=factory,
                scheduler_config=_make_scheduler_config(),
            )
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "pre_close timing" in r.message
        ]
        assert not warning_records, f"unexpected WARNING: {[r.message for r in warning_records]}"

    def test_projected_miss_emits_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        factory = _make_sync_factory(tmp_path)
        # Three prior invocations each taking 39 minutes (2340 s).
        for i in range(3):
            _insert_invocation(
                factory,
                invocation_id=f"inv-miss-{i}",
                start_at=_TRADING_DAY.replace(hour=19, minute=30),
                duration_seconds=2340.0,
            )
        # Fire at 15:30 ET (19:30 UTC); avg duration 39 min → projected end 20:09 UTC > 20:00 UTC.
        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.orchestrator"):
            _warn_if_pre_close_projected_late(
                firing_run_type=RunType.pre_close,
                now=_TRADING_DAY.replace(hour=19, minute=30),
                sync_session_factory=factory,
                scheduler_config=_make_scheduler_config(),
            )
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "pre_close timing" in r.message
        ]
        assert warning_records, "expected a WARNING for projected post-close completion"
        assert "orders may submit post-close" in warning_records[0].message

    def test_warning_contains_overshoot_and_invocation_count(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        factory = _make_sync_factory(tmp_path)
        for i in range(2):
            _insert_invocation(
                factory,
                invocation_id=f"inv-detail-{i}",
                start_at=_TRADING_DAY.replace(hour=19, minute=30),
                duration_seconds=2340.0,
            )
        with caplog.at_level(logging.WARNING, logger="alphamind.scheduler.orchestrator"):
            _warn_if_pre_close_projected_late(
                firing_run_type=RunType.pre_close,
                now=_TRADING_DAY.replace(hour=19, minute=30),
                sync_session_factory=factory,
                scheduler_config=_make_scheduler_config(),
            )
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "pre_close timing" in r.message
        ]
        assert warning_records
        msg = warning_records[0].message
        # Should mention the count of invocations used for the estimate.
        assert "2 recent invocations" in msg
