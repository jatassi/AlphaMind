"""Tests for ``alphamind.scheduler.driver`` — APScheduler driver (story 04a).

The driver wires one cron job per ``scheduler.yaml`` trigger key, gates
each fire on (i) the NYSE trading calendar for weekday triggers and (ii)
the overlap-dedup window across rolling triggers, and dispatches
``run_invocation(trigger_type="scheduled", ...)`` from story 03b on every
fire that survives the guards. The driver registers as a
``PipelineSupervisor`` task from story 01 so SIGINT / SIGTERM cleanly
shut it down. On per-invocation exceptions, the driver catches and logs
(the daemon keeps running per parent decision (H)).

Tests stub ``run_invocation`` and the calendar/dedup helpers so the
driver's wiring is exercised without hitting the database or the
Anthropic API. Manual end-to-end execution is the responsibility of
story 05's verify script.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.run_types import RunType
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.config.models.venue import (
    Alpaca,
    AlpacaCredentials,
    SessionHours,
    SessionWindow,
    VenueConfig,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    invocation_record_to_row,
)
from tests.scheduler.conftest import (
    SHIPPED_CONFIG_DIR,
    _make_venue_config,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers shared across test classes
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)


def _make_scheduler_config(
    *,
    triggers: dict[str, str] | None = None,
    overlap_dedup_lookback_minutes: int = 30,
    market_calendar_exchange: str = "XNYS",
    supervisor_shutdown_timeout_seconds: int = 10,
) -> SchedulerConfig:
    """Build a ``SchedulerConfig`` for tests with sensible defaults."""
    if triggers is None:
        triggers = {
            "pre_open": "0 9 * * mon-fri",
            "market_hours_rolling": "30 9,11,13,15 * * mon-fri",
            "pre_close": "30 15 * * mon-fri",
            "off_hours_rolling": "0 0,4,8,20 * * mon-fri",
            "weekend_saturday": "0 10 * * sat",
            "weekend_sunday": "0 18 * * sun",
        }
    return SchedulerConfig(
        timezone="US/Eastern",
        max_instances=1,
        overlap_dedup_lookback_minutes=overlap_dedup_lookback_minutes,
        emergency_poll_interval_seconds=5,
        market_calendar_exchange=market_calendar_exchange,
        supervisor_shutdown_timeout_seconds=supervisor_shutdown_timeout_seconds,
        triggers=triggers,
    )


def _make_context(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> Any:
    """Compose the standard ``RunInvocationContext`` driver tests use."""
    from alphamind.scheduler.run_context import RunInvocationContext

    return RunInvocationContext(
        session_factory=session_factory,
        sync_session_factory="sync-factory-sentinel",  # type: ignore[arg-type]
        process_lifetime_id="proc-driver-1",
        archive_root=tmp_path / "archive",
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=tmp_path / ".env",
        venue_config=_make_venue_config(),
        execution_mode=ExecutionMode.paper,
    )


def _make_invocation_record(
    *,
    invocation_id: str,
    start_at: str,
    phase2_completed_at: str | None,
) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id="proc-driver-1",
        start_at=start_at,
        phase1_completed_at=None,
        phase2_completed_at=phase2_completed_at,
        trigger_type="scheduled",
        trigger_source="market_hours_rolling",
        trigger_reason="30 9,11,13,15 * * mon-fri",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/resolved_config.json"
        ),
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/data_calibration_state.json"
        ),
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


# ---------------------------------------------------------------------------
# 1. Trigger-key → RunType resolution
# ---------------------------------------------------------------------------


class TestRegisterPipelineJobsTriggerResolution:
    def test_registers_one_job_per_trigger_with_id_matching_key(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """One ``APScheduler`` job per yaml trigger; job ID == trigger key."""
        from alphamind.scheduler.driver import register_pipeline_jobs

        cfg = _make_scheduler_config()
        sched = AsyncIOScheduler(timezone=cfg.timezone)
        try:
            register_pipeline_jobs(
                scheduler=sched,
                scheduler_config=cfg,
                context=_make_context(session_factory=async_factory, tmp_path=tmp_path),
            )
            registered = {job.id for job in sched.get_jobs()}
        finally:
            # AsyncIOScheduler.shutdown requires a running loop only when
            # the scheduler has been started; we never call start() here so
            # we leave it alone.
            pass

        assert registered == set(cfg.triggers)

    def test_raises_value_error_when_trigger_key_has_no_run_type(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """A yaml typo (e.g. ``pre_opn``) must abort registration with ``ValueError``."""
        from alphamind.scheduler.driver import register_pipeline_jobs

        cfg = _make_scheduler_config(
            triggers={
                # The first key is valid; the second is a typo that must fail.
                "pre_open": "0 9 * * mon-fri",
                "pre_opn": "0 9 * * mon-fri",
            }
        )
        sched = AsyncIOScheduler(timezone=cfg.timezone)
        with pytest.raises(ValueError, match="pre_opn"):
            register_pipeline_jobs(
                scheduler=sched,
                scheduler_config=cfg,
                context=_make_context(session_factory=async_factory, tmp_path=tmp_path),
            )


# ---------------------------------------------------------------------------
# Shared helpers for the job-wrapper tests
# ---------------------------------------------------------------------------


def _patch_run_invocation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured: dict[str, Any] | None = None,
    raise_exc: Exception | None = None,
    invocation_id: str = "inv-driver-1",
) -> dict[str, Any]:
    """Stub ``alphamind.scheduler.driver.run_invocation`` with a recording fake."""
    from alphamind.execution.write_paths.phase1 import (
        Phase1Summary,
    )
    from alphamind.scheduler import driver as module
    from alphamind.scheduler.orchestrator import InvocationSummary

    state: dict[str, Any] = {"calls": 0, "kwargs": []} if captured is None else captured

    async def _stub(**kwargs: Any) -> InvocationSummary:
        state["calls"] = state.get("calls", 0) + 1
        state.setdefault("kwargs", []).append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        return InvocationSummary(
            invocation_id=invocation_id,
            trigger_type=kwargs["trigger_type"],
            trigger_source=kwargs.get("trigger_source", "scheduled"),
            firing_run_type=kwargs["firing_run_type"],
            phase1_summary=Phase1Summary(
                fills_processed=0,
                fills_quarantined=0,
                ca_activities_processed=0,
                reconciliation_alerts=0,
            ),
            commands_submitted=0,
            commands_rejected=0,
            staleness_flag=False,
            duration_seconds=0.25,
        )

    monkeypatch.setattr(module, "run_invocation", _stub)
    return state


# ---------------------------------------------------------------------------
# 2. ``_make_scheduled_job`` — happy path dispatch + completion log
# ---------------------------------------------------------------------------


class TestMakeScheduledJobHappyPath:
    async def test_dispatches_run_invocation_and_logs_completion(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Happy-path fire dispatches ``run_invocation(scheduled, ...)`` and logs completion."""
        from alphamind.scheduler.driver import _make_scheduled_job

        state = _patch_run_invocation(monkeypatch)
        cfg = _make_scheduler_config()
        # weekend_saturday bypasses both gates — easiest pure-dispatch fixture.
        job = _make_scheduled_job(
            trigger_key="weekend_saturday",
            run_type=RunType.weekend_saturday,
            cron_expression=cfg.triggers["weekend_saturday"],
            scheduler_config=cfg,
            context=_make_context(session_factory=async_factory, tmp_path=tmp_path),
        )

        with caplog.at_level(logging.INFO, logger="alphamind.scheduler.driver"):
            await job()

        assert state["calls"] == 1
        kwargs = state["kwargs"][0]
        assert kwargs["trigger_type"] == "scheduled"
        assert kwargs["trigger_source"] == "weekend_saturday"
        assert kwargs["trigger_reason"] == cfg.triggers["weekend_saturday"]
        assert kwargs["firing_run_type"] is RunType.weekend_saturday
        assert kwargs["context"].process_lifetime_id == "proc-driver-1"

        completion_messages = [
            record.getMessage() for record in caplog.records if "completed" in record.getMessage()
        ]
        assert completion_messages, "expected a 'completed' log line after a successful fire"
        assert "weekend_saturday" in completion_messages[0]
        assert "inv-driver-1" in completion_messages[0]


