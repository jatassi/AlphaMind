"""Tests for the 18 default condition predicates (story 05a / ALP-671; ALP-739).

Each test covers one of the three predicate categories:

* Event-driven (PipelineAborted, MonitorWebsocketDisconnected,
  AgentMalformedOutput).
* State-polling (MarginCallDetected, HardBlockGuardrailRejection,
  HaltModeEntered, CommandAbandoned, ThesisResolved, EntryNoFill,
  DataDirectoryDiskPressure).
* Dormant (CriticalApiFailure, ImportantApiFailure,
  ScheduleMissCritical, ScheduleMiss, RegimeJump,
  OptionalDataCategorySkipped, DrawdownTierCrossed,
  ProfileBoundaryCrossed).

Plus the :func:`build_default_rules` factory shape assertions.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center._kernel.ids import alert_rule_name
from alphamind.command_center.alerts.conditions import (
    AgentMalformedOutputCondition,
    CommandAbandonedCondition,
    CriticalApiFailureCondition,
    DataDirectoryDiskPressureCondition,
    DormantCondition,
    DrawdownTierCrossedCondition,
    EntryNoFillCondition,
    HaltModeEnteredCondition,
    HardBlockGuardrailRejectionCondition,
    ImportantApiFailureCondition,
    MarginCallDetectedCondition,
    MonitorWebsocketDisconnectedCondition,
    OptionalDataCategorySkippedCondition,
    PipelineAbortedCondition,
    ProfileBoundaryCrossedCondition,
    RegimeJumpCondition,
    ScheduleMissCondition,
    ScheduleMissCriticalCondition,
    ThesisResolvedCondition,
    build_default_rules,
)
from alphamind.command_center.alerts.rules import (
    AlertEvaluatorState,
    AlertSeverity,
)
from alphamind.persistence.models import Base
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory production-Base factory for state-polling predicates.
# ---------------------------------------------------------------------------


@pytest.fixture
async def state_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory production-Base factory seeded with a process_lifetime + invocation."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ProcessLifetimeRow(
                process_lifetime_id="plt-test",
                process_role="monitor",
                process_start_at="2026-05-26T00:00:00Z",
                process_pid=42,
                hostname="t",
                git_sha="d",
                git_branch="t",
                git_dirty=0,
                python_version="3.13",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="/dev/null",
                anthropic_sdk_version="x",
                claude_agent_sdk_version="x",
                os_release="t",
            )
        )
        session.add(
            InvocationRow(
                invocation_id="inv-test",
                process_lifetime_id="plt-test",
                start_at="2026-05-26T11:50:00Z",
                phase1_completed_at=None,
                phase2_completed_at=None,
                trigger_type="manual",
                trigger_source="operator_console",
                trigger_reason="test",
                active_mode="normal",
                git_sha_at_invocation="x",
                active_profile="x",
                active_regime="x",
                active_overlays_json="{}",
                resolved_config_hash="x",
                resolved_config_snapshot_path="x",
                feature_flags_snapshot_json="{}",
                data_calibration_state_snapshot_path="x",
                data_source_freshness_json="{}",
                fill_collection_summary_json=None,
                command_execution_summary_json=None,
                staleness_flag=None,
                snapshot_metadata_json=None,
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_activity(
    factory: async_sessionmaker[AsyncSession],
    *,
    entry_id: str,
    event_type: str,
    entry_at: str,
    event_group: str,
    detail_json: str = "{}",
    position_id: str | None = None,
    thesis_id: str | None = None,
) -> None:
    async with factory() as session:
        session.add(
            ActivityLogRow(
                entry_id=entry_id,
                invocation_id="inv-test",
                entry_at=entry_at,
                event_type=event_type,
                event_group=event_group,
                source="OPERATOR_CONSOLE",
                detail_json=detail_json,
                position_id=position_id,
                order_id=None,
                thesis_id=thesis_id,
            )
        )
        await session.commit()


async def _seed_order(
    factory: async_sessionmaker[AsyncSession],
    *,
    order_id: str,
    order_role: str,
    status: str,
    last_update_timestamp: str,
    filled_quantity: float = 0.0,
    instrument_spec_json: str = '{"instrument_type": "EQUITY", "ticker": "SCHW"}',
    price_parameters_json: str = '{"limit_price": "61.50", "stop_trigger_price": null}',
    duration: str = "DAY",
) -> None:
    """Insert one ``orders`` row for EntryNoFill state-polling tests.

    FK enforcement is off on the in-memory engine, so ``bracket_id`` /
    ``position_id`` may reference rows that don't exist — only the columns
    the predicate reads are meaningful.
    """
    async with factory() as session:
        session.add(
            OrderRow(
                order_id=order_id,
                position_id=None,
                bracket_id="brk-test",
                order_role=order_role,
                order_class="BRACKET",
                instrument_spec_json=instrument_spec_json,
                direction="SELL",
                order_type="LIMIT",
                quantity=10.0,
                price_parameters_json=price_parameters_json,
                duration=duration,
                status=status,
                alpaca_order_id="alp-1",
                alpaca_order_id_chain_json='["alp-1"]',
                submission_timestamp="2026-05-26T09:30:00+00:00",
                last_update_timestamp=last_update_timestamp,
                filled_quantity=filled_quantity,
                average_fill_price=None,
                remaining_quantity=10.0 - filled_quantity,
                modification_count=0,
                metadata_json='{"originating_thesis_id": null, '
                '"originating_pm_command_id": null, "age_hours": 0.0}',
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Event-driven predicates.
# ---------------------------------------------------------------------------


class TestPipelineAbortedCondition:
    @pytest.mark.asyncio
    async def test_fires_on_failed_invocation_ended(self) -> None:
        cond = PipelineAbortedCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.INVOCATION_ENDED,
            payload={
                "invocation_id": "inv-bad",
                "status": "failed",
                "ended_at": "2026-05-26T12:00:00Z",
            },
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is True
        assert result.debounce_key == "inv-bad"
        assert result.context["invocation_id"] == "inv-bad"

    @pytest.mark.asyncio
    async def test_does_not_fire_on_succeeded_invocation_ended(self) -> None:
        cond = PipelineAbortedCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.INVOCATION_ENDED,
            payload={"invocation_id": "inv-ok", "status": "succeeded"},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_on_unrelated_event(self) -> None:
        cond = PipelineAbortedCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.INVOCATION_STARTED,
            payload={"invocation_id": "inv-other"},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_on_monitor_event(self) -> None:
        cond = PipelineAbortedCondition()
        event = MonitorEvent(
            event_type=MonitorEventType.WEBSOCKET_CONNECTED,
            payload={},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_on_no_event(self) -> None:
        cond = PipelineAbortedCondition()
        result = await cond.evaluate(event=None, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False


class TestMonitorWebsocketDisconnectedCondition:
    @pytest.mark.asyncio
    async def test_fires_on_websocket_disconnected(self) -> None:
        cond = MonitorWebsocketDisconnectedCondition()
        event = MonitorEvent(
            event_type=MonitorEventType.WEBSOCKET_DISCONNECTED,
            payload={"session_id": "ws-1", "reason": "timeout"},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is True
        assert result.debounce_key == "ws-1"

    @pytest.mark.asyncio
    async def test_does_not_fire_on_connected(self) -> None:
        cond = MonitorWebsocketDisconnectedCondition()
        event = MonitorEvent(
            event_type=MonitorEventType.WEBSOCKET_CONNECTED,
            payload={"session_id": "ws-1"},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False


class TestAgentMalformedOutputCondition:
    @pytest.mark.asyncio
    async def test_fires_on_malformed_output_retry(self) -> None:
        cond = AgentMalformedOutputCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.AGENT_RETRYING,
            payload={
                "agent_name": "synthesizer",
                "reason": "malformed_output: missing required field",
                "invocation_id": "inv-1",
            },
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is True
        assert result.debounce_key == "synthesizer"

    @pytest.mark.asyncio
    async def test_does_not_fire_on_non_malformed_retry(self) -> None:
        cond = AgentMalformedOutputCondition()
        event = PipelineEvent(
            event_type=PipelineEventType.AGENT_RETRYING,
            payload={"agent_name": "synthesizer", "reason": "timeout"},
        )
        result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
        assert result.fired is False


# ---------------------------------------------------------------------------
# State-polling predicates.
# ---------------------------------------------------------------------------


class TestMarginCallDetectedCondition:
    @pytest.mark.asyncio
    async def test_fires_when_row_exists_inside_window(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="al-1",
            event_type="MARGIN_CALL",
            entry_at="2026-05-26T11:55:00Z",
            event_group="CASH_AND_MARGIN",
        )
        cond = MarginCallDetectedCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.debounce_key == "al-1"

    @pytest.mark.asyncio
    async def test_does_not_fire_when_no_rows(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        cond = MarginCallDetectedCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_without_factory(self) -> None:
        cond = MarginCallDetectedCondition()
        state = AlertEvaluatorState(now=_NOW)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_for_row_outside_window(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="al-old",
            event_type="MARGIN_CALL",
            entry_at="2026-05-26T08:00:00Z",  # 4h ago
            event_group="CASH_AND_MARGIN",
        )
        cond = MarginCallDetectedCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False


class TestHardBlockGuardrailRejectionCondition:
    @pytest.mark.asyncio
    async def test_fires_on_hard_block_row(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="gr-1",
            event_type="GUARDRAIL_REJECTION",
            entry_at="2026-05-26T11:55:00Z",
            event_group="RISK_AND_GUARDRAIL",
            detail_json='{"zone": "hard_block", "rule": "max_position_size"}',
            position_id=None,
        )
        cond = HardBlockGuardrailRejectionCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True

    @pytest.mark.asyncio
    async def test_does_not_fire_on_soft_block(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="gr-soft",
            event_type="GUARDRAIL_REJECTION",
            entry_at="2026-05-26T11:55:00Z",
            event_group="RISK_AND_GUARDRAIL",
            detail_json='{"zone": "soft_block"}',
        )
        cond = HardBlockGuardrailRejectionCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False


class TestHaltModeEnteredCondition:
    @pytest.mark.asyncio
    async def test_fires_on_halt_activated_row(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="halt-1",
            event_type="HALT_ACTIVATED",
            entry_at="2026-05-26T11:58:00Z",
            event_group="RISK_AND_GUARDRAIL",
        )
        cond = HaltModeEnteredCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.debounce_key == "halt-1"


class TestCommandAbandonedCondition:
    @pytest.mark.asyncio
    async def test_fires_on_command_abandoned(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_activity(
            state_factory,
            entry_id="cmd-1",
            event_type="COMMAND_ABANDONED",
            entry_at="2026-05-26T11:58:00Z",
            event_group="PM_DECISION",
        )
        cond = CommandAbandonedCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True


class TestThesisResolvedCondition:
    @pytest.mark.asyncio
    async def test_fires_on_thesis_resolved(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Need to seed theses row first for FK; alternatively use thesis_id=None.
        await _seed_activity(
            state_factory,
            entry_id="th-1",
            event_type="THESIS_RESOLVED",
            entry_at="2026-05-26T11:58:00Z",
            event_group="THESIS",
            thesis_id=None,
        )
        cond = ThesisResolvedCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True


class TestEntryNoFillCondition:
    @pytest.mark.asyncio
    async def test_fires_on_expired_entry_zero_fill(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_order(
            state_factory,
            order_id="ord-expired",
            order_role="ENTRY",
            status="EXPIRED",
            last_update_timestamp="2026-05-26T11:58:00+00:00",
        )
        cond = EntryNoFillCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.debounce_key == "ord-expired"
        assert result.context["count"] == 1
        assert result.context["order_ids"] == ["ord-expired"]
        (entry,) = result.context["entries"]
        assert entry["ticker"] == "SCHW"
        assert entry["limit_price"] == "61.50"
        assert entry["time_in_force"] == "DAY"
        assert entry["reason"] == "expired"
        assert entry["status"] == "EXPIRED"

    @pytest.mark.asyncio
    async def test_fires_on_cancelled_entry_zero_fill(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_order(
            state_factory,
            order_id="ord-cancelled",
            order_role="ENTRY",
            status="CANCELLED",
            last_update_timestamp="2026-05-26T11:58:00+00:00",
        )
        cond = EntryNoFillCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.context["entries"][0]["reason"] == "cancelled-superseded"

    @pytest.mark.asyncio
    async def test_options_entry_reports_underlying_as_ticker(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_order(
            state_factory,
            order_id="ord-opt",
            order_role="ENTRY",
            status="EXPIRED",
            last_update_timestamp="2026-05-26T11:58:00+00:00",
            instrument_spec_json=(
                '{"instrument_type": "OPTIONS", "underlying": "AAPL", "strike": 150.0, '
                '"expiration": "2026-06-19", "contract_type": "CALL", '
                '"contract_multiplier": 100}'
            ),
        )
        cond = EntryNoFillCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.context["entries"][0]["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_does_not_fire_when_partially_filled(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_order(
            state_factory,
            order_id="ord-partial",
            order_role="ENTRY",
            status="CANCELLED",
            last_update_timestamp="2026-05-26T11:58:00+00:00",
            filled_quantity=5.0,
        )
        cond = EntryNoFillCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_for_non_entry_role(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_order(
            state_factory,
            order_id="ord-tp",
            order_role="TAKE_PROFIT",
            status="EXPIRED",
            last_update_timestamp="2026-05-26T11:58:00+00:00",
        )
        cond = EntryNoFillCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_for_pending_entry(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_order(
            state_factory,
            order_id="ord-pending",
            order_role="ENTRY",
            status="PENDING",
            last_update_timestamp="2026-05-26T11:58:00+00:00",
        )
        cond = EntryNoFillCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_outside_window(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_order(
            state_factory,
            order_id="ord-stale",
            order_role="ENTRY",
            status="EXPIRED",
            last_update_timestamp="2026-05-26T08:00:00+00:00",  # 4h ago
        )
        cond = EntryNoFillCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_without_factory(self) -> None:
        cond = EntryNoFillCondition()
        state = AlertEvaluatorState(now=_NOW)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_aggregates_multiple_concurrent_no_fill_entries(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # The zero-fill-day scenario: a basket of DAY entries all terminal in
        # the same window. A LIMIT-1 query would mask all but the newest; the
        # aggregate surfaces every one and debounces on the most-recent id.
        await _seed_order(
            state_factory,
            order_id="ord-schw",
            order_role="ENTRY",
            status="CANCELLED",
            last_update_timestamp="2026-05-26T11:55:00+00:00",
            instrument_spec_json='{"instrument_type": "EQUITY", "ticker": "SCHW"}',
        )
        await _seed_order(
            state_factory,
            order_id="ord-zs",
            order_role="ENTRY",
            status="EXPIRED",
            last_update_timestamp="2026-05-26T11:58:00+00:00",  # most recent
            instrument_spec_json='{"instrument_type": "EQUITY", "ticker": "ZS"}',
        )
        cond = EntryNoFillCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.context["count"] == 2
        assert set(result.context["order_ids"]) == {"ord-schw", "ord-zs"}
        tickers = {entry["ticker"] for entry in result.context["entries"]}
        assert tickers == {"SCHW", "ZS"}
        # Debounce key is the most-recent order so a newer terminal entry
        # re-fires while a shrinking set stays debounced.
        assert result.debounce_key == "ord-zs"

    @pytest.mark.asyncio
    async def test_fires_on_add_entry_role(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_order(
            state_factory,
            order_id="ord-add",
            order_role="ADD_ENTRY",
            status="EXPIRED",
            last_update_timestamp="2026-05-26T11:58:00+00:00",
        )
        cond = EntryNoFillCondition()
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.context["order_ids"] == ["ord-add"]

    @pytest.mark.asyncio
    async def test_lookback_tracks_configured_window(
        self,
        state_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # A row 8 min old fires under the default 15-min lookback but not under
        # a 5-min lookback threaded from a debounce override — proving the
        # window is configurable and can't silently desync from debounce.
        await _seed_order(
            state_factory,
            order_id="ord-8min",
            order_role="ENTRY",
            status="EXPIRED",
            last_update_timestamp="2026-05-26T11:52:00+00:00",  # 8 min before _NOW
        )
        state = AlertEvaluatorState(now=_NOW, foreign_reader_factory=state_factory)
        assert (await EntryNoFillCondition().evaluate(event=None, state=state)).fired is True
        narrow = EntryNoFillCondition(lookback=timedelta(minutes=5))
        assert (await narrow.evaluate(event=None, state=state)).fired is False


@pytest.fixture(autouse=True)
def _reset_directory_size_cache() -> None:
    """Drop the module-level size cache before each test runs.

    The cache key is a resolved Path; tests use ``tmp_path`` which is
    unique per test so there's no cache hit hazard, but the dict still
    accumulates entries. Clearing it on every test keeps the test-suite
    memory footprint flat regardless of test count.
    """
    from alphamind.command_center.alerts import conditions

    conditions._directory_size_cache.clear()


class TestDataDirectoryDiskPressureCondition:
    @pytest.mark.asyncio
    async def test_fires_when_size_exceeds_threshold(
        self,
        tmp_path: object,
    ) -> None:
        from pathlib import Path

        data_dir = Path(str(tmp_path)) / "data"
        data_dir.mkdir()
        # Create a file that exceeds a tiny threshold.
        big = data_dir / "big.bin"
        big.write_bytes(b"x" * 1024)
        cond = DataDirectoryDiskPressureCondition(
            data_dir=data_dir,
            threshold_bytes=100,
        )
        state = AlertEvaluatorState(now=_NOW)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is True
        assert result.context["size_bytes"] >= 1024

    @pytest.mark.asyncio
    async def test_cache_reuses_size_within_ttl(self, tmp_path: object) -> None:
        """Regression for finding #14 (Wave-5 review).

        ``_directory_size_bytes`` previously ran sync inside the async
        ``evaluate`` and blocked the event loop on every tick. The fix
        offloads it to a thread and caches the result for a 5-minute
        TTL so the engine's recurring polls don't all rewalk. We assert
        a second evaluation hits the cache by patching the walker to
        raise — if the cache works, the second call still succeeds.
        """
        from pathlib import Path

        from alphamind.command_center.alerts import conditions

        data_dir = Path(str(tmp_path)) / "data"
        data_dir.mkdir()
        (data_dir / "x.bin").write_bytes(b"y" * 1024)
        cond = DataDirectoryDiskPressureCondition(
            data_dir=data_dir,
            threshold_bytes=100,
        )
        state = AlertEvaluatorState(now=_NOW)
        first = await cond.evaluate(event=None, state=state)
        assert first.fired is True
        # Replace the walker — if the cache works, second eval doesn't
        # call it. If the cache fails, second eval raises.
        original = conditions._directory_size_bytes

        def boom(_path: Path) -> int:
            msg = "walker invoked despite cache TTL not elapsed"
            raise AssertionError(msg)

        conditions._directory_size_bytes = boom  # type: ignore[assignment]
        try:
            second = await cond.evaluate(event=None, state=state)
        finally:
            conditions._directory_size_bytes = original
        assert second.fired is True
        assert second.context["size_bytes"] == first.context["size_bytes"]

    @pytest.mark.asyncio
    async def test_does_not_fire_when_under_threshold(self, tmp_path: object) -> None:
        from pathlib import Path

        data_dir = Path(str(tmp_path)) / "data"
        data_dir.mkdir()
        (data_dir / "small.bin").write_bytes(b"x")
        cond = DataDirectoryDiskPressureCondition(
            data_dir=data_dir,
            threshold_bytes=1_000_000_000,
        )
        state = AlertEvaluatorState(now=_NOW)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False

    @pytest.mark.asyncio
    async def test_does_not_fire_when_directory_missing(self, tmp_path: object) -> None:
        from pathlib import Path

        missing = Path(str(tmp_path)) / "missing"
        cond = DataDirectoryDiskPressureCondition(
            data_dir=missing,
            threshold_bytes=0,
        )
        state = AlertEvaluatorState(now=_NOW)
        result = await cond.evaluate(event=None, state=state)
        assert result.fired is False


# ---------------------------------------------------------------------------
# Dormant predicates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cond_factory",
    [
        CriticalApiFailureCondition,
        ImportantApiFailureCondition,
        ScheduleMissCriticalCondition,
        ScheduleMissCondition,
        RegimeJumpCondition,
        OptionalDataCategorySkippedCondition,
        DrawdownTierCrossedCondition,
        ProfileBoundaryCrossedCondition,
    ],
)
@pytest.mark.asyncio
async def test_dormant_predicates_never_fire(cond_factory: type) -> None:
    cond = cond_factory()
    event = PipelineEvent(
        event_type=PipelineEventType.INVOCATION_ENDED,
        payload={"status": "failed"},
    )
    result = await cond.evaluate(event=event, state=AlertEvaluatorState(now=_NOW))
    assert result.fired is False


def test_dormant_log_startup_warning_emits_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cond = DormantCondition(
        name=alert_rule_name("test_dormant"),
        missing_emit="test missing emit",
    )
    with caplog.at_level(logging.WARNING, logger="alphamind.command_center.alerts.conditions"):
        cond.log_startup_warning()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "test_dormant" in warnings[0].message
    assert "test missing emit" in warnings[0].message


# ---------------------------------------------------------------------------
# build_default_rules
# ---------------------------------------------------------------------------


class TestBuildDefaultRules:
    def test_returns_exactly_18_rules(self) -> None:
        rules = build_default_rules()
        assert len(rules) == 18

    def test_rule_names_are_unique(self) -> None:
        rules = build_default_rules()
        names = [str(r.name) for r in rules]
        assert len(set(names)) == 18

    def test_critical_rules_route_to_in_app_and_discord(self) -> None:
        rules = build_default_rules()
        for rule in rules:
            if rule.severity == AlertSeverity.CRITICAL:
                assert "in_app" in rule.channels
                assert "discord" in rule.channels

    def test_important_rules_route_to_in_app_and_discord(self) -> None:
        rules = build_default_rules()
        for rule in rules:
            if rule.severity == AlertSeverity.IMPORTANT:
                assert "in_app" in rule.channels
                assert "discord" in rule.channels

    def test_operational_rules_route_to_in_app_only(self) -> None:
        rules = build_default_rules()
        for rule in rules:
            if rule.severity == AlertSeverity.OPERATIONAL:
                assert rule.channels == ("in_app",)

    def test_debounce_overrides_apply(self) -> None:
        from alphamind.command_center.alerts.conditions import (
            DEFAULT_DEBOUNCE_WINDOWS,
        )

        override = timedelta(seconds=42)
        name = next(iter(DEFAULT_DEBOUNCE_WINDOWS))
        rules = build_default_rules(debounce_overrides={name: override})
        match = next(r for r in rules if r.name == name)
        assert match.debounce_window == override

    def test_channel_overrides_apply(self) -> None:
        from alphamind.command_center.alerts.conditions import (
            DEFAULT_DEBOUNCE_WINDOWS,
        )

        name = next(iter(DEFAULT_DEBOUNCE_WINDOWS))
        rules = build_default_rules(channel_overrides={name: ("in_app",)})
        match = next(r for r in rules if r.name == name)
        assert match.channels == ("in_app",)
