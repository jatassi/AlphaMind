"""Wiring tests — assert the SSE emitter composes with existing monitor seams.

The wiring helpers in ``control/wiring.py`` wrap existing callbacks
(``on_immediate_breach``, ``activity_log_sink``, emergency-trigger writer,
greeks-refresh emit) so each producer fires structural callbacks the SSE
consumer can observe. These tests assert the composition: the wrapped
callbacks emit the expected SSE event AND delegate to the inner callback.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.config.models.guardrails import BreachResponse
from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopHealthSignal,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.control.events import SSEEventEmitter
from alphamind.execution.continuous_monitor.control.wiring import (
    make_breach_loop_health_emit,
    make_deferred_breach_emit_for_breach_loop,
    make_greeks_refresh_emit,
    wrap_emergency_activity_log_writer,
    wrap_fill_enrichment_with_emit,
    wrap_on_immediate_breach,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EmergencyInvocationRequestedDetail,
    EventGroup,
    EventSource,
    EventType,
)
from alphamind.risk_guardrails.breach_behavior import RiskZone


def _build_rule_evaluation(rule_id: str = "per_position_max_size") -> RuleEvaluation:
    return RuleEvaluation(
        rule_id=rule_id,
        current_value=0.07,
        limit_value=0.05,
        overage=0.02,
        zone=RiskZone.BLOCKED,
        classification=BreachResponse.immediate_engine,
        breaching_position_id="pos-1",
    )


@pytest.mark.asyncio
class TestWrapOnImmediateBreach:
    async def test_fires_breach_detected_immediate_and_delegates(self) -> None:
        emitter = SSEEventEmitter()
        inner_calls: list[tuple[Any, Any]] = []

        async def inner(result: Any, rule: RuleEvaluation) -> None:
            inner_calls.append((result, rule))

        composed = wrap_on_immediate_breach(emitter=emitter, inner=inner)
        async with emitter.subscribe() as queue:
            # The wrapper does not read ``result`` — pass a sentinel.
            result_sentinel = object()
            evaluation = _build_rule_evaluation()
            await composed(result_sentinel, evaluation)  # type: ignore[arg-type]
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "breach_detected"
        assert event.payload["rule"] == "per_position_max_size"
        assert event.payload["response_classification"] == "immediate"
        assert len(inner_calls) == 1
        assert inner_calls[0][1] is evaluation


@pytest.mark.asyncio
class TestMakeDeferredBreachEmit:
    async def test_fires_breach_detected_deferred(self) -> None:
        emitter = SSEEventEmitter()
        emit = make_deferred_breach_emit_for_breach_loop(emitter=emitter)
        async with emitter.subscribe() as queue:
            evaluation = _build_rule_evaluation(rule_id="daily_drawdown")
            emit(evaluation)
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "breach_detected"
        assert event.payload["response_classification"] == "deferred"
        assert event.payload["rule"] == "daily_drawdown"


@pytest.mark.asyncio
class TestWrapEmergencyActivityLogWriter:
    async def test_emergency_entry_fires_sse_emit_and_delegates(self) -> None:
        emitter = SSEEventEmitter()
        written: list[ActivityLogEntry] = []

        async def inner(entry: ActivityLogEntry) -> None:
            written.append(entry)

        composed = wrap_emergency_activity_log_writer(emitter=emitter, inner=inner)
        detail = EmergencyInvocationRequestedDetail(
            trigger_type="regime_jump",
            trigger_reason="regime_jump_low_to_crisis",
            cooldown_remaining_seconds=0,
        )
        entry = ActivityLogEntry(
            entry_id="mon-emt-1",
            invocation_id="inv-1",
            timestamp=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
            event_type=EventType.EMERGENCY_INVOCATION_REQUESTED,
            event_group=EventGroup.RISK_AND_GUARDRAIL,
            position_id=None,
            order_id=None,
            thesis_id=None,
            source=EventSource.GUARDRAIL_LAYER,
            detail=detail,
        )
        async with emitter.subscribe() as queue:
            await composed(entry)
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "emergency_invocation_triggered"
        assert event.payload["reason"] == "regime_jump_low_to_crisis"
        assert len(written) == 1

    async def test_non_emergency_entry_passes_through_without_emit(self) -> None:
        emitter = SSEEventEmitter()
        written: list[ActivityLogEntry] = []

        async def inner(entry: ActivityLogEntry) -> None:
            written.append(entry)

        composed = wrap_emergency_activity_log_writer(emitter=emitter, inner=inner)
        # Use any non-emergency event type. The wrapper short-circuits and
        # does not emit; the inner writer still gets called.
        from alphamind.portfolio_state.events.activity_log import (
            GreeksRefreshFailedDetail,
        )

        entry = ActivityLogEntry(
            entry_id="mon-grf-1",
            invocation_id="inv-1",
            timestamp=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
            event_type=EventType.GREEKS_REFRESH_FAILED,
            event_group=EventGroup.RISK_AND_GUARDRAIL,
            position_id=None,
            order_id=None,
            thesis_id=None,
            source=EventSource.GUARDRAIL_LAYER,
            detail=GreeksRefreshFailedDetail(
                underlying_ticker="AAPL",
                occ_symbol="AAPL250620C00150000",
                failure_reason="iv_fetch_timeout",
                prior_as_of=None,
            ),
        )
        async with emitter.subscribe() as queue:
            await composed(entry)
            # No event should have fired; queue.get should time out.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=0.1)
        assert len(written) == 1


@pytest.mark.asyncio
class TestWrapFillEnrichment:
    async def test_emits_fill_received_when_record_carries_fields(self) -> None:
        emitter = SSEEventEmitter()

        # Bare record with the four fields the emit needs.
        class FakeRecord:
            def __init__(self) -> None:
                self.order_id = "ord-1"
                self.fill_price = 100.5
                self.fill_quantity = 10.0
                self.position_id = "pos-1"

        async def inner(record: Any) -> Any:
            return record

        composed = wrap_fill_enrichment_with_emit(emitter=emitter, inner=inner)
        async with emitter.subscribe() as queue:
            await composed(FakeRecord())
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "fill_received"
        assert event.payload["order_id"] == "ord-1"

    async def test_inner_callable_result_is_returned(self) -> None:
        emitter = SSEEventEmitter()

        class FakeRecord:
            def __init__(self) -> None:
                self.order_id = "ord-1"
                self.fill_price = 100.5
                self.fill_quantity = 10.0
                self.position_id = "pos-1"

        async def inner(record: Any) -> Any:
            record.enriched = True
            return record

        composed = wrap_fill_enrichment_with_emit(emitter=emitter, inner=inner)
        record = await composed(FakeRecord())
        assert getattr(record, "enriched", False)


@pytest.mark.asyncio
class TestMakeGreeksRefreshEmit:
    async def test_fires_greeks_refreshed(self) -> None:
        emitter = SSEEventEmitter()
        emit = make_greeks_refresh_emit(emitter=emitter)
        async with emitter.subscribe() as queue:
            emit("AAPL")
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "greeks_refreshed"
        assert event.payload["underlying"] == "AAPL"


@pytest.mark.asyncio
class TestMakeBreachLoopHealthEmit:
    """ALP-732 — the loop's ``on_health_signal`` sink emits the right SSE event."""

    async def test_degraded_signal_emits_breach_loop_degraded(self) -> None:
        emitter = SSEEventEmitter()
        emit = make_breach_loop_health_emit(emitter=emitter)
        async with emitter.subscribe() as queue:
            emit(
                BreachLoopHealthSignal(
                    degraded=True, consecutive_failures=3, last_error="RuntimeError('x')"
                )
            )
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "breach_loop_degraded"
        assert event.payload["consecutive_failures"] == 3
        assert event.payload["last_error"] == "RuntimeError('x')"
        assert emitter.is_breach_loop_degraded() is True

    async def test_recovered_signal_emits_breach_loop_recovered(self) -> None:
        emitter = SSEEventEmitter()
        emit = make_breach_loop_health_emit(emitter=emitter)
        async with emitter.subscribe() as queue:
            emit(BreachLoopHealthSignal(degraded=True, consecutive_failures=2, last_error="boom"))
            await asyncio.wait_for(queue.get(), timeout=0.5)
            emit(BreachLoopHealthSignal(degraded=False, consecutive_failures=2, last_error=None))
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "breach_loop_recovered"
        assert event.payload["consecutive_failures"] == 2
        assert emitter.is_breach_loop_degraded() is False

    async def test_degraded_with_none_error_falls_back_to_unknown(self) -> None:
        # ``last_error`` is None only on recovery, but the degraded branch
        # defends against a None by substituting "unknown" so the
        # min_length=1 model field never rejects.
        emitter = SSEEventEmitter()
        emit = make_breach_loop_health_emit(emitter=emitter)
        async with emitter.subscribe() as queue:
            emit(BreachLoopHealthSignal(degraded=True, consecutive_failures=1, last_error=None))
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.payload["last_error"] == "unknown"