# ---------------------------------------------------------------------------
# 3. Market-calendar guard
# ---------------------------------------------------------------------------


class TestMakeScheduledJobMarketCalendarGuard:
    async def test_pre_open_skipped_on_non_trading_day(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``pre_open`` on Christmas Day must log skip and never dispatch."""
        from alphamind.scheduler import driver as module
        from alphamind.scheduler.driver import _make_scheduled_job

        state = _patch_run_invocation(monkeypatch)
        # Force the wrapper's "now" to a known non-trading day (2026-12-25 is
        # Friday Christmas Day — XNYS closed).
        non_trading_day = datetime(2026, 12, 25, 9, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(module, "_now_utc", lambda: non_trading_day, raising=False)

        cfg = _make_scheduler_config()
        job = _make_scheduled_job(
            trigger_key="pre_open",
            run_type=RunType.pre_open,
            cron_expression=cfg.triggers["pre_open"],
            scheduler_config=cfg,
            context=_make_context(session_factory=async_factory, tmp_path=tmp_path),
        )

        with caplog.at_level(logging.INFO, logger="alphamind.scheduler.driver"):
            await job()

        assert state["calls"] == 0
        skip_messages = [
            record.getMessage()
            for record in caplog.records
            if "skipped" in record.getMessage() and "non-trading" in record.getMessage()
        ]
        assert skip_messages, "expected a 'skipped — non-trading day' log line"
        assert "pre_open" in skip_messages[0]

    async def test_weekend_saturday_dispatches_on_non_trading_day(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Per parent decision (E), weekend triggers bypass the calendar gate."""
        from alphamind.scheduler import driver as module
        from alphamind.scheduler.driver import _make_scheduled_job

        state = _patch_run_invocation(monkeypatch)
        # Christmas Day 2026 — exchange closed. Weekend trigger must still fire.
        non_trading_day = datetime(2026, 12, 25, 10, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(module, "_now_utc", lambda: non_trading_day, raising=False)

        cfg = _make_scheduler_config()
        job = _make_scheduled_job(
            trigger_key="weekend_saturday",
            run_type=RunType.weekend_saturday,
            cron_expression=cfg.triggers["weekend_saturday"],
            scheduler_config=cfg,
            context=_make_context(session_factory=async_factory, tmp_path=tmp_path),
        )

        await job()

        assert state["calls"] == 1
        assert state["kwargs"][0]["firing_run_type"] is RunType.weekend_saturday


# ---------------------------------------------------------------------------
# 4. Overlap-dedup guard
# ---------------------------------------------------------------------------


async def _insert_completed_invocation(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str,
    completed_at: datetime,
) -> None:
    """Insert a row with ``phase2_completed_at`` stamped at *completed_at*."""
    completed_iso = completed_at.isoformat().replace("+00:00", "Z")
    async with factory() as session:
        session.add(
            invocation_record_to_row(
                _make_invocation_record(
                    invocation_id=invocation_id,
                    start_at=completed_iso,
                    phase2_completed_at=completed_iso,
                )
            )
        )
        await session.commit()


class TestMakeScheduledJobDedupGuard:
    async def test_market_hours_rolling_skipped_when_recent_completion(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A completion 10 min ago with lookback=30 must suppress the rolling fire."""
        from alphamind.scheduler import driver as module
        from alphamind.scheduler.driver import _make_scheduled_job

        # Weekday trading day so the calendar gate is satisfied.
        now = datetime(2026, 5, 5, 13, 30, 0, tzinfo=UTC)  # Tue 2026-05-05
        monkeypatch.setattr(module, "_now_utc", lambda: now, raising=False)

        await _insert_completed_invocation(
            async_factory,
            invocation_id="inv-recent",
            completed_at=now - timedelta(minutes=10),
        )

        state = _patch_run_invocation(monkeypatch)
        cfg = _make_scheduler_config(overlap_dedup_lookback_minutes=30)
        job = _make_scheduled_job(
            trigger_key="market_hours_rolling",
            run_type=RunType.market_hours_rolling,
            cron_expression=cfg.triggers["market_hours_rolling"],
            scheduler_config=cfg,
            context=_make_context(session_factory=async_factory, tmp_path=tmp_path),
        )

        with caplog.at_level(logging.INFO, logger="alphamind.scheduler.driver"):
            await job()

        assert state["calls"] == 0
        skip_messages = [
            record.getMessage()
            for record in caplog.records
            if "skipped" in record.getMessage() and "dedup" in record.getMessage()
        ]
        assert skip_messages, "expected a 'skipped — dedup window' log line"
        assert "market_hours_rolling" in skip_messages[0]

    async def test_pre_close_dispatches_despite_recent_completion(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anchored ``pre_close`` bypasses the dedup window."""
        from alphamind.scheduler import driver as module
        from alphamind.scheduler.driver import _make_scheduled_job

        # Weekday trading day so the calendar gate is satisfied.
        now = datetime(2026, 5, 5, 19, 30, 0, tzinfo=UTC)
        monkeypatch.setattr(module, "_now_utc", lambda: now, raising=False)

        await _insert_completed_invocation(
            async_factory,
            invocation_id="inv-recent",
            completed_at=now - timedelta(minutes=10),
        )

        state = _patch_run_invocation(monkeypatch)
        cfg = _make_scheduler_config(overlap_dedup_lookback_minutes=30)
        job = _make_scheduled_job(
            trigger_key="pre_close",
            run_type=RunType.pre_close,
            cron_expression=cfg.triggers["pre_close"],
            scheduler_config=cfg,
            context=_make_context(session_factory=async_factory, tmp_path=tmp_path),
        )

        await job()

        assert state["calls"] == 1
        assert state["kwargs"][0]["firing_run_type"] is RunType.pre_close


# ---------------------------------------------------------------------------
# 5. Exception handling — daemon keeps running per parent decision (H)
# ---------------------------------------------------------------------------


class TestMakeScheduledJobExceptionSwallowed:
    async def test_run_invocation_exception_is_logged_and_swallowed(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An exception raised inside ``run_invocation`` must NOT propagate."""
        from alphamind.scheduler.driver import _make_scheduled_job

        _patch_run_invocation(
            monkeypatch,
            raise_exc=RuntimeError("synthetic-orchestrator-fault"),
        )
        cfg = _make_scheduler_config()
        job = _make_scheduled_job(
            trigger_key="weekend_saturday",  # bypass both gates so we reach dispatch.
            run_type=RunType.weekend_saturday,
            cron_expression=cfg.triggers["weekend_saturday"],
            scheduler_config=cfg,
            context=_make_context(session_factory=async_factory, tmp_path=tmp_path),
        )

        with caplog.at_level(logging.ERROR, logger="alphamind.scheduler.driver"):
            # The contract is: no exception propagates.
            await job()

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "expected an ERROR log record from log.exception"
        assert any(
            "weekend_saturday" in r.getMessage() and "failed" in r.getMessage()
            for r in error_records
        )
        # ``log.exception`` attaches a traceback; verify by inspecting exc_info.
        assert any(r.exc_info is not None for r in error_records)


# ---------------------------------------------------------------------------
# 6. ``run_pipeline_scheduler_task`` — the supervisor-facing task
# ---------------------------------------------------------------------------


def _make_pipeline_session() -> Any:
    """Build the ``PipelineSession`` the supervisor threads into tasks."""
    from alphamind.scheduler.session import PipelineSession

    return PipelineSession(
        process_lifetime_id="proc-driver-1",
        started_at=_NOW,
        mode="paper",
    )


class TestRunPipelineSchedulerTask:
    async def test_starts_logs_next_fire_and_shuts_down_on_cancel(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Task starts the scheduler, logs next-fire schedule, then shuts down cleanly."""
        from alphamind.scheduler.driver import run_pipeline_scheduler_task

        _patch_run_invocation(monkeypatch)
        cfg = _make_scheduler_config()
        session = _make_pipeline_session()

        with caplog.at_level(logging.INFO, logger="alphamind.scheduler.driver"):
            task = asyncio.create_task(
                run_pipeline_scheduler_task(
                    session,
                    scheduler_config=cfg,
                    context=_make_context(session_factory=async_factory, tmp_path=tmp_path),
                )
            )
            # Yield so the task gets a chance to start the scheduler and log
            # next-fire times before we cancel.
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        messages = [r.getMessage() for r in caplog.records]
        # One "next fire" log line per registered trigger (6 in default config).
        next_fire_lines = [m for m in messages if "next fire" in m.lower()]
        assert len(next_fire_lines) == len(cfg.triggers), (
            f"expected one 'next fire' log line per trigger; got "
            f"{len(next_fire_lines)} from {next_fire_lines}"
        )
        stopped_lines = [m for m in messages if "pipeline scheduler stopped" in m]
        assert stopped_lines, "expected a shutdown log line on cancellation"


# ---------------------------------------------------------------------------
# 7. ``__main__`` wiring — daemon mode must register the apscheduler task
# ---------------------------------------------------------------------------


class TestMainRegistersApschedulerTask:
    def test_daemon_mode_registers_apscheduler_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``python -m alphamind.scheduler run`` registers the ``apscheduler`` task."""
        from alphamind.scheduler import __main__ as module

        captured: dict[str, Any] = {"task_names": (), "supervisor_ran": False}

        # Stub the heavy infra so ``_run_daemon`` runs in isolation.
        async def _stub_process_lifetime(**_kwargs: Any) -> str:
            return "proc-daemon-1"

        monkeypatch.setattr(module, "record_process_lifetime", _stub_process_lifetime)

        import contextlib
        from typing import cast

        from alphamind.persistence.session import EnginePair

        @contextlib.asynccontextmanager
        async def _stub_engine_pair(path: str | None = None) -> Any:
            yield EnginePair(
                async_engine=cast(Any, object()),
                async_session_factory=cast(Any, object()),
                sync_engine=cast(Any, object()),
                sync_session_factory=cast(Any, object()),
            )

        monkeypatch.setattr(module, "engine_pair_context", _stub_engine_pair)
        monkeypatch.setattr(module, "configure_pipeline_logging", lambda: None)

        # Stub the venue loader so we don't hit the filesystem.

        creds = AlpacaCredentials(
            rest_url="https://paper-api.alpaca.markets",
            ws_url="wss://paper-api.alpaca.markets",
            api_key_env="ALPACA_PAPER_KEY",
            api_secret_env="ALPACA_PAPER_SECRET",
        )
        fake_venue = VenueConfig(
            alpaca=Alpaca(paper=creds, live=creds, rate_limit_per_minute=200),
            session_hours=SessionHours(
                regular=SessionWindow(open="09:30", close="16:00"),
                pre_market=SessionWindow(open="04:00", close="09:30"),
                after_hours=SessionWindow(open="16:00", close="20:00"),
            ),
        )
        monkeypatch.setattr(module, "_load_venue_config", lambda config_dir: fake_venue)

        # Stub the supervisor's ``run`` to capture the registered task names.
        from alphamind.scheduler.supervisor import PipelineSupervisor

        async def _stub_run(self: PipelineSupervisor) -> None:
            captured["task_names"] = self.task_names()
            captured["supervisor_ran"] = True

        monkeypatch.setattr(PipelineSupervisor, "run", _stub_run)

        module.main(argv=["run", "--mode", "paper"])

        assert captured["supervisor_ran"] is True
        assert "apscheduler" in captured["task_names"]


def test_schema_trigger_types_mirrors_wire_run_type_literal() -> None:
    """``_SCHEMA_TRIGGER_TYPES`` must stay in sync with the wire vocabulary (ALP-755).

    The next-trigger preview filters trigger keys against this set; it exists
    only to keep keys outside the schema's ``_RunType`` literal off the wire.
    Once a run type joins ``_RunType`` it must also be previewable here, or a
    weekend trigger silently stops updating the operator console. Deriving the
    expectation from ``_RunType`` itself guards against future drift.
    """
    from typing import get_args

    from alphamind.scheduler.control.models import _RunType
    from alphamind.scheduler.driver import _SCHEMA_TRIGGER_TYPES

    assert set(get_args(_RunType)) == _SCHEMA_TRIGGER_TYPES
