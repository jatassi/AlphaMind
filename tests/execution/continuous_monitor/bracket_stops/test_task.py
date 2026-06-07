"""Tests for ``run_options_bracket_watcher`` (story 04c / ALP-440).

The run-forever task exposes its single-iteration kernel for testability:
each test drives one cycle with the surrounding fakes for the position
repository, the bracket repository, the underlying-price cache, the
broker-call submitter, and the activity-log emitter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

import pytest

from alphamind._kernel.ids import (
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.commands.command_models import (
    BracketOrderParameters,
    PriceCondition,
    PriceLeg,
)
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
from alphamind.execution.write_paths.command_execution.open import _wire_leg_to_bracket_leg
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
    EnforcementBinding,
    PLAnchorSpec,
    PriceTrigger,
    TriggerSignal,
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
)

_NOW = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)


def _const_str(value: str) -> Callable[[], Awaitable[str]]:
    """Async-callable returning *value* — async-native provider seam for tests."""

    async def _inner() -> str:
        return value

    return _inner


def _const_float(value: float) -> Callable[[], Awaitable[float]]:
    """Async-callable returning *value* — async-native provider seam for tests."""

    async def _inner() -> float:
        return value

    return _inner


def _trigger_ids(*, session_id: str = "mon-S") -> TriggerIdGenerator:
    """Fresh trigger-id generator scoped to *session_id* for isolation across tests."""
    return TriggerIdGenerator(session_id=session_id)


def _config(*, cadence: float = 1.0, max_age_seconds: float = 900.0) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        greeks_refresh_inspection_cadence_seconds=30,
        bracket_stop_evaluation_cadence_seconds=cadence,
        underlying_price_max_age_seconds=max_age_seconds,
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
        position_id=PositionId(position_id),
        thesis_id=ThesisId("THE-NVDA-0123456789abcdef0123456789abcdef"),
        bracket_id=BracketId(bracket_id),
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=_NOW,
        details=OptionsPositionDetails(
            underlying_ticker=Symbol(underlying),
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
                fill_price=price(premium_paid),
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
            underlying_ticker=Symbol("NVDA"),
            threshold_usd=threshold,
            direction=direction,  # type: ignore[arg-type]
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
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
            underlying_ticker=Symbol("NVDA"),
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
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _broker_enforced_floor_bracket(
    *,
    bracket_id: str = "brk-1",
    position_id: str = "pos-1",
    floor_price: float = 865.0,
) -> BracketRecord:
    """A bracket whose stop leg is the BROKER_ENFORCED capital floor (ALP-856).

    The floor rests at the broker (a GTC ``stop_limit``); the monitor never
    submits a close for it — that would double-close against the broker's own
    fill. A second MONITOR_ENFORCED mechanical leg satisfies the hard-backstop
    requirement so the record is valid.
    """
    floor_leg = BracketLeg(
        # Production form: f"{bracket_id}-leg-floor"
        # (open._capital_floor_bracket_leg).
        leg_id=f"{bracket_id}-leg-floor",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("alpaca-floor-uuid"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"),
            threshold_usd=floor_price,
            direction="LTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
        enforcement_binding=EnforcementBinding.BROKER_ENFORCED,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(floor_leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _monitor_stop_plus_floor_bracket(
    *,
    bracket_id: str = "brk-1",
    position_id: str = "pos-1",
    stop_threshold: float = 865.0,
    floor_order_id: str = "ORD-FLOOR-xyz",
) -> BracketRecord:
    """A bracket with a MONITOR_ENFORCED stop AND a BROKER_ENFORCED resting floor.

    The monitor stop fires the close; the resting broker floor must then be
    cancelled (cancel-on-monitor-fire) so no orphaned floor lingers against a
    now-closing position (ALP-856). The floor leg's ``order_id`` is the floor's
    durable OMS order_id (the FK target / precommitted OrderRow), NOT the alpaca
    id — the closer resolves it → the OrderRow's ``alpaca_order_id`` before
    cancelling.
    """
    monitor_stop = BracketLeg(
        leg_id="leg-monitor-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"),
            threshold_usd=stop_threshold,
            direction="LTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
        enforcement_binding=EnforcementBinding.MONITOR_ENFORCED,
    )
    floor_leg = BracketLeg(
        # Production form: f"{bracket_id}-leg-floor"
        # (open._capital_floor_bracket_leg) — the closer matches the floor by this
        # suffix (FL11), so the fixture must mint the real id.
        leg_id=f"{bracket_id}-leg-floor",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(floor_order_id),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"),
            threshold_usd=stop_threshold - 1.0,  # deeper than the monitor stop
            direction="LTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
        enforcement_binding=EnforcementBinding.BROKER_ENFORCED,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(monitor_stop, floor_leg),
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
    cancelled_floors: list[str] = field(default_factory=list)

    async def submit_options_close(
        self,
        *,
        position: PositionRecord,
        details: OptionsPositionDetails,
        client_order_id: str,
        trigger_reason: PositionExitMethod,
    ) -> CloseSubmissionResult:
        del details, trigger_reason
        self.options_calls.append((position.position_id, client_order_id))
        return CloseSubmissionResult(order_ids=(client_order_id,), mode="single_leg")

    async def submit_strategy_close(
        self,
        *,
        position: PositionRecord,
        details: object,
        client_order_id_base: str,
        trigger_reason: PositionExitMethod,
    ) -> CloseSubmissionResult:
        del details, trigger_reason
        self.strategy_calls.append(position.position_id)
        return CloseSubmissionResult(order_ids=(client_order_id_base,), mode="strategy_combined")

    async def cancel_floor(self, *, alpaca_order_id: str) -> None:
        self.cancelled_floors.append(alpaca_order_id)


@dataclass
class FakeActivityLog:
    entries: list[ActivityLogEntry] = field(default_factory=list)

    async def emit(self, entry: ActivityLogEntry) -> None:
        self.entries.append(entry)


async def _seed_cache(prices: dict[str, float]) -> UnderlyingPriceCache:
    cache = UnderlyingPriceCache()
    for ticker, value in prices.items():
        await cache.update(UnderlyingQuote(ticker=ticker, price=value, as_of=_NOW))
    return cache


async def _seed_cache_stale(
    prices: dict[str, float],
    *,
    age_seconds: float,
) -> UnderlyingPriceCache:
    """Seed the cache with quotes whose ``as_of`` is *age_seconds* before ``_NOW``.

    Use together with ``now=_NOW`` and ``underlying_price_max_age_seconds`` in the
    config to control freshness in tests: a quote seeded ``age_seconds >
    max_age_seconds`` reads ``STALE``; one seeded at exactly ``_NOW`` reads ``FRESH``.
    """
    cache = UnderlyingPriceCache()
    stale_as_of = _NOW - timedelta(seconds=age_seconds)
    for ticker, value in prices.items():
        await cache.update(UnderlyingQuote(ticker=ticker, price=value, as_of=stale_as_of))
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


class TestBrokerEnforcedFloorAbsorb:
    """ALP-856 absorb-on-broker-fire — the monitor never closes a broker floor.

    The broker-enforced capital floor rests at the broker. When it fills, the
    monitor must NOT also submit a close — that double-closes against the broker's
    own fill. The monitor enforces this by dropping a BROKER_ENFORCED leg from
    firing eligibility: even when the floor's trigger condition reads satisfied,
    the monitor leaves the close to the broker.
    """

    async def test_monitor_does_not_close_a_broker_enforced_floor(self) -> None:
        position = _options_position(direction=Direction.LONG)
        # Spot below the floor → the floor's condition is satisfied. A
        # monitor-enforced leg would fire here; the broker floor must not.
        bracket = _broker_enforced_floor_bracket(floor_price=865.0)
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

        # The broker owns the floor's close — the monitor submits nothing.
        assert submitter.options_calls == []
        assert log.entries == []


class TestCancelOnMonitorFire:
    """ALP-856 cancel-on-monitor-fire — a fired monitor stop cancels the floor.

    When a MONITOR_ENFORCED stop fires and closes the position, the resting
    BROKER_ENFORCED floor (a GTC ``stop_limit``) must be cancelled — the position
    no longer exists, so an uncancelled floor would orphan against it. The cancel
    targets the floor leg's broker ``order_id``.
    """

    async def test_monitor_fire_cancels_the_resting_broker_floor(self) -> None:
        position = _options_position(direction=Direction.LONG)
        bracket = _monitor_stop_plus_floor_bracket(
            stop_threshold=865.0, floor_order_id="ORD-FLOOR-xyz"
        )
        cache = await _seed_cache({"NVDA": 860.0})  # below the stop → fires
        submitter = FakeSubmitter()
        log = FakeActivityLog()

        # The floor leg's order_id is the OMS order_id; the resolver maps it to
        # the floor OrderRow's broker alpaca id (the sanctioned DB seam).
        async def _resolver(order_id: str) -> str | None:
            return "alpaca-floor-uuid" if order_id == "ORD-FLOOR-xyz" else None

        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=log.emit,
            invocation_id_provider=_const_str("inv-001"),
            floor_alpaca_id_resolver=_resolver,
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=set(),
        )

        # The monitor stop fired its close ...
        assert len(submitter.options_calls) == 1
        # ... and the resting broker floor was cancelled by the RESOLVED broker
        # id — no orphan remains.
        assert submitter.cancelled_floors == ["alpaca-floor-uuid"]

    async def test_no_floor_cancel_when_no_broker_floor_present(self) -> None:
        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")  # monitor-only
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

        assert len(submitter.options_calls) == 1
        assert submitter.cancelled_floors == []


class TestPreSubmitFailureRetries:
    """CL1 — a pre-submit failure must NOT permanently silence the protective leg.

    The fail-safe invariant: a leg is marked fired only once the broker submit has
    actually been ATTEMPTED. A raise BEFORE the submit (a transient DB error inside
    the invocation-id provider, a ValueError building the engine client_order_id)
    leaves the leg un-fired so the next cycle retries — never an unprotected
    position for the rest of the session.
    """

    async def test_invocation_provider_failure_does_not_mark_fired(self) -> None:
        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = await _seed_cache({"NVDA": 860.0})
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        fired_legs: set[tuple[str, str]] = set()

        calls = {"n": 0}

        async def _flaky_invocation_id() -> str:
            # First cycle: the provider's session hits a transient DB error
            # BEFORE the broker submit. Subsequent cycles recover.
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient DB error resolving invocation id")
            return "inv-001"

        # Cycle 1: the pre-submit provider raises. No close is submitted and — the
        # bug being fixed — the leg must NOT be marked fired.
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=log.emit,
            invocation_id_provider=_flaky_invocation_id,
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=fired_legs,
        )
        assert submitter.options_calls == []
        assert log.entries == []
        # The leg stayed un-fired — the next cycle is free to retry.
        assert fired_legs == set()

        # Cycle 2: the provider recovers; the leg fires and a close is submitted.
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=log.emit,
            invocation_id_provider=_flaky_invocation_id,
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=fired_legs,
        )
        assert len(submitter.options_calls) == 1
        assert len(log.entries) == 1
        assert fired_legs == {("brk-1", "leg-stop-1")}

    async def test_post_submit_failure_stays_fired(self) -> None:
        """The complement: a POST-submit raise keeps the leg fired (the order may
        have reached the broker — re-firing risks a double-close)."""
        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = await _seed_cache({"NVDA": 860.0})
        log = FakeActivityLog()
        fired_legs: set[tuple[str, str]] = set()

        @dataclass
        class _RaisingSubmitter:
            calls: int = 0

            async def submit_options_close(
                self,
                *,
                position: PositionRecord,
                details: OptionsPositionDetails,
                client_order_id: str,
                trigger_reason: PositionExitMethod,
            ) -> CloseSubmissionResult:
                del position, details, client_order_id, trigger_reason
                self.calls += 1
                raise RuntimeError("broker submit reached the gateway then failed")

            async def submit_strategy_close(
                self,
                *,
                position: PositionRecord,
                details: object,
                client_order_id_base: str,
                trigger_reason: PositionExitMethod,
            ) -> CloseSubmissionResult:
                raise NotImplementedError

            async def cancel_floor(self, *, alpaca_order_id: str) -> None:
                del alpaca_order_id

        submitter = _RaisingSubmitter()
        # Two cycles: the submit raises both times, but the leg stays fired after
        # the first attempt so the second cycle does NOT re-attempt the submit.
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
        # Exactly one submit attempt — the post-submit raise kept the leg fired.
        assert submitter.calls == 1
        assert fired_legs == {("brk-1", "leg-stop-1")}


class TestPerPositionIsolation:
    """CL2 — one position's failure skips ONLY that position, not the whole tick.

    The per-leg evaluators swallow their own ValueErrors; an unexpected error that
    escapes them (e.g. a NET_MARK stop mis-routed onto a single-option position →
    TypeError) must not abort the cycle and silently leave every remaining
    position's protective stop unevaluated (the silent-wedge class).
    """

    async def test_one_position_failure_does_not_skip_the_others(self) -> None:
        # Position A carries a NET_MARK stop but is a SINGLE-OPTION position — the
        # strategy net-mark evaluator raises TypeError (an uncaught mis-route) that
        # escapes _evaluate_bracket_legs.
        position_a = _options_position(position_id="pos-A", bracket_id="brk-A")
        misrouted_leg = BracketLeg(
            leg_id="leg-misroute",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=None,
            trigger=PriceTrigger(
                underlying_ticker=Symbol("NVDA"), threshold_usd=1.0, direction="GTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
            trigger_signal=TriggerSignal.NET_MARK,  # NET_MARK on a single option → TypeError
        )
        bracket_a = BracketRecord(
            bracket_id=BracketId("brk-A"),
            position_id=PositionId("pos-A"),
            status=BracketStatus.ACTIVE,
            entry_order_id=OrderId("ord-A"),
            protective_legs=(misrouted_leg,),
            modification_history=(),
            corporate_action_cancellation_reason=None,
        )
        # Position B is a normal directional price-stop that SHOULD fire this tick.
        position_b = _options_position(position_id="pos-B", bracket_id="brk-B")
        bracket_b = _price_stop_bracket(bracket_id="brk-B", position_id="pos-B", threshold=865.0)

        cache = await _seed_cache({"NVDA": 860.0})  # below 865 → position B fires
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        # Order A first so its failure would, pre-fix, abort the tick before B.
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((position_a, position_b)),
            bracket_repository=FakeBracketRepository((bracket_a, bracket_b)),
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
        # Position B's stop still fired despite position A's TypeError mis-route.
        assert submitter.options_calls == [("pos-B", submitter.options_calls[0][1])]
        assert len(log.entries) == 1
        assert log.entries[0].position_id == "pos-B"


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
        # The engine client_order_id carries the broker-carried link (ALP-844):
        # the position's thesis + the current invocation.
        assert len(submitter.options_calls) == 1
        _, client_order_id = submitter.options_calls[0]
        assert position.thesis_id is not None
        expected_brk = derive_engine_command_id(
            monitor_session_id="mon-X",
            trigger_id=2,
            command_ordinal=0,
            thesis_id=position.thesis_id,
            invocation_id="inv-001",
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
# Strategy P/L target firing
# ---------------------------------------------------------------------------


def _credit_spread_net_premium_at(entry_spot: float) -> float:
    """Net premium (credit-negative) for a bull put credit spread at *entry_spot*.

    Short 850 put, long 840 put — the signed sum (SHORT subtracts) is the
    credit received, expressed credit-negative.
    """
    from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_price
    from alphamind.risk_guardrails.guardrail_evaluation.types import ContractType

    ttm = (date(2026, 6, 19) - _NOW.date()).days / 365
    short_put = bs_price(
        spot=entry_spot,
        strike=850.0,
        time_to_expiration_years=ttm,
        risk_free_rate=0.045,
        implied_volatility=0.30,
        contract_type=ContractType.PUT,
    )
    long_put = bs_price(
        spot=entry_spot,
        strike=840.0,
        time_to_expiration_years=ttm,
        risk_free_rate=0.045,
        implied_volatility=0.30,
        contract_type=ContractType.PUT,
    )
    return (-short_put + long_put) * 100.0


def _strategy_leg(*, leg_id: str, strike: float, direction: Direction) -> StrategyLeg:
    return StrategyLeg(
        leg_id=leg_id,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=strike,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.PUT,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=0.0,
            greeks=OptionGreeks(
                delta=-0.3,
                gamma=0.02,
                theta=-0.04,
                vega=0.2,
                as_of_timestamp=_NOW,
                iv_used=0.30,
            ),
        ),
        direction=direction,
    )


def _credit_strategy_position(*, net_premium_usd: float) -> PositionRecord:
    return PositionRecord(
        position_id=PositionId("pos-strat-1"),
        thesis_id=ThesisId("THE-NVDA-0123456789abcdef0123456789abcdef"),
        bracket_id=BracketId("brk-strat-1"),
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=_NOW,
        details=StrategyPositionDetails(
            strategy_type_label="vertical_spread",
            legs=(
                _strategy_leg(leg_id="leg-short", strike=850.0, direction=Direction.SHORT),
                _strategy_leg(leg_id="leg-long", strike=840.0, direction=Direction.LONG),
            ),
            net_premium_usd=net_premium_usd,
            max_profit_usd=abs(net_premium_usd),
            max_loss_usd=-(1000.0 - abs(net_premium_usd)),
            breakeven_levels=(),
            strategy_greeks=OptionGreeks(delta=0.1, gamma=0.0, theta=0.01, vega=-0.05),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=price(1.0),
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


def _strategy_pl_target_bracket(*, target_pct: float = 0.50) -> BracketRecord:
    leg = BracketLeg(
        leg_id="leg-strat-target",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"),
            threshold_usd=850.0,
            direction="GTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
        pl_anchor=PLAnchorSpec(
            spec_type="target",
            pct=target_pct,
            planned_entry_price=3.0,
        ),
    )
    return BracketRecord(
        bracket_id=BracketId("brk-strat-1"),
        position_id=PositionId("pos-strat-1"),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-strat"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


class TestStrategyPLTargetFiring:
    """Strategy P/L-target brackets are no longer disabled (ALP-601) — a
    credit strategy's take-profit routes into the strategy evaluator."""

    async def test_strategy_pl_target_fires_when_net_pl_at_target(self) -> None:
        net_premium = _credit_spread_net_premium_at(entry_spot=850.0)
        position = _credit_strategy_position(net_premium_usd=net_premium)
        bracket = _strategy_pl_target_bracket(target_pct=0.50)
        # Spot far above both strikes → spread near-worthless → net P/L ~ full
        # credit, well past the 50%-of-credit target → fires.
        cache = await _seed_cache({"NVDA": 1000.0})
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
        assert len(submitter.strategy_calls) == 1
        assert log.entries[0].detail.exit_method is PositionExitMethod.TARGET_REACHED

    async def test_strategy_pl_target_does_not_fire_below_target(self) -> None:
        net_premium = _credit_spread_net_premium_at(entry_spot=850.0)
        position = _credit_strategy_position(net_premium_usd=net_premium)
        bracket = _strategy_pl_target_bracket(target_pct=0.50)
        # Spot at the entry level → net P/L ~ 0, below the target → no fire.
        cache = await _seed_cache({"NVDA": 850.0})
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
        assert submitter.strategy_calls == []
        assert log.entries == []


