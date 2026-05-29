"""Tests for the entry-window expiry watcher cycle (ALP-737).

The run-forever task exposes its single-iteration kernel
(``_run_entry_window_cycle``) for testability: each test drives one cycle with
a fake bracket reader + a capturing fake canceller and asserts which brackets
fired. The canceller seam (broker cancel + writeback) is exercised separately
in ``test_canceller.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from alphamind._kernel.ids import BracketId, OrderId, PositionId, Symbol
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    EntryWindowCancelOutcome,
)
from alphamind.execution.continuous_monitor.entry_window.task import (
    _run_entry_window_cycle,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    PriceTrigger,
)

_NOW = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)


def _pending_entry_bracket(
    *,
    bracket_id: str,
    deadline: datetime | None,
) -> BracketRecord:
    """A minimal valid PENDING_ENTRY bracket carrying an entry-window deadline."""
    price_stop = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{bracket_id}-ord-stop"),
        trigger=PriceTrigger(underlying_ticker=Symbol("ZS"), threshold_usd=140.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(f"POS-{bracket_id}"),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId(f"{bracket_id}-ord-entry"),
        protective_legs=(price_stop,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=deadline,
    )


@dataclass
class _FakeReader:
    brackets: tuple[BracketRecord, ...]

    async def get_pending_entry_brackets(self) -> tuple[BracketRecord, ...]:
        return self.brackets


@dataclass
class _FakeCanceller:
    outcome: EntryWindowCancelOutcome = EntryWindowCancelOutcome.CANCELLED
    calls: list[str] = field(default_factory=list)

    async def cancel(self, *, bracket: BracketRecord, now: datetime) -> EntryWindowCancelOutcome:
        del now
        self.calls.append(bracket.bracket_id)
        return self.outcome


def _config() -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=1,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=1.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=3,
        supervisor_shutdown_timeout_seconds=5,
    )


async def test_cycle_cancels_only_expired_brackets() -> None:
    """Only a bracket whose deadline is strictly before ``now`` fires; a
    future-deadline bracket is left to keep resting (ALP-737 AC2)."""
    expired = _pending_entry_bracket(bracket_id="BRK-EXPIRED", deadline=_NOW - timedelta(hours=1))
    future = _pending_entry_bracket(bracket_id="BRK-FUTURE", deadline=_NOW + timedelta(hours=1))
    canceller = _FakeCanceller()
    fired: set[str] = set()

    await _run_entry_window_cycle(
        config=_config(),
        bracket_reader=_FakeReader((expired, future)),
        canceller=canceller,
        now=_NOW,
        fired=fired,
    )

    assert canceller.calls == ["BRK-EXPIRED"]
    assert fired == {"BRK-EXPIRED"}


async def test_cycle_does_not_fire_at_exact_deadline() -> None:
    """A bracket exactly at its deadline does NOT fire — the contract is a
    strict ``now() > deadline`` (orders.py BracketRecord lifecycle)."""
    at_deadline = _pending_entry_bracket(bracket_id="BRK-AT", deadline=_NOW)
    canceller = _FakeCanceller()
    fired: set[str] = set()

    await _run_entry_window_cycle(
        config=_config(),
        bracket_reader=_FakeReader((at_deadline,)),
        canceller=canceller,
        now=_NOW,
        fired=fired,
    )

    assert canceller.calls == []
    assert fired == set()


async def test_cycle_skips_already_fired_brackets() -> None:
    """A bracket already cancelled this session is not re-fired even while the
    DB still shows it ``PENDING_ENTRY`` (pre-reconciliation)."""
    expired = _pending_entry_bracket(bracket_id="BRK-1", deadline=_NOW - timedelta(hours=1))
    canceller = _FakeCanceller()
    fired = {"BRK-1"}

    await _run_entry_window_cycle(
        config=_config(),
        bracket_reader=_FakeReader((expired,)),
        canceller=canceller,
        now=_NOW,
        fired=fired,
    )

    assert canceller.calls == []


async def test_cycle_failed_outcome_is_retried_next_cycle() -> None:
    """A transient broker failure does NOT mark the bracket fired, so the next
    cycle attempts the cancel again."""
    expired = _pending_entry_bracket(bracket_id="BRK-1", deadline=_NOW - timedelta(hours=1))
    canceller = _FakeCanceller(outcome=EntryWindowCancelOutcome.FAILED)
    fired: set[str] = set()

    await _run_entry_window_cycle(
        config=_config(),
        bracket_reader=_FakeReader((expired,)),
        canceller=canceller,
        now=_NOW,
        fired=fired,
    )

    assert canceller.calls == ["BRK-1"]
    assert fired == set()


async def test_cycle_already_filled_outcome_is_not_retried() -> None:
    """When the broker says the entry already filled, the bracket is marked
    handled (reconciliation will activate it) and is not re-fired."""
    expired = _pending_entry_bracket(bracket_id="BRK-1", deadline=_NOW - timedelta(hours=1))
    canceller = _FakeCanceller(outcome=EntryWindowCancelOutcome.SKIPPED_FILLED)
    fired: set[str] = set()

    await _run_entry_window_cycle(
        config=_config(),
        bracket_reader=_FakeReader((expired,)),
        canceller=canceller,
        now=_NOW,
        fired=fired,
    )

    assert fired == {"BRK-1"}


async def test_cycle_swallows_per_bracket_canceller_error() -> None:
    """A canceller exception is logged and skipped — one bad bracket never
    stalls the rest of the cycle, and it is retried (not marked fired)."""

    @dataclass
    class _RaisingCanceller:
        calls: list[str] = field(default_factory=list)

        async def cancel(
            self, *, bracket: BracketRecord, now: datetime
        ) -> EntryWindowCancelOutcome:
            del now
            self.calls.append(bracket.bracket_id)
            if bracket.bracket_id == "BRK-BAD":
                raise RuntimeError("broker exploded")
            return EntryWindowCancelOutcome.CANCELLED

    bad = _pending_entry_bracket(bracket_id="BRK-BAD", deadline=_NOW - timedelta(hours=1))
    good = _pending_entry_bracket(bracket_id="BRK-GOOD", deadline=_NOW - timedelta(hours=1))
    canceller = _RaisingCanceller()
    fired: set[str] = set()

    await _run_entry_window_cycle(
        config=_config(),
        bracket_reader=_FakeReader((bad, good)),
        canceller=canceller,
        now=_NOW,
        fired=fired,
    )

    assert canceller.calls == ["BRK-BAD", "BRK-GOOD"]
    assert fired == {"BRK-GOOD"}  # the bad one is retried, not marked handled
