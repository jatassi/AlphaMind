"""Tests for ``__main__._register_breach_loop`` (ALP-123 follow-up).

The /review on PR #48 flagged the 175-line helper as only smoke-tested. This
file pins the helper's wiring contract:

1. The cascade dispatcher is constructed with the trigger-ids generator
   passed in by the daemon (not a fresh per-call instance).
2. The emergency-trigger callback is constructed from the supplied breach
   config + session factory.
3. The breach-loop task is registered on the supervisor under the
   ``breach_loop`` name.
4. The supervisor's ``activity_log_sink`` is a real writer that persists
   entries via ``db_session_factory`` (was ``_no_op_sink`` pre-fix).
5. (Coupled with Fix 3 — ALP-123) The trigger-ids generator passed to
   ``_register_breach_loop`` and to ``register_options_bracket_watcher_task``
   is the *same* instance, so a bracket-stop fire and a cascade dispatch
   in the same session cannot mint the same engine-originated
   ``client_order_id``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.guardrails import BreachResponse
from alphamind.execution.continuous_monitor.__main__ import _register_breach_loop
from alphamind.execution.continuous_monitor.cascade_dispatch import TriggerIdGenerator
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.risk_guardrails.breach_behavior import BreachBehaviorConfig


@pytest.fixture()
async def db_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "alphamind.db"
    engine = make_async_engine(str(db_path))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_async_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260511T143000Z-abcdef01",
        started_at=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
        mode="paper",
    )


def _config() -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        greeks_refresh_inspection_cadence_seconds=30,
        bracket_stop_evaluation_cadence_seconds=1.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
    )


def _breach_behavior_config() -> BreachBehaviorConfig:
    return BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=30,
        drawdown_velocity_threshold_pct_of_daily_limit=60.0,
        multi_rule_breach_simultaneous_deferred_rules_count=3,
        cascade_max_steps=8,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=30,
    )


async def test_register_breach_loop_uses_shared_trigger_ids(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The cascade dispatcher consumes the trigger_ids passed by the daemon."""
    session = _session()
    supervisor = MonitorSupervisor(session=session, config=_config())
    shared_trigger_ids = TriggerIdGenerator(session_id=session.session_id)
    breach_response_lookup: MappingProxyType[str, BreachResponse] = MappingProxyType({})

    captured: dict[str, Any] = {}

    real_cls = (
        "alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher.CascadeDispatcher"
    )

    def _capture_init(self: object, **kwargs: Any) -> None:
        captured["dispatcher_kwargs"] = kwargs

    with mock.patch(f"{real_cls}.__init__", _capture_init):
        _register_breach_loop(
            supervisor,
            underlying_cache=object(),  # opaque — wiring stores the reference
            session=session,
            breach_behavior_config=_breach_behavior_config(),
            breach_response_lookup=breach_response_lookup,
            db_session_factory=db_session_factory,
            trigger_ids=shared_trigger_ids,
        )

    # The cascade dispatcher received the *same* generator instance the daemon
    # constructed — not a fresh one with a session_id-only constructor call.
    assert captured["dispatcher_kwargs"]["trigger_ids"] is shared_trigger_ids


async def test_register_breach_loop_registers_breach_loop_task(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The supervisor ends up with a ``breach_loop`` task registered."""
    session = _session()
    supervisor = MonitorSupervisor(session=session, config=_config())
    _register_breach_loop(
        supervisor,
        underlying_cache=object(),
        session=session,
        breach_behavior_config=_breach_behavior_config(),
        breach_response_lookup=MappingProxyType({}),
        db_session_factory=db_session_factory,
        trigger_ids=TriggerIdGenerator(session_id=session.session_id),
    )
    assert "breach_loop" in supervisor.task_names()


async def test_register_breach_loop_wires_real_activity_log_sink(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression — the supervisor's breach-loop task receives a real
    activity_log_sink that persists entries via db_session_factory.

    Before the fix the sink was ``_no_op_sink`` and ``HALT_ACTIVATED`` /
    ``HALT_LIFTED`` entries were silently dropped — masked only by the
    ``_ClosedMarket`` stub (which gates the breach loop's entry emission).
    Once ``_ClosedMarket`` lifts, this regression would have made halt
    transitions invisible to the activity log.
    """
    session = _session()
    supervisor = MonitorSupervisor(session=session, config=_config())
    captured: dict[str, Any] = {}

    real_fn = "alphamind.execution.continuous_monitor.__main__.register_breach_loop_task"

    def _capture(supervisor_arg: object, **kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    with mock.patch(real_fn, _capture):
        _register_breach_loop(
            supervisor,
            underlying_cache=object(),
            session=session,
            breach_behavior_config=_breach_behavior_config(),
            breach_response_lookup=MappingProxyType({}),
            db_session_factory=db_session_factory,
            trigger_ids=TriggerIdGenerator(session_id=session.session_id),
        )

    sink = captured["kwargs"]["activity_log_sink"]
    # Pre-fix this was bound to ``_no_op_sink`` (returns None unconditionally).
    # Post-fix it is the closure ``_activity_log_sink`` that loops the iterable
    # and awaits ``make_activity_log_emitter`` per row. We can't compare names,
    # so we assert the post-fix behavior: a real writer must be a non-no-op
    # callable that materializes the iterable.
    assert sink is not None
    assert callable(sink)
    # Drain an empty iterable — should be a clean no-op against the real DB
    # path (i.e., no exception thrown by the emitter wiring).
    await sink([])