# ---------------------------------------------------------------------------
# Thesis-shaped invalidation: trigger selected by thesis nature (ALP-852)
# ---------------------------------------------------------------------------


def _directional_stop_bracket(
    *,
    threshold: float = 865.0,
    direction: str = "LTE",
) -> BracketRecord:
    """A directional thesis-invalidation PRICE_STOP firing on the underlying."""
    leg = BracketLeg(
        leg_id="leg-dir-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"),
            threshold_usd=threshold,
            direction=direction,  # type: ignore[arg-type]
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
        trigger_signal=TriggerSignal.UNDERLYING_PRICE,
    )
    return BracketRecord(
        bracket_id=BracketId("brk-1"),
        position_id=PositionId("pos-1"),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


class TestThesisShapedTriggerSelection:
    """ALP-852/861 — the thesis-invalidation stop selects its trigger by signal.

    A directional thesis's PRICE_STOP fires on the underlying; the
    non-directional (vol / spread) firing paths are covered against the REAL leg
    constructor in :class:`TestNonDirectionalStopFiresThroughRealConstructor`.
    Selection is by the leg's ``trigger_signal``, authoritative over any
    ``pl_anchor`` it happens to carry.
    """

    async def test_directional_stop_fires_on_underlying_price(self) -> None:
        """AC1 — a directional thesis's invalidation leg fires on the underlying."""
        position = _options_position(direction=Direction.LONG)
        bracket = _directional_stop_bracket(threshold=865.0, direction="LTE")
        cache = await _seed_cache({"NVDA": 860.0})  # underlying below threshold → fires
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
        assert log.entries[0].detail.exit_method is PositionExitMethod.STOP_TRIGGERED

    async def test_trigger_signal_takes_precedence_over_pl_anchor(self) -> None:
        """The leg's ``trigger_signal`` — not its ``pl_anchor`` — drives routing.

        This leg carries both ``trigger_signal=UNDERLYING_PRICE`` AND a ``stop``
        ``pl_anchor``. The pre-852 dispatch routed any pl_anchored leg into the
        option-price evaluator (geometry-only). The thesis-shaped dispatch reads
        the signal: ``UNDERLYING_PRICE`` evaluates the underlying, so the leg
        fires on the spot breach (860 < 865 LTE) regardless of the anchor —
        proving the signal is authoritative over the anchor.
        """
        position = _options_position(direction=Direction.LONG, premium_paid=12.0, iv=0.30)
        leg = BracketLeg(
            leg_id="leg-dir-with-anchor",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=None,
            trigger=PriceTrigger(
                underlying_ticker=Symbol("NVDA"), threshold_usd=865.0, direction="LTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
            trigger_signal=TriggerSignal.UNDERLYING_PRICE,
            # An anchor a directional stop would not normally carry; present here
            # only to prove the signal — not the anchor — selects the evaluator.
            pl_anchor=PLAnchorSpec(
                spec_type="stop",
                pct=0.30,
                planned_entry_price=12.0,
                actual_entry_price=12.0,
                recalculated_at_fill=True,
            ),
        )
        bracket = BracketRecord(
            bracket_id=BracketId("brk-1"),
            position_id=PositionId("pos-1"),
            status=BracketStatus.ACTIVE,
            entry_order_id=OrderId("ord-entry-1"),
            protective_legs=(leg,),
            modification_history=(),
            corporate_action_cancellation_reason=None,
        )
        cache = await _seed_cache({"NVDA": 860.0})  # underlying breach → must fire
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


# ---------------------------------------------------------------------------
# Non-directional stop built by the REAL leg constructor (ALP-861)
# ---------------------------------------------------------------------------


def _activate(leg: BracketLeg) -> BracketLeg:
    """Return *leg* with status ACTIVE — the monitor only fires ACTIVE legs.

    The real OPEN writeback builds legs PENDING_ACTIVATION; Phase 1 entry-fill
    activates them. Tests that build a leg via the production constructor flip
    that one field so the firing kernel evaluates it, leaving every other field
    exactly as the constructor produced it (in particular: NO ``pl_anchor``).
    """
    return replace(leg, status=BracketLegStatus.ACTIVE)


def _non_directional_option_stop_bracket_real(
    *,
    trigger_price: float,
    comparator: str,
) -> BracketRecord:
    """A non-directional single-option PRICE_STOP built by ``_wire_leg_to_bracket_leg``.

    Crucially this exercises the REAL production leg constructor — the leg
    carries ``trigger_signal=OPTION_PRICE`` and NO ``pl_anchor`` (the constructor
    does not build one). Pre-ALP-861 the monitor routed this into the pct-of-PnL
    evaluator, which raised ValueError on the missing anchor and never fired.
    """
    wire_leg = PriceLeg(
        type="price",
        is_hard=True,
        trigger_signal="option_price",
        condition=PriceCondition(
            underlying_trigger="NVDA",
            comparator=comparator,  # type: ignore[arg-type]
            trigger_price=price(trigger_price),
        ),
        order_parameters=BracketOrderParameters(order_type="market"),
    )
    leg = _activate(
        _wire_leg_to_bracket_leg(
            leg_id="leg-nondir-opt",
            wire_leg=wire_leg,
            leg_order_id=None,
            ticker="NVDA",
            enforcement_binding=EnforcementBinding.MONITOR_ENFORCED,
        )
    )
    return BracketRecord(
        bracket_id=BracketId("brk-1"),
        position_id=PositionId("pos-1"),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _non_directional_strategy_stop_bracket_real(
    *,
    trigger_price: float,
    comparator: str,
) -> BracketRecord:
    """A non-directional spread PRICE_STOP built by ``_wire_leg_to_bracket_leg``.

    Carries ``trigger_signal=NET_MARK`` and NO ``pl_anchor`` — the real
    constructor's output for a spread thesis-invalidation stop.
    """
    wire_leg = PriceLeg(
        type="price",
        is_hard=True,
        trigger_signal="net_mark",
        condition=PriceCondition(
            underlying_trigger="NVDA",
            comparator=comparator,  # type: ignore[arg-type]
            trigger_price=price(trigger_price),
        ),
        order_parameters=BracketOrderParameters(order_type="market"),
    )
    leg = _activate(
        _wire_leg_to_bracket_leg(
            leg_id="leg-nondir-net",
            wire_leg=wire_leg,
            leg_order_id=None,
            ticker="NVDA",
            enforcement_binding=EnforcementBinding.MONITOR_ENFORCED,
        )
    )
    return BracketRecord(
        bracket_id=BracketId("brk-strat-1"),
        position_id=PositionId("pos-strat-1"),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-strat"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


class TestNonDirectionalStopFiresThroughRealConstructor:
    """ALP-861 — the non-directional thesis-invalidation stop actually fires.

    These brackets are built by the production ``_wire_leg_to_bracket_leg``
    constructor, which produces a PRICE_STOP leg carrying ``trigger_signal`` and
    NO ``pl_anchor``. Pre-ALP-861 the monitor routed such a leg into the
    pct-of-PnL evaluator, which raised ValueError on the missing anchor and was
    silently swallowed — the stop never fired. With the absolute option-mark /
    net-mark evaluator the stop fires on ``condition.trigger_price``.
    """

    async def test_single_option_stop_fires_on_option_mark_crossing(self) -> None:
        """AC1 — a single-option non-directional stop fires when the option mark
        crosses ``condition.trigger_price``, via the real leg constructor.

        At spot 820 the long call (strike 850, IV 0.30, ~39 DTE) marks ~$21.
        ``trigger_price=25.0`` with comparator ``<=`` → the mark is below it →
        fire. The underlying (820) is far above the $25 level, so an
        underlying-route evaluation would NOT fire — proving the option-mark
        route is taken. Before ALP-861 this never fired (no ``pl_anchor``).
        """
        position = _options_position(direction=Direction.LONG, premium_paid=35.24, iv=0.30)
        bracket = _non_directional_option_stop_bracket_real(trigger_price=25.0, comparator="<=")
        cache = await _seed_cache({"NVDA": 820.0})
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
        assert log.entries[0].detail.exit_method is PositionExitMethod.STOP_TRIGGERED

    async def test_single_option_stop_does_not_fire_when_mark_above(self) -> None:
        """A single-option non-directional stop does NOT fire when the option
        mark is still above ``condition.trigger_price``.

        At spot 820 the call marks ~$21; ``trigger_price=15.0`` (``<=``) is below
        the mark → no fire.
        """
        position = _options_position(direction=Direction.LONG, premium_paid=35.24, iv=0.30)
        bracket = _non_directional_option_stop_bracket_real(trigger_price=15.0, comparator="<=")
        cache = await _seed_cache({"NVDA": 820.0})
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

    async def test_spread_stop_fires_on_net_mark_crossing(self) -> None:
        """AC2 — a spread non-directional stop fires when the net mark crosses
        ``condition.trigger_price``, via the real leg constructor.

        The bull put credit spread (short 850 put / long 840 put) was opened near
        $850, where its per-contract net mark (cost to close one spread unit) is
        ~$4.74. As NVDA falls toward the strikes the short put richens and the
        cost to buy the spread back rises; at spot 780 the net mark is ~$7.89.
        ``trigger_price=6.0`` with comparator ``>=`` → the worsening net mark has
        crossed the level → fire. (Before ALP-861 this never fired — the leg
        carries no ``pl_anchor``.)
        """
        net_premium = _credit_spread_net_premium_at(entry_spot=850.0)
        position = _credit_strategy_position(net_premium_usd=net_premium)
        bracket = _non_directional_strategy_stop_bracket_real(trigger_price=6.0, comparator=">=")
        cache = await _seed_cache({"NVDA": 780.0})
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
        assert len(submitter.strategy_calls) == 1
        assert submitter.options_calls == []

    async def test_prefill_strategy_skeleton_is_skipped_not_wedged(self) -> None:
        """A NET_MARK stop on a pre-fill strategy skeleton skips, not wedges.

        A skeleton strategy position carries zeroed greeks (``iv_used`` unset),
        so the net-mark evaluator's Black-Scholes derivation raises ValueError.
        The cycle must skip that leg and complete normally — never fire, never
        raise — the same per-leg resilience the pct-of-PnL path has.
        """
        base = _credit_strategy_position(net_premium_usd=-100.0)
        assert isinstance(base.details, StrategyPositionDetails)
        skeleton_legs = tuple(
            replace(
                leg,
                options=replace(
                    leg.options,
                    greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
                ),
            )
            for leg in base.details.legs
        )
        skeleton = replace(base, details=replace(base.details, legs=skeleton_legs))
        bracket = _non_directional_strategy_stop_bracket_real(trigger_price=1.0, comparator=">=")
        cache = await _seed_cache({"NVDA": 780.0})
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _run_bracket_stop_cycle(
            config=_config(),
            position_repository=FakePositionRepository((skeleton,)),
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
        assert submitter.strategy_calls == []
        assert submitter.options_calls == []


# ---------------------------------------------------------------------------
# Non-options positions skipped
# ---------------------------------------------------------------------------


class TestEquityPositionsSkipped:
    async def test_equity_positions_are_skipped(self) -> None:
        from alphamind.portfolio_state.records.positions import EquityPositionDetails

        equity = PositionRecord(
            position_id=PositionId("pos-eq"),
            thesis_id=None,
            bracket_id=BracketId("brk-eq"),
            status=PositionStatus.OPEN,
            direction=Direction.LONG,
            entry_timestamp=_NOW,
            details=EquityPositionDetails(
                ticker=Symbol("AAPL"), share_count=100.0, average_cost_basis_per_share=150.0
            ),
            execution_history=(
                PositionFill(
                    fill_timestamp=_NOW,
                    fill_price=price(150.0),
                    fill_quantity=100.0,
                    slippage=signed_money(0.0),
                    fees=money(0.0),
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
            trigger=PriceTrigger(
                underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        bracket = BracketRecord(
            bracket_id=BracketId("brk-eq"),
            position_id=PositionId("pos-eq"),
            status=BracketStatus.ACTIVE,
            entry_order_id=OrderId("ord-eq"),
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
            trigger=PriceTrigger(
                underlying_ticker=Symbol("NVDA"), threshold_usd=865.0, direction="LTE"
            ),
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
            bracket_id=BracketId("brk-1"),
            position_id=PositionId("pos-1"),
            status=BracketStatus.ACTIVE,
            entry_order_id=OrderId("ord-entry-1"),
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
# Freshness gate — ALP-829
# ---------------------------------------------------------------------------


class TestFreshnessGate:
    """bracket_stops must never evaluate a position against a stale or absent spot.

    AC: stale spot → skipped (no fire); fresh spot → fires; missing → skipped.
    """

    async def test_stale_spot_does_not_fire(self) -> None:
        """A spot older than ``underlying_price_max_age_seconds`` is skipped."""
        position = _options_position(direction=Direction.LONG)
        # Threshold=865 LTE; spot 860 would fire if fresh — must not fire when stale.
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        # Quote is 1 800s old; max_age is 900s → STALE.
        cache = await _seed_cache_stale({"NVDA": 860.0}, age_seconds=1800.0)
        submitter = FakeSubmitter()
        activity = FakeActivityLog()
        await _run_bracket_stop_cycle(
            config=_config(max_age_seconds=900.0),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=activity.emit,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=set(),
            stale_tickers=set(),
        )
        assert submitter.options_calls == []
        assert activity.entries == []

    async def test_fresh_spot_fires_exactly_as_before(self) -> None:
        """A spot within ``underlying_price_max_age_seconds`` fires normally."""
        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        # Quote seeded at _NOW → age 0 → FRESH.
        cache = await _seed_cache({"NVDA": 860.0})
        submitter = FakeSubmitter()
        activity = FakeActivityLog()
        await _run_bracket_stop_cycle(
            config=_config(max_age_seconds=900.0),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=activity.emit,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=set(),
            stale_tickers=set(),
        )
        assert len(submitter.options_calls) == 1
        assert len(activity.entries) == 1

    async def test_missing_spot_skips_via_gated_read(self) -> None:
        """Empty cache → MissingPrice → position skipped (unchanged behavior, gated path)."""
        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = UnderlyingPriceCache()  # empty
        submitter = FakeSubmitter()
        activity = FakeActivityLog()
        await _run_bracket_stop_cycle(
            config=_config(max_age_seconds=900.0),
            position_repository=FakePositionRepository((position,)),
            bracket_repository=FakeBracketRepository((bracket,)),
            cache=cache,
            submitter=submitter,
            activity_log=activity.emit,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-S",
            trigger_ids=_trigger_ids(),
            now=_NOW,
            risk_free_rate=0.045,
            fired_legs=set(),
            stale_tickers=set(),
        )
        assert submitter.options_calls == []
        assert activity.entries == []

    async def test_stale_log_fires_once_on_fresh_to_stale_transition(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Skipped-stale warning emits once on the fresh→stale transition, not every cycle."""
        import logging

        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = await _seed_cache_stale({"NVDA": 860.0}, age_seconds=1800.0)

        stale_tickers: set[str] = set()  # shared across cycles — tracks prior state

        async def _one_cycle() -> None:
            await _run_bracket_stop_cycle(
                config=_config(max_age_seconds=900.0),
                position_repository=FakePositionRepository((position,)),
                bracket_repository=FakeBracketRepository((bracket,)),
                cache=cache,
                submitter=FakeSubmitter(),
                activity_log=FakeActivityLog().emit,
                invocation_id_provider=_const_str("inv-001"),
                monitor_session_id="mon-S",
                trigger_ids=_trigger_ids(),
                now=_NOW,
                risk_free_rate=0.045,
                fired_legs=set(),
                stale_tickers=stale_tickers,
            )

        def _stale_warns() -> int:
            return sum(
                1 for r in caplog.records if "NVDA" in r.message and "stale" in r.message.lower()
            )

        task_logger = "alphamind.execution.continuous_monitor.bracket_stops.task"
        with caplog.at_level(logging.WARNING, logger=task_logger):
            await _one_cycle()
            first_count = _stale_warns()
            await _one_cycle()  # still stale — no additional log
            second_count = _stale_warns()

        assert first_count == 1, "stale warning should fire on first stale cycle"
        assert second_count == 1, "stale warning must not repeat on subsequent stale cycles"


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

        with pytest.raises((ValueError, TypeError)):
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

        with pytest.raises((ValueError, TypeError)):
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
# Run-forever loop: supervised_loop drives beats + pacing (ALP-829)
# ---------------------------------------------------------------------------


class TestRunForeverLoop:
    async def test_run_forever_drives_loop_via_supervised_loop(self) -> None:
        """``run_options_bracket_watcher`` iterates via the ``loop`` seam.

        The ``loop`` callable is the supervisor's ``supervised_loop`` factory
        (name + cadence pre-bound). The watcher iterates it and no longer owns
        a trailing sleep or a hand-wired beat. Three iterations complete before
        the fake loop raises ``CancelledError``.
        """
        import asyncio

        from alphamind.execution.continuous_monitor.bracket_stops.task import (
            run_options_bracket_watcher,
        )
        from alphamind.execution.continuous_monitor.session import MonitorSession

        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = await _seed_cache({"NVDA": 870.0})  # does not fire
        submitter = FakeSubmitter()
        activity = FakeActivityLog()
        iterations = 0

        async def _fake_loop() -> AsyncIterator[None]:
            nonlocal iterations
            for _ in range(3):
                iterations += 1
                yield
            raise asyncio.CancelledError

        session = MonitorSession(
            session_id="mon-20260511T143000Z-deadbeef",
            started_at=_NOW,
            mode="paper",
        )

        with pytest.raises(asyncio.CancelledError):
            await run_options_bracket_watcher(
                session,
                _config(cadence=2.5),
                position_repository=FakePositionRepository((position,)),
                bracket_repository=FakeBracketRepository((bracket,)),
                cache=cache,
                submitter=submitter,
                activity_log=activity.emit,
                invocation_id_provider=_const_str("inv-001"),
                risk_free_rate_provider=_const_float(0.045),
                trigger_ids=_trigger_ids(session_id=session.session_id),
                now=lambda: _NOW,
                loop=_fake_loop,
            )

        assert iterations == 3

    async def test_watchdog_bound_and_wedged_cycle_trip_via_supervised_loop(self) -> None:
        """ALP-829: watcher runs through ``supervised_loop`` at the bracket cadence.

        Drives the production ``run_options_bracket_watcher`` through the real
        ``supervisor.supervised_loop`` (no manual ``register_watch``) and a real
        ``_watchdog_loop``, and asserts:
        1. The ``bracket_stops`` watch entry is registered at the cadence-derived
           (tight) bound — ``cadence * watchdog_cadence_multiplier`` — not the old
           1h global.
        2. A wedged cycle (the watcher body blocks, so beats stop) trips
           ``os._exit(1)``.

        Timing is driven by a fake monotonic clock the watchdog reads; the
        watchdog's own sleep advances that clock so a stall is reached in a few
        virtual iterations without real wall-clock waits.
        """
        import asyncio
        from unittest import mock

        from alphamind.execution.continuous_monitor.bracket_stops.task import (
            run_options_bracket_watcher,
        )
        from alphamind.execution.continuous_monitor.session import MonitorSession
        from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor

        position = _options_position(direction=Direction.LONG)
        bracket = _price_stop_bracket(threshold=865.0, direction="LTE")
        cache = await _seed_cache({"NVDA": 870.0})  # would not fire anyway

        cadence = 1.0
        cfg = _config(cadence=cadence)
        expected_bound = cadence * cfg.watchdog_cadence_multiplier  # 10.0s

        clock = [0.0]

        def _monotonic() -> float:
            return clock[0]

        exits: list[int] = []

        # The watcher body wedges on its second cycle: the position read hangs
        # forever, so ``supervised_loop`` beats once then never beats again.
        class _WedgingRepo:
            def __init__(self) -> None:
                self.calls = 0

            async def get_open_positions(self) -> tuple[PositionRecord, ...]:
                self.calls += 1
                if self.calls >= 2:
                    await asyncio.Event().wait()  # block forever (wedged cycle)
                return (position,)

        async def _sleep(delay: float) -> None:
            # Both supervised_loop pacing and the watchdog check-interval route
            # through here; advancing the clock each call lets the watchdog reach
            # the stall bound after a few virtual iterations.
            await asyncio.sleep(0)
            clock[0] += delay

        session = MonitorSession(
            session_id="mon-watchdog-test",
            started_at=_NOW,
            mode="paper",
        )
        supervisor = MonitorSupervisor(
            session=session,
            config=cfg,
            sleep=_sleep,
            monotonic=_monotonic,
        )
        stop_event = asyncio.Event()
        supervisor._stop_event = stop_event

        def _record_exit_and_stop(code: int) -> None:
            exits.append(code)
            stop_event.set()  # let the watchdog loop return cleanly

        with mock.patch(
            "alphamind.execution.continuous_monitor.supervisor.os._exit",
            side_effect=_record_exit_and_stop,
        ):
            watcher_task = asyncio.ensure_future(
                run_options_bracket_watcher(
                    session,
                    cfg,
                    position_repository=_WedgingRepo(),
                    bracket_repository=FakeBracketRepository((bracket,)),
                    cache=cache,
                    submitter=FakeSubmitter(),
                    activity_log=FakeActivityLog().emit,
                    invocation_id_provider=_const_str("inv-001"),
                    risk_free_rate_provider=_const_float(0.045),
                    trigger_ids=_trigger_ids(session_id=session.session_id),
                    now=lambda: _NOW,
                    loop=lambda: supervisor.supervised_loop("bracket_stops", cadence),
                )
            )
            watchdog_task = asyncio.ensure_future(supervisor._watchdog_loop())
            try:
                # Bounded spin: wait for the watchdog to trip.
                for _ in range(100_000):
                    if exits:
                        break
                    await asyncio.sleep(0)
            finally:
                watcher_task.cancel()
                watchdog_task.cancel()
                await asyncio.gather(watcher_task, watchdog_task, return_exceptions=True)

        # 1. Registered at the cadence-derived tight bound (not a 1h global).
        entry = supervisor._watch.get("bracket_stops")
        assert entry is not None
        assert entry.bound_seconds == expected_bound

        # 2. The wedged cycle tripped the watchdog.
        assert exits == [1], "watchdog must call os._exit(1) when bracket_stops stalls"
