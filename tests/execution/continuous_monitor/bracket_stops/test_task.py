"""Tests for ``run_options_bracket_watcher`` (story 04c / ALP-440).

The run-forever task exposes its single-iteration kernel for testability:
each test drives one cycle with the surrounding fakes for the position
repository, the bracket repository, the underlying-price cache, the
broker-call submitter, and the activity-log emitter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.bracket_stops.closer import (
    CloseSubmissionResult,
)
from alphamind.execution.continuous_monitor.bracket_stops.task import (
    _run_bracket_stop_cycle,
)
from alphamind.execution.continuous_monitor.cascade_dispatch import TriggerIdGenerator
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventSource,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    PLAnchorSpec,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)

_NOW = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)


def _const_str(value: str):  # type: ignore[no-untyped-def]
    """Async-callable returning *value* — async-native provider seam for tests."""

    async def _inner() -> str:
        return value

    return _inner


def _const_float(value: float):  # type: ignore[no-untyped-def]
    """Async-callable returning *value* — async-native provider seam for tests."""

    async def _inner() -> float:
        return value

    return _inner


def _trigger_ids(*, session_id: str = "mon-S") -> TriggerIdGenerator:
    """Fresh trigger-id generator scoped to *session_id* for isolation across tests."""
    return TriggerIdGenerator(session_id=session_id)


def _config(*, cadence: float = 1.0) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        greeks_refresh_inspection_cadence_seconds=30,
        bracket_stop_evaluation_cadence_seconds=cadence,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
    )


def _options_position(
    *,
    position_id: str = "pos-1",
    underlying: str = "NVDA",
    direction: Direction = Direction.LONG,
    bracket_id: str = "brk-1",
    premium_paid: float = 12.0,
    iv: float = 0.30,
) -> PositionRecord:
    return PositionRecord(
        position_id=position_id,
        thesis_id="THESIS-1",
        bracket_id=bracket_id,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=_NOW,
        details=OptionsPositionDetails(
            underlying_ticker=underlying,
            strike_price=850.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=premium_paid,
            greeks=OptionGreeks(
                delta=0.5,
                gamma=0.02,
                theta=-0.04,
                vega=0.2,
                as_of_timestamp=_NOW,
                iv_used=iv,
            ),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=premium_paid,
                fill_quantity=1.0,
                slippage=0.0,
                fees=0.0,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _price_stop_bracket(
    *,
    bracket_id: str = "brk-1",
    position_id: str = "pos-1",
    threshold: float = 865.0,
    direction: str = "LTE",
) -> BracketRecord:
    leg = BracketLeg(
        leg_id="leg-stop-1",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker="NVDA",
            threshold_usd=threshold,
            direction=direction,  # type: ignore[arg-type]
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.ACTIVE,
        entry_order_id="ord-entry-1",
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _pl_target_bracket(
    *,
    bracket_id: str = "brk-1",
    position_id: str = "pos-1",
    target_pct: float = 0.80,
    actual_entry_price: float = 12.0,
) -> BracketRecord:
    leg = BracketLeg(
        leg_id="leg-target-1",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker="NVDA",
            threshold_usd=actual_entry_price * (1.0 + target_pct),
            direction="GTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
        pl_anchor=PLAnchorSpec(
            spec_type="target",
            pct=target_pct,
            planned_entry_price=actual_entry_price,
            actual_entry_price=actual_entry_price,
            recalculated_at_fill=True,
        ),
    )
    return BracketRecord(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.ACTIVE,
        entry_order_id="ord-entry-1",
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


class FakePositionRepository:
    def __init__(self, positions: tuple[PositionRecord, ...]) -> None:
        self._positions = positions
        self.calls = 0

    async def get_open_positions(self) -> tuple[PositionRecord, ...]:
        self.calls += 1
        return self._positions


class FakeBracketRepository:
    def __init__(self, brackets: tuple[BracketRecord, ...]) -> None:
        self._brackets = brackets

    async def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]:
        return tuple(b for b in self._brackets if b.position_id in position_ids)


@dataclass
class FakeSubmitter:
    options_calls: list[tuple[str, str]] = field(default_factory=list)
    strategy_calls: list[str] = field(default_factory=list)

    async def submit_options_close(
        self,
        *,
        position: PositionRecord,
        details: OptionsPositionDetails,
        client_order_id: str,
    ) -> CloseSubmissionResult:
        del details
        self.options_calls.append((position.position_id, client_order_id))
        return CloseSubmissionResult(order_ids=(client_order_id,), mode="single_leg")

    async def submit_strategy_close(
        self, *, position: PositionRecord, details: object, client_order_id_base: str
    ) -> CloseSubmissionResult:
        del details
        self.strategy_calls.append(position.position_id)
        return CloseSubmissionResult(order_ids=(client_order_id_base,), mode="strategy_combined")


@dataclass
class FakeActivityLog:
    entries: list[ActivityLogEntry] = field(default_factory=list)

    async def emit(self, entry: ActivityLogEntry) -> None:
        self.entries.append(entry)


async def _seed_cache(prices: dict[str, float]) -> UnderlyingPriceCache:
    cache = UnderlyingPriceCache()
    for ticker, price in prices.items():
        await cache.update(UnderlyingQuote(ticker=ticker, price=price, as_of=_NOW))
    return cache


# ---------------------------------------------------------------------------
# Price-based stop firing
# ---------------------------------------------------------------------------


class TestPriceStopFiring:
    async def test_long_stop_fires_when_spot_below(self) -> None:
        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        # Spot below threshold → fires.
        cache = await _seed_cache({"NVDA": 860.0})
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        fired_ids: set[tuple[str, str]] = set()
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=log.emit,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=fired_ids,
        )
        assert len(submitter.options_calls) == 1
        assert len(log.entries) == 1
        assert log.entries[0].source is EventSource.BRACKET_MANAGER
        assert log.entries[0].detail.exit_method is PositionExitMethod.STOP_TRIGGERED

    async def test_long_stop_does_not_fire_above_threshold(self) -> None:
        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = await _seed_cache({"NVDA": 870.0})
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=log.emit,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=set(),
        )
        assert submitter.options_calls == []
        assert log.entries == []


# ---------------------------------------------------------------------------
# Already-fired tracking
# ---------------------------------------------------------------------------


class TestAlreadyFiredTracking:
    async def test_each_leg_fires_at_most_once_per_session(self) -> None:
        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = await _seed_cache({"NVDA": 860.0})
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        fired_legs: set[tuple[str, str]] = set()
        # Two consecutive cycles with the same firing condition.
        for _ in range(2):
            await _run_bracket_stop_cycle(
                config=_config(),
                position_repository=FakePositionRepository((position,)),
                bracket_repository=FakeBracketRepository((bracket,)),
                cache=cache,
                submitter=submitter,
                activity_log=log.emit,
                invocation_id_provider=_const_str("inv-001"),
                monitor_session_id="mon-S",
                trigger_ids=_trigger_ids(),
                now=_NOW,
                risk_free_rate=0.045,
                fired_legs=fired_legs,
            )
        # Exactly one fire even though the trigger remains satisfied.
        assert len(submitter.options_calls) == 1
        assert len(log.entries) == 1


class TestTriggerIdsSharedWithCascade:
    """Regression — bracket-stop fires consume from the shared TriggerIdGenerator.

    The prior implementation derived ``trigger_id`` from
    ``abs(hash((bracket.bracket_id, leg.leg_id))) % (10**9)`` — non-deterministic
    (PYTHONHASHSEED-randomized) and able to collide with the cascade
    dispatcher's monotonic 1-based counter. Both producers encode
    ``MON.{session}.{trigger}.0`` as the engine-originated
    ``client_order_id``; a collision lands two broker submissions with
    identical IDs.

    The fix threads the cascade dispatcher's :class:`TriggerIdGenerator`
    instance into the bracket-stops wiring. This test asserts:

    1. The bracket-stop fire advances the shared sequence (consumes ``.next()``).
    2. A subsequent dispatcher ``.next()`` returns the *next* id, not a
       collision with the bracket-stop's id.
    """

    async def test_fire_consumes_shared_generator(self) -> None:
        from alphamind.execution.oms.command_ids import derive_engine_command_id

        shared = _trigger_ids(session_id="mon-X")
        # Burn one to seed the generator at 2 — matches "cascade fired first".
        first_cascade_id = shared.next()
        assert first_cascade_id == 1

        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = await _seed_cache({"NVDA": 860.0})
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=log.emit,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-X",
            trigger_ids=shared,
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=set(),
        )

        # The bracket fire took the next slot (2) from the shared generator.
        assert len(submitter.options_calls) == 1
        _, client_order_id = submitter.options_calls[0]
        expected_brk = derive_engine_command_id(
            monitor_session_id="mon-X", trigger_id=2, command_ordinal=0
        )
        assert client_order_id == expected_brk

        # A subsequent dispatcher pull lands at 3 — no collision with the
        # bracket-stop's slot 2 (or the cascade's earlier slot 1).
        second_cascade_id = shared.next()
        assert second_cascade_id == 3

        # The cascade's previous id and the bracket-stop's id are distinct,
        # which is the property the deterministic fix guarantees.
        assert first_cascade_id != 2


# ---------------------------------------------------------------------------
# P/L target firing
# ---------------------------------------------------------------------------


class TestPLTargetFiring:
    async def test_pl_target_fires_when_derived_price_above(self) -> None:
        position = _options_position(
            direction=Direction.LONG,
            premium_paid=35.24,
            iv=0.30,
        )
        bracket = _pl_target_bracket(target_pct=0.80, actual_entry_price=35.24)
        # Spot 900 with strike 850 IV 0.30 → derived ~$67.77 > target $63.43 → fires.
        cache = await _seed_cache({"NVDA": 900.0})
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=log.emit,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=set(),
        )
        assert len(submitter.options_calls) == 1
        assert log.entries[0].detail.exit_method is PositionExitMethod.TARGET_REACHED


# ---------------------------------------------------------------------------
# Non-options positions skipped
# ---------------------------------------------------------------------------


class TestEquityPositionsSkipped:
    async def test_equity_positions_are_skipped(self) -> None:
        from alphamind.portfolio_state.records.positions import EquityPositionDetails

        equity = PositionRecord(
            position_id="pos-eq",
            thesis_id=None,
            bracket_id="brk-eq",
            status=PositionStatus.OPEN,
            direction=Direction.LONG,
            entry_timestamp=_NOW,
            details=EquityPositionDetails(
                ticker="AAPL", share_count=100.0, average_cost_basis_per_share=150.0
            ),
            execution_history=(
                PositionFill(
                    fill_timestamp=_NOW,
                    fill_price=150.0,
                    fill_quantity=100.0,
                    slippage=0.0,
                    fees=0.0,
                ),
            ),
            realized_pnl_to_date_usd=None,
            corporate_action_adjustment_needed=False,
            parent_position_id=None,
            origin=None,
        )
        leg = BracketLeg(
            leg_id="leg-eq",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=None,
            trigger=PriceTrigger(underlying_ticker="AAPL", threshold_usd=140.0, direction="LTE"),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        bracket = BracketRecord(
            bracket_id="brk-eq",
            position_id="pos-eq",
            status=BracketStatus.ACTIVE,
            entry_order_id="ord-eq",
            protective_legs=(leg,),
            modification_history=(),
            corporate_action_cancellation_reason=None,
        )
        cache = await _seed_cache({"AAPL": 130.0})
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((equity,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=log.emit,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=set(),
        )
        # Equity bracket-stops are NOT this story's responsibility — Alpaca's
        # native bracket order class handles them. Watcher skips silently.
        assert submitter.options_calls == []
        assert log.entries == []


# ---------------------------------------------------------------------------
# Cancelled / non-active legs do not fire
# ---------------------------------------------------------------------------


class TestLegStatusFiltering:
    async def test_cancelled_leg_does_not_fire(self) -> None:
        position = _options_position(direction=Direction.LONG)
        leg = BracketLeg(
            leg_id="leg-cancelled",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=None,
            trigger=PriceTrigger(underlying_ticker="NVDA", threshold_usd=865.0, direction="LTE"),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.CANCELLED,
        )
        # Build a BracketRecord with one cancelled leg + a backstop mechanical
        # leg that is ACTIVE — required by the BracketRecord validator
        # (hard-backstop invariant).
        backstop = BracketLeg(
            leg_id="leg-time",
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=None,
            trigger=__import__(
                "alphamind.portfolio_state.records.orders", fromlist=["TimeTrigger"]
            ).TimeTrigger(deadline=_NOW + timedelta(hours=24)),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        bracket = BracketRecord(
            bracket_id="brk-1",
            position_id="pos-1",
            status=BracketStatus.ACTIVE,
            entry_order_id="ord-entry-1",
            protective_legs=(leg, backstop),
            modification_history=(),
            corporate_action_cancellation_reason=None,
        )
        cache = await _seed_cache({"NVDA": 860.0})
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=log.emit,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=set(),
        )
        # Cancelled price-stop leg ignored; time-expiration leg not in this
        # story's scope (timing is out-of-scope per story Out of scope).
        assert submitter.options_calls == []
        assert log.entries == []


# ---------------------------------------------------------------------------
# Cache miss
# ---------------------------------------------------------------------------


class TestCacheMiss:
    async def test_missing_spot_does_not_fire(self) -> None:
        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = UnderlyingPriceCache()  # empty
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=log.emit,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=set(),
        )
        assert submitter.options_calls == []
        assert log.entries == []


# ---------------------------------------------------------------------------
# Config knob
# ---------------------------------------------------------------------------


class TestConfigKnob:
    def test_bracket_stop_evaluation_cadence_seconds_default(self) -> None:
        """The config default is 1.0 per the story acceptance criteria."""
        cfg = ContinuousMonitorConfig(
            breach_evaluation_cadence_seconds=60,
            greeks_refresh_interval_minutes=15,
            greeks_refresh_underlying_move_threshold_pct=2.0,
            greeks_refresh_inspection_cadence_seconds=30,
            underlying_stream_provider="alpaca-iex",
            max_reconnect_attempts=5,
            supervisor_shutdown_timeout_seconds=5,
        )
        assert cfg.bracket_stop_evaluation_cadence_seconds == 1.0

    def test_bracket_stop_evaluation_cadence_seconds_positive_required(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ContinuousMonitorConfig(
                breach_evaluation_cadence_seconds=60,
                greeks_refresh_interval_minutes=15,
                greeks_refresh_underlying_move_threshold_pct=2.0,
                greeks_refresh_inspection_cadence_seconds=30,
                bracket_stop_evaluation_cadence_seconds=0.0,
                underlying_stream_provider="alpaca-iex",
                max_reconnect_attempts=5,
                supervisor_shutdown_timeout_seconds=5,
            )

        with pytest.raises(ValidationError):
            ContinuousMonitorConfig(
                breach_evaluation_cadence_seconds=60,
                greeks_refresh_interval_minutes=15,
                greeks_refresh_underlying_move_threshold_pct=2.0,
                greeks_refresh_inspection_cadence_seconds=30,
                bracket_stop_evaluation_cadence_seconds=-1.0,
                underlying_stream_provider="alpaca-iex",
                max_reconnect_attempts=5,
                supervisor_shutdown_timeout_seconds=5,
            )


# ---------------------------------------------------------------------------
# Run-forever loop respects cadence
# ---------------------------------------------------------------------------


class TestRunForeverLoop:
    async def test_run_forever_sleeps_on_cadence(self) -> None:
        """``run_options_bracket_watcher`` sleeps for the configured cadence."""
        import asyncio

        from alphamind.execution.continuous_monitor.bracket_stops.task import (
            run_options_bracket_watcher,
        )
        from alphamind.execution.continuous_monitor.session import MonitorSession

        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = await _seed_cache({"NVDA": 870.0})  # does not fire
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        sleep_calls: list[float] = []

        async def _record_sleep(duration: float) -> None:
            sleep_calls.append(duration)
            if len(sleep_calls) >= 3:
                raise asyncio.CancelledError

        session = MonitorSession(
            session_id="mon-20260511T143000Z-deadbeef",
            started_at=_NOW,
            mode="paper",
        )
        cfg = _config(cadence=2.5)

        with pytest.raises(asyncio.CancelledError):
            await run_options_bracket_watcher(
                session,
                cfg,
                position_repository=FakePositionRepository((position,)),
                bracket_repository=FakeBracketRepository((bracket,)),
                cache=cache,
                submitter=submitter,
                activity_log=log.emit,
                invocation_id_provider=_const_str("inv-001"),
                risk_free_rate_provider=_const_float(0.045),
                trigger_ids=_trigger_ids(session_id=session.session_id),
                now=lambda: _NOW,
                sleep=_record_sleep,
            )
        assert all(d == 2.5 for d in sleep_calls)
        assert len(sleep_calls) >= 1
