"""Tests for ``run_greeks_refresh`` and the underlying single-cycle orchestrator (story 03a).

The run-forever task exposes its single-iteration kernel ``_run_refresh_cycle``
for testability: each test drives one cycle with the surrounding fakes for
repository, cache, IV provider, greeks writer, and activity-log emitter. The
end-to-end ``run_greeks_refresh`` wraps the kernel in an ``asyncio`` loop with
a sleep budget driven by ``ContinuousMonitorConfig.greeks_refresh_inspection_cadence_seconds``;
the loop body itself is the kernel, so the tests exercise the same code path
the supervisor calls on the real loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.greeks_refresh import (
    IVQuote,
    LastRefreshState,
    run_greeks_refresh,
)
from alphamind.execution.continuous_monitor.greeks_refresh.task import (
    _run_refresh_cycle,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventType,
    GreeksRefreshFailedDetail,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
    occ_symbol_for_options,
)

# ---------------------------------------------------------------------------
# Test fixtures — frozen records and fake collaborators
# ---------------------------------------------------------------------------


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


def _config(
    *,
    interval_minutes: int = 15,
    move_threshold_pct: float = 2.0,
    inspection_cadence: int = 30,
) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=interval_minutes,
        greeks_refresh_underlying_move_threshold_pct=move_threshold_pct,
        greeks_refresh_inspection_cadence_seconds=inspection_cadence,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
    )


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260511T140000Z-deadbeef",
        started_at=datetime(2026, 5, 11, 14, 0, tzinfo=UTC),
        mode="paper",
    )


def _options_position(
    *,
    position_id: str,
    as_of_timestamp: datetime | None,
    prior_iv: float = 0.20,
    prior_greeks: OptionGreeks | None = None,
    underlying_ticker: str = "AAPL",
    strike: float = 200.0,
    expiration: date = date(2026, 6, 19),
) -> PositionRecord:
    greeks = prior_greeks or OptionGreeks(
        delta=0.4,
        gamma=0.02,
        theta=-0.01,
        vega=0.1,
        as_of_timestamp=as_of_timestamp,
        iv_used=prior_iv if as_of_timestamp is not None else None,
        refresh_failed=False,
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        details=OptionsPositionDetails(
            underlying_ticker=Symbol(underlying_ticker),
            strike_price=strike,
            expiration_date=expiration,
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=2.5,
            greeks=greeks,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
                fill_price=price(2.5),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _strategy_position(*, position_id: str, as_of_timestamp: datetime) -> PositionRecord:
    leg_one_options = OptionsPositionDetails(
        underlying_ticker=Symbol("SPY"),
        strike_price=500.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=5.0,
        greeks=OptionGreeks(
            delta=0.5, gamma=0.01, theta=-0.02, vega=0.15, as_of_timestamp=as_of_timestamp
        ),
    )
    leg_two_options = OptionsPositionDetails(
        underlying_ticker=Symbol("SPY"),
        strike_price=510.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=2.0,
        greeks=OptionGreeks(
            delta=0.3, gamma=0.01, theta=-0.015, vega=0.12, as_of_timestamp=as_of_timestamp
        ),
    )
    strategy_greeks = OptionGreeks(
        delta=0.2, gamma=0.0, theta=-0.005, vega=0.03, as_of_timestamp=as_of_timestamp
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        details=StrategyPositionDetails(
            strategy_type_label="vertical_call_spread",
            legs=(
                StrategyLeg(leg_id="leg-1", direction=Direction.LONG, options=leg_one_options),
                StrategyLeg(leg_id="leg-2", direction=Direction.SHORT, options=leg_two_options),
            ),
            net_premium_usd=300.0,
            max_profit_usd=700.0,
            max_loss_usd=300.0,
            breakeven_levels=(503.0,),
            strategy_greeks=strategy_greeks,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
                fill_price=price(3.0),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


class FakeRepository:
    """Repository stub: returns a fixed tuple of open positions."""

    def __init__(self, positions: tuple[PositionRecord, ...]) -> None:
        self._positions = positions
        self.calls = 0

    async def get_open_positions(self) -> tuple[PositionRecord, ...]:
        self.calls += 1
        return self._positions


@dataclass
class FakeIVProvider:
    """Records all batch IV-fetch calls; can be configured to fail / partial / OK."""

    quotes: dict[str, IVQuote] = field(default_factory=dict)
    raises: BaseException | None = None
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def fetch(self, occ_symbols: Iterable[str]) -> dict[str, IVQuote]:
        sym_tuple = tuple(occ_symbols)
        self.calls.append(sym_tuple)
        if self.raises is not None:
            raise self.raises
        return {sym: self.quotes[sym] for sym in sym_tuple if sym in self.quotes}


@dataclass
class FakeGreeksWriter:
    """Captures (position_id, OptionGreeks) writes; the orchestrator's persistence
    contract — story 03a's narrow writer surface.

    For strategy positions, the writer's call shape includes per-leg greeks
    plus the aggregated strategy_greeks; we capture both via a separate method
    so the test can distinguish.
    """

    options_writes: list[tuple[str, OptionGreeks]] = field(default_factory=list)
    strategy_writes: list[tuple[str, dict[str, OptionGreeks], OptionGreeks]] = field(
        default_factory=list
    )

    async def update_options_greeks(self, *, position_id: str, greeks: OptionGreeks) -> None:
        self.options_writes.append((position_id, greeks))

    async def update_strategy_greeks(
        self,
        *,
        position_id: str,
        per_leg: dict[str, OptionGreeks],
        aggregated: OptionGreeks,
    ) -> None:
        self.strategy_writes.append((position_id, per_leg, aggregated))


@dataclass
class FakeActivityLog:
    entries: list[ActivityLogEntry] = field(default_factory=list)

    async def emit(self, entry: ActivityLogEntry) -> None:
        self.entries.append(entry)


# ---------------------------------------------------------------------------
# occ_symbol_for_options — pure helper exercised before the orchestrator
# ---------------------------------------------------------------------------


class TestOccSymbolForOptions:
    def test_call_format(self) -> None:
        details = OptionsPositionDetails(
            underlying_ticker=Symbol("AAPL"),
            strike_price=200.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=2.5,
            greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
        )
        assert occ_symbol_for_options(details) == "O:AAPL260619C00200000"

    def test_put_format(self) -> None:
        details = OptionsPositionDetails(
            underlying_ticker=Symbol("AAPL"),
            strike_price=200.5,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.PUT,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=2.5,
            greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
        )
        assert occ_symbol_for_options(details) == "O:AAPL260619P00200500"


# ---------------------------------------------------------------------------
# Single-cycle orchestrator
# ---------------------------------------------------------------------------


async def _seeded_cache(prices: dict[str, float], as_of: datetime) -> UnderlyingPriceCache:
    cache = UnderlyingPriceCache()
    for ticker, value in prices.items():
        await cache.update(UnderlyingQuote(ticker=ticker, price=value, as_of=as_of))
    return cache


class TestScheduledTrigger:
    async def test_position_older_than_interval_is_refreshed(self) -> None:
        """When ``now - last_refreshed_at >= greeks_refresh_interval_minutes``,
        the position is refreshed and its anchor updates."""
        config = _config(interval_minutes=15, move_threshold_pct=2.0)
        now = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        # 20 minutes ago — exceeds the 15-minute interval.
        last = now - timedelta(minutes=20)
        position = _options_position(position_id=PositionId("pos-1"), as_of_timestamp=last)
        symbol = occ_symbol_for_options(position.details)  # type: ignore[arg-type]
        cache = await _seeded_cache({"AAPL": 200.0}, now)
        iv_provider = FakeIVProvider(
            quotes={symbol: IVQuote(occ_symbol=symbol, iv=0.30, as_of=now)}
        )
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()
        states = {
            "pos-1": LastRefreshState(
                position_id=PositionId("pos-1"),
                last_refreshed_at=last,
                underlying_price_at_last_refresh=200.0,
            )
        }

        await _run_refresh_cycle(
            config=config,
            repository=FakeRepository((position,)),
            cache=cache,
            states=states,
            iv_fetch=iv_provider.fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=now,
            invocation_id_provider=_const_str("inv-test-001"),
            market_open=True,
        )

        assert len(writer.options_writes) == 1
        pid, greeks = writer.options_writes[0]
        assert pid == "pos-1"
        # Successful refresh: refresh_failed False, freshness metadata stamped.
        assert greeks.refresh_failed is False
        assert greeks.iv_used == 0.30
        assert greeks.as_of_timestamp == now
        # Anchor advanced.
        assert states["pos-1"].last_refreshed_at == now
        # Activity log untouched on success.
        assert activity_log.entries == []

    async def test_position_younger_than_interval_skipped(self) -> None:
        config = _config(interval_minutes=15, move_threshold_pct=2.0)
        now = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        # 1 minute ago — well under the 15-minute interval.
        last = now - timedelta(minutes=1)
        position = _options_position(position_id=PositionId("pos-2"), as_of_timestamp=last)
        cache = await _seeded_cache({"AAPL": 200.0}, now)
        symbol = occ_symbol_for_options(position.details)  # type: ignore[arg-type]
        iv_provider = FakeIVProvider(
            quotes={symbol: IVQuote(occ_symbol=symbol, iv=0.30, as_of=now)}
        )
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()
        states = {
            "pos-2": LastRefreshState(
                position_id=PositionId("pos-2"),
                last_refreshed_at=last,
                underlying_price_at_last_refresh=200.0,
            )
        }

        await _run_refresh_cycle(
            config=config,
            repository=FakeRepository((position,)),
            cache=cache,
            states=states,
            iv_fetch=iv_provider.fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=now,
            invocation_id_provider=_const_str("inv-test-002"),
            market_open=True,
        )

        assert writer.options_writes == []
        # The IV provider isn't queried for skipped positions.
        assert iv_provider.calls == []


class TestMoveTrigger:
    async def test_move_above_threshold_triggers_refresh_within_interval(self) -> None:
        """A position whose underlying moved > threshold is refreshed even when
        the scheduled interval has not elapsed."""
        config = _config(interval_minutes=15, move_threshold_pct=2.0)
        now = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        # 2 minutes ago — well within the 15-minute interval.
        last = now - timedelta(minutes=2)
        position = _options_position(position_id=PositionId("pos-3"), as_of_timestamp=last)
        symbol = occ_symbol_for_options(position.details)  # type: ignore[arg-type]
        # Anchor at 200.0; current spot 205.0 → 2.5% move > 2.0% threshold.
        cache = await _seeded_cache({"AAPL": 205.0}, now)
        iv_provider = FakeIVProvider(
            quotes={symbol: IVQuote(occ_symbol=symbol, iv=0.30, as_of=now)}
        )
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()
        states = {
            "pos-3": LastRefreshState(
                position_id=PositionId("pos-3"),
                last_refreshed_at=last,
                underlying_price_at_last_refresh=200.0,
            )
        }

        await _run_refresh_cycle(
            config=config,
            repository=FakeRepository((position,)),
            cache=cache,
            states=states,
            iv_fetch=iv_provider.fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=now,
            invocation_id_provider=_const_str("inv-test-003"),
            market_open=True,
        )

        assert len(writer.options_writes) == 1
        _, greeks = writer.options_writes[0]
        assert greeks.refresh_failed is False
        # Anchor advanced to the new spot.
        assert states["pos-3"].underlying_price_at_last_refresh == 205.0

    async def test_move_below_threshold_does_not_trigger(self) -> None:
        config = _config(interval_minutes=15, move_threshold_pct=2.0)
        now = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        last = now - timedelta(minutes=2)
        position = _options_position(position_id=PositionId("pos-4"), as_of_timestamp=last)
        # 1% move — under the 2% threshold.
        cache = await _seeded_cache({"AAPL": 202.0}, now)
        iv_provider = FakeIVProvider()
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()
        states = {
            "pos-4": LastRefreshState(
                position_id=PositionId("pos-4"),
                last_refreshed_at=last,
                underlying_price_at_last_refresh=200.0,
            )
        }

        await _run_refresh_cycle(
            config=config,
            repository=FakeRepository((position,)),
            cache=cache,
            states=states,
            iv_fetch=iv_provider.fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=now,
            invocation_id_provider=_const_str("inv-test-004"),
            market_open=True,
        )

        assert writer.options_writes == []


class TestIVFetchFailure:
    async def test_no_iv_row_writes_refresh_failed_and_emits_activity_log(self) -> None:
        """When the IV provider returns no quote for a due position, the writer
        records refresh_failed=True while preserving prior greeks, and the
        activity log gets a GREEKS_REFRESH_FAILED entry."""
        config = _config(interval_minutes=15, move_threshold_pct=2.0)
        now = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        last = now - timedelta(minutes=20)
        position = _options_position(position_id=PositionId("pos-5"), as_of_timestamp=last)
        cache = await _seeded_cache({"AAPL": 200.0}, now)
        # Provider returns no quote for our position.
        iv_provider = FakeIVProvider(quotes={})
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()
        states = {
            "pos-5": LastRefreshState(
                position_id=PositionId("pos-5"),
                last_refreshed_at=last,
                underlying_price_at_last_refresh=200.0,
            )
        }

        await _run_refresh_cycle(
            config=config,
            repository=FakeRepository((position,)),
            cache=cache,
            states=states,
            iv_fetch=iv_provider.fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=now,
            invocation_id_provider=_const_str("inv-test-005"),
            market_open=True,
        )

        # The writer was called once to flip refresh_failed=True; prior greeks
        # values are preserved.
        assert len(writer.options_writes) == 1
        _, greeks = writer.options_writes[0]
        assert greeks.refresh_failed is True
        # The prior greeks are preserved untouched.
        prior = position.details.greeks  # type: ignore[union-attr]
        assert greeks.delta == prior.delta
        assert greeks.gamma == prior.gamma
        assert greeks.theta == prior.theta
        assert greeks.vega == prior.vega
        assert greeks.as_of_timestamp == prior.as_of_timestamp
        # Activity log gets a GREEKS_REFRESH_FAILED entry.
        assert len(activity_log.entries) == 1
        entry = activity_log.entries[0]
        assert entry.event_type == EventType.GREEKS_REFRESH_FAILED
        assert entry.position_id == "pos-5"
        # ``mon-`` prefix groups all continuous-monitor entry IDs so operators
        # can ``grep monitor.log | grep '^mon-'``; ``mon-grf-`` namespaces the
        # greeks-refresh subsystem within that union.
        assert entry.entry_id.startswith("mon-grf-")
        detail = entry.detail
        assert isinstance(detail, GreeksRefreshFailedDetail)
        assert detail.underlying_ticker == "AAPL"
        assert detail.prior_as_of == last
        assert detail.failure_reason == "iv_fetch_no_row"

    async def test_provider_exception_is_treated_as_failure(self) -> None:
        """An exception from the IV provider is caught, refresh_failed=True is
        written, and the activity log records the failure with a reason tagged
        from the exception class."""
        config = _config(interval_minutes=15, move_threshold_pct=2.0)
        now = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        last = now - timedelta(minutes=20)
        position = _options_position(position_id=PositionId("pos-6"), as_of_timestamp=last)
        cache = await _seeded_cache({"AAPL": 200.0}, now)
        iv_provider = FakeIVProvider(raises=RuntimeError("simulated"))
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()
        states = {
            "pos-6": LastRefreshState(
                position_id=PositionId("pos-6"),
                last_refreshed_at=last,
                underlying_price_at_last_refresh=200.0,
            )
        }

        await _run_refresh_cycle(
            config=config,
            repository=FakeRepository((position,)),
            cache=cache,
            states=states,
            iv_fetch=iv_provider.fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=now,
            invocation_id_provider=_const_str("inv-test-006"),
            market_open=True,
        )

        assert len(writer.options_writes) == 1
        _, greeks = writer.options_writes[0]
        assert greeks.refresh_failed is True
        assert len(activity_log.entries) == 1
        detail = activity_log.entries[0].detail
        assert isinstance(detail, GreeksRefreshFailedDetail)
        assert detail.failure_reason == "iv_fetch_db_error"

    async def test_failure_bumps_last_refreshed_at_to_avoid_per_cycle_flood(self) -> None:
        """Regression — a position whose refresh fails advances its scheduled
        anchor so the next inspection-cycle does not re-fire instantly.

        Without this guard, ``last_refreshed_at`` stays at the original anchor
        (>= the scheduled interval ago) and every subsequent cycle re-evaluates
        the failure, emitting a fresh ``GREEKS_REFRESH_FAILED`` entry every
        ``greeks_refresh_inspection_cadence_seconds`` indefinitely.

        After the fix, the cycle bumps the anchor to ``ctx.now`` and the
        next-cycle scheduled trigger does not re-fire until a full interval
        has elapsed. (The move-trigger still re-fires if spot drifts past the
        threshold mid-interval.)
        """
        config = _config(interval_minutes=15, move_threshold_pct=2.0)
        now1 = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        last = now1 - timedelta(minutes=20)
        position = _options_position(position_id=PositionId("pos-flood"), as_of_timestamp=last)
        cache = await _seeded_cache({"AAPL": 200.0}, now1)
        iv_provider = FakeIVProvider(quotes={})  # always fails
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()
        states = {
            "pos-flood": LastRefreshState(
                position_id=PositionId("pos-flood"),
                last_refreshed_at=last,
                underlying_price_at_last_refresh=200.0,
            )
        }

        # Cycle 1: position is overdue → fails → bumps anchor to now1.
        await _run_refresh_cycle(
            config=config,
            repository=FakeRepository((position,)),
            cache=cache,
            states=states,
            iv_fetch=iv_provider.fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=now1,
            invocation_id_provider=_const_str("inv-flood-1"),
            market_open=True,
        )
        assert len(activity_log.entries) == 1
        assert states["pos-flood"].last_refreshed_at == now1
        assert states["pos-flood"].underlying_price_at_last_refresh == 200.0

        # Cycle 2: 30 seconds later — well under the 15-minute interval, no
        # underlying move. Position is NOT due; no new failure entry.
        now2 = now1 + timedelta(seconds=30)
        await _run_refresh_cycle(
            config=config,
            repository=FakeRepository((position,)),
            cache=cache,
            states=states,
            iv_fetch=iv_provider.fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=now2,
            invocation_id_provider=_const_str("inv-flood-2"),
            market_open=True,
        )
        assert len(activity_log.entries) == 1, (
            "second cycle re-fired a GREEKS_REFRESH_FAILED entry — anchor was not bumped"
        )


class TestOffHours:
    async def test_market_closed_yields_no_writes_and_no_emissions(self) -> None:
        """Per architecture § 4d: outside market hours the loop pauses; theta
        accrues via Δt but the task does no work."""
        config = _config(interval_minutes=15, move_threshold_pct=2.0)
        now = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        last = now - timedelta(minutes=60)  # very stale
        position = _options_position(position_id=PositionId("pos-7"), as_of_timestamp=last)
        cache = await _seeded_cache({"AAPL": 300.0}, now)  # absurd move
        iv_provider = FakeIVProvider()
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()
        states = {
            "pos-7": LastRefreshState(
                position_id=PositionId("pos-7"),
                last_refreshed_at=last,
                underlying_price_at_last_refresh=200.0,
            )
        }

        await _run_refresh_cycle(
            config=config,
            repository=FakeRepository((position,)),
            cache=cache,
            states=states,
            iv_fetch=iv_provider.fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=now,
            invocation_id_provider=_const_str("inv-test-007"),
            market_open=False,
        )

        assert writer.options_writes == []
        assert iv_provider.calls == []
        assert activity_log.entries == []


class TestStrategyRefresh:
    async def test_strategy_position_refreshes_per_leg_and_aggregate(self) -> None:
        config = _config(interval_minutes=15, move_threshold_pct=2.0)
        now = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        last = now - timedelta(minutes=20)
        position = _strategy_position(position_id=PositionId("strat-1"), as_of_timestamp=last)
        cache = await _seeded_cache({"SPY": 500.0}, now)
        details = position.details
        assert isinstance(details, StrategyPositionDetails)
        leg_one_symbol = occ_symbol_for_options(details.legs[0].options)
        leg_two_symbol = occ_symbol_for_options(details.legs[1].options)
        iv_provider = FakeIVProvider(
            quotes={
                leg_one_symbol: IVQuote(occ_symbol=leg_one_symbol, iv=0.22, as_of=now),
                leg_two_symbol: IVQuote(occ_symbol=leg_two_symbol, iv=0.20, as_of=now),
            }
        )
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()
        states = {
            "strat-1": LastRefreshState(
                position_id=PositionId("strat-1"),
                last_refreshed_at=last,
                underlying_price_at_last_refresh=500.0,
            )
        }

        await _run_refresh_cycle(
            config=config,
            repository=FakeRepository((position,)),
            cache=cache,
            states=states,
            iv_fetch=iv_provider.fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=now,
            invocation_id_provider=_const_str("inv-test-008"),
            market_open=True,
        )

        # The strategy writer was invoked once with per-leg + aggregated greeks.
        assert len(writer.strategy_writes) == 1
        pid, per_leg, aggregated = writer.strategy_writes[0]
        assert pid == "strat-1"
        assert set(per_leg.keys()) == {"leg-1", "leg-2"}
        # Each leg's greeks carry refresh metadata.
        for leg_greeks in per_leg.values():
            assert leg_greeks.refresh_failed is False
            assert leg_greeks.as_of_timestamp == now
        # Aggregated greeks present.
        assert aggregated.refresh_failed is False
        assert aggregated.as_of_timestamp == now
        # Anchor advanced.
        assert states["strat-1"].last_refreshed_at == now


# ---------------------------------------------------------------------------
# run_greeks_refresh — supervisor-facing run-forever entry point
# ---------------------------------------------------------------------------


class TestRunForeverEntryPoint:
    async def test_cancellation_exits_cleanly(self) -> None:
        """The supervisor cancels every task at shutdown; the loop must exit
        without raising ``CancelledError`` upward (per the supervisor's
        contract)."""
        config = _config(inspection_cadence=1)
        cache = UnderlyingPriceCache()
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()

        async def _no_op_fetch(_symbols: Iterable[str]) -> dict[str, IVQuote]:
            return {}

        task = asyncio.create_task(
            run_greeks_refresh(
                _session(),
                config,
                repository=FakeRepository(()),
                cache=cache,
                iv_fetch=_no_op_fetch,
                writer=writer,
                activity_log=activity_log.emit,
                risk_free_rate_provider=_const_float(0.045),
                now=lambda: datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
                market_open=lambda _: True,
                invocation_id_provider=_const_str("inv-runforever-001"),
                sleep=asyncio.sleep,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_loop_drives_refresh_cycles(self) -> None:
        """The loop drives multiple refresh cycles, advancing the per-position
        anchor on each successful refresh. A short-circuit sleep keeps the test
        wall-clock-fast."""
        config = _config(interval_minutes=15, inspection_cadence=1)
        now_holder = [datetime(2026, 5, 11, 14, 0, tzinfo=UTC)]

        def _now() -> datetime:
            return now_holder[0]

        last = now_holder[0] - timedelta(minutes=20)
        position = _options_position(position_id=PositionId("pos-loop-1"), as_of_timestamp=last)
        cache = await _seeded_cache({"AAPL": 200.0}, _now())
        symbol = occ_symbol_for_options(position.details)  # type: ignore[arg-type]
        iv_provider = FakeIVProvider(
            quotes={symbol: IVQuote(occ_symbol=symbol, iv=0.30, as_of=_now())}
        )
        writer = FakeGreeksWriter()
        activity_log = FakeActivityLog()

        cycles_done = asyncio.Event()

        async def _instrumented_sleep(_seconds: float) -> None:
            # Mark first cycle complete, then sleep briefly so the test can stop.
            cycles_done.set()
            await asyncio.sleep(0)

        task = asyncio.create_task(
            run_greeks_refresh(
                _session(),
                config,
                repository=FakeRepository((position,)),
                cache=cache,
                iv_fetch=iv_provider.fetch,
                writer=writer,
                activity_log=activity_log.emit,
                risk_free_rate_provider=_const_float(0.045),
                now=_now,
                market_open=lambda _: True,
                invocation_id_provider=_const_str("inv-loop-001"),
                sleep=_instrumented_sleep,
            )
        )
        await cycles_done.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(writer.options_writes) >= 1
