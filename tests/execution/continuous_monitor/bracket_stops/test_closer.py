"""Tests for ``submit_options_bracket_close`` (story 04c / ALP-440)."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import (
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.continuous_monitor.bracket_stops.closer import (
    CloseSubmissionResult,
    prepare_bracket_close,
    submit_options_bracket_close,
)
from alphamind.execution.oms.command_ids import parse_engine_command_id
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PositionClosedDetail,
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
    StrategyLeg,
    StrategyPositionDetails,
)

_NOW = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)


def _const_str(value: str) -> Callable[[], Awaitable[str]]:
    """Async-callable returning *value* — async-native provider seam for tests."""

    async def _inner() -> str:
        return value

    return _inner


_OPTIONS_THESIS_ID = "THE-NVDA-0123456789abcdef0123456789abcdef"
_STRATEGY_THESIS_ID = "THE-SPY-fedcba9876543210fedcba9876543210"


def _options_position(*, position_id: str = "pos-1") -> PositionRecord:
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(_OPTIONS_THESIS_ID),
        bracket_id=BracketId("brk-1"),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW,
        details=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=850.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=12.0,
            greeks=OptionGreeks(
                delta=0.5,
                gamma=0.02,
                theta=-0.04,
                vega=0.2,
                as_of_timestamp=_NOW,
                iv_used=0.30,
            ),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=price(12.0),
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


def _strategy_position(*, position_id: str = "pos-st") -> PositionRecord:
    leg_one = OptionsPositionDetails(
        underlying_ticker=Symbol("SPY"),
        strike_price=500.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=5.0,
        greeks=OptionGreeks(
            delta=0.5,
            gamma=0.01,
            theta=-0.02,
            vega=0.15,
            as_of_timestamp=_NOW,
            iv_used=0.25,
        ),
    )
    leg_two = OptionsPositionDetails(
        underlying_ticker=Symbol("SPY"),
        strike_price=510.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=2.0,
        greeks=OptionGreeks(
            delta=0.3,
            gamma=0.01,
            theta=-0.015,
            vega=0.12,
            as_of_timestamp=_NOW,
            iv_used=0.25,
        ),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(_STRATEGY_THESIS_ID),
        bracket_id=BracketId("brk-st"),
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=_NOW,
        details=StrategyPositionDetails(
            strategy_type_label="vertical_call_spread",
            legs=(
                StrategyLeg(leg_id="leg-1", direction=Direction.LONG, options=leg_one),
                StrategyLeg(leg_id="leg-2", direction=Direction.SHORT, options=leg_two),
            ),
            net_premium_usd=300.0,
            max_profit_usd=700.0,
            max_loss_usd=300.0,
            breakeven_levels=(503.0,),
            strategy_greeks=OptionGreeks(
                delta=0.2,
                gamma=0.0,
                theta=-0.005,
                vega=0.03,
                as_of_timestamp=_NOW,
                iv_used=0.25,
            ),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
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


def _bracket(*, position_id: str = "pos-1", floor_order_id: str | None = None) -> BracketRecord:
    leg = BracketLeg(
        leg_id="leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=865.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    legs: tuple[BracketLeg, ...] = (leg,)
    if floor_order_id is not None:
        # ALP-856 — a BROKER_ENFORCED capital-floor leg whose ``order_id`` is the
        # floor's durable OMS order_id (the precommitted floor OrderRow), NOT the
        # broker alpaca id. The closer resolves it → OrderRow.alpaca_order_id.
        floor_leg = BracketLeg(
            # Production form: f"{bracket_id}-leg-floor"
            # (open._capital_floor_bracket_leg) — the closer matches the floor by
            # this suffix, not by every BROKER_ENFORCED leg (FL11).
            leg_id="brk-1-leg-floor",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId(floor_order_id),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("NVDA"), threshold_usd=800.0, direction="LTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            enforcement_binding=EnforcementBinding.BROKER_ENFORCED,
            status=BracketLegStatus.ACTIVE,
        )
        legs = (leg, floor_leg)
    return BracketRecord(
        bracket_id=BracketId("brk-1"),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=legs,
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


@dataclass
class FakeSubmitter:
    """Captures every closer call. Configurable to simulate the strategy combined-close
    rejection (where the submitter's strategy method internally falls back to per-leg)."""

    options_calls: list[tuple[str, str]] = field(default_factory=list)
    strategy_calls: list[tuple[str, str]] = field(default_factory=list)
    strategy_returns: CloseSubmissionResult | None = None
    trigger_reasons: list[PositionExitMethod] = field(default_factory=list)
    cancelled_floors: list[str] = field(default_factory=list)
    # Unified, ordered log of every submitter call so a test can assert the
    # relative order of the floor cancel vs. the close submit (FL7 — the cancel
    # must precede the submit to shrink the double-SELL window to near-zero).
    call_sequence: list[str] = field(default_factory=list)

    async def submit_options_close(
        self,
        *,
        position: PositionRecord,
        details: OptionsPositionDetails,
        client_order_id: str,
        trigger_reason: PositionExitMethod,
    ) -> CloseSubmissionResult:
        del details
        self.options_calls.append((position.position_id, client_order_id))
        self.trigger_reasons.append(trigger_reason)
        self.call_sequence.append("submit_options_close")
        return CloseSubmissionResult(
            order_ids=(client_order_id,),
            mode="single_leg",
        )

    async def submit_strategy_close(
        self,
        *,
        position: PositionRecord,
        details: StrategyPositionDetails,
        client_order_id_base: str,
        trigger_reason: PositionExitMethod,
    ) -> CloseSubmissionResult:
        del details
        self.strategy_calls.append((position.position_id, client_order_id_base))
        self.trigger_reasons.append(trigger_reason)
        self.call_sequence.append("submit_strategy_close")
        if self.strategy_returns is not None:
            return self.strategy_returns
        return CloseSubmissionResult(
            order_ids=(client_order_id_base,),
            mode="strategy_combined",
        )

    async def cancel_floor(self, *, alpaca_order_id: str) -> None:
        self.cancelled_floors.append(alpaca_order_id)
        self.call_sequence.append("cancel_floor")


@dataclass
class FakeActivityLog:
    entries: list[ActivityLogEntry] = field(default_factory=list)

    async def emit(self, entry: ActivityLogEntry) -> None:
        self.entries.append(entry)


async def _prepare_and_submit(
    *,
    position: PositionRecord,
    bracket: BracketRecord,
    trigger_reason: PositionExitMethod,
    submitter: FakeSubmitter,
    activity_log: FakeActivityLog,
    invocation_id_provider: Callable[[], Awaitable[str]],
    monitor_session_id: str,
    trigger_id: int,
    now: datetime,
    estimated_exit_price: float,
    realized_pnl_usd: float,
    floor_alpaca_id_resolver: Callable[[str], Awaitable[str | None]] | None = None,
) -> CloseSubmissionResult:
    """Run the full closer flow: pre-submit prepare then the broker submit.

    The closer is a two-step surface (CL1): :func:`prepare_bracket_close` does the
    pre-submit work that can raise, then :func:`submit_options_bracket_close`
    performs the broker call + activity-log write. These tests exercise the closer
    end-to-end, so they thread both.
    """
    prepared = await prepare_bracket_close(
        position=position,
        monitor_session_id=monitor_session_id,
        trigger_id=trigger_id,
        invocation_id_provider=invocation_id_provider,
    )
    return await submit_options_bracket_close(
        position=position,
        bracket=bracket,
        trigger_reason=trigger_reason,
        submitter=submitter,
        activity_log=activity_log.emit,
        prepared=prepared,
        now=now,
        estimated_exit_price=estimated_exit_price,
        realized_pnl_usd=realized_pnl_usd,
        floor_alpaca_id_resolver=floor_alpaca_id_resolver,
    )


# ---------------------------------------------------------------------------
# Single-leg options close
# ---------------------------------------------------------------------------


class TestSingleLegClose:
    async def test_submits_close_via_options_path(self) -> None:
        position = _options_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        result = await _prepare_and_submit(
            position=position,
            bracket=_bracket(),
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-20260511T143000Z-aabbccdd",
            trigger_id=1,
            now=_NOW,
            estimated_exit_price=10.0,
            realized_pnl_usd=-200.0,
        )
        assert len(submitter.options_calls) == 1
        assert submitter.strategy_calls == []
        assert result.mode == "single_leg"

    async def test_persists_position_closed_activity_log(self) -> None:
        position = _options_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _prepare_and_submit(
            position=position,
            bracket=_bracket(),
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-20260511T143000Z-aabbccdd",
            trigger_id=1,
            now=_NOW,
            estimated_exit_price=10.0,
            realized_pnl_usd=-200.0,
        )
        assert len(log.entries) == 1
        entry = log.entries[0]
        assert entry.event_type is EventType.POSITION_CLOSED
        assert entry.event_group is EventGroup.POSITION_LIFECYCLE
        assert entry.source is EventSource.BRACKET_MANAGER
        assert entry.position_id == "pos-1"
        assert isinstance(entry.detail, PositionClosedDetail)
        assert entry.detail.exit_method is PositionExitMethod.STOP_TRIGGERED
        assert entry.detail.exit_price == 10.0
        assert entry.detail.realized_pnl_usd == -200.0
        # The activity-log row's order_id is the closing order ID.
        assert entry.order_id == submitter.options_calls[0][1]
        # ``mon-brk-`` prefix groups bracket-stop entries within the
        # continuous-monitor ``mon-`` vocabulary.
        assert entry.entry_id.startswith("mon-brk-")

    async def test_target_reached_exit_method_recorded(self) -> None:
        position = _options_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _prepare_and_submit(
            position=position,
            bracket=_bracket(),
            trigger_reason=PositionExitMethod.TARGET_REACHED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-20260511T143000Z-aabbccdd",
            trigger_id=2,
            now=_NOW,
            estimated_exit_price=25.0,
            realized_pnl_usd=1300.0,
        )
        entry = log.entries[0]
        assert entry.detail.exit_method is PositionExitMethod.TARGET_REACHED

    async def test_client_order_id_carries_thesis_and_invocation(self) -> None:
        """The closer's engine client_order_id carries the broker-carried link
        (ALP-844): the closed position's thesis (*why*) + the current invocation
        (*when*) woven into the canonical ``MON.<session>.<trigger>.0`` id, so
        the resulting Alpaca order self-attributes."""
        position = _options_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _prepare_and_submit(
            position=position,
            bracket=_bracket(),
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-20260511T143000Z-aabbccdd"),
            monitor_session_id="mon-20260511T143000Z-aabbccdd",
            trigger_id=42,
            now=_NOW,
            estimated_exit_price=10.0,
            realized_pnl_usd=-200.0,
        )
        client_order_id = submitter.options_calls[0][1]
        components = parse_engine_command_id(client_order_id)
        assert components.monitor_session_id == "mon-20260511T143000Z-aabbccdd"
        assert components.trigger_id == 42
        assert components.command_ordinal == 0
        assert components.thesis_id == _OPTIONS_THESIS_ID
        # The parsed invocation_id is the FULL inv-prefixed form (FK-valid
        # against invocations.invocation_id) — it equals the provider's id.
        assert components.invocation_id == "inv-20260511T143000Z-aabbccdd"

    async def test_trigger_reason_threaded_to_submitter(self) -> None:
        """The closer threads the thesis-shaped exit reason to the submitter so the
        fresh close is framed as a Monitor-enforced thesis exit (ALP-853) — not an
        engine-envelope cascade close. ``STOP_TRIGGERED`` flows through unchanged."""
        position = _options_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _prepare_and_submit(
            position=position,
            bracket=_bracket(),
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-20260511T143000Z-aabbccdd",
            trigger_id=9,
            now=_NOW,
            estimated_exit_price=10.0,
            realized_pnl_usd=-200.0,
        )
        assert submitter.trigger_reasons == [PositionExitMethod.STOP_TRIGGERED]

    async def test_thesis_less_position_raises(self) -> None:
        """A bracket-stop close on a position with no thesis_id raises rather
        than minting a thesis-less engine client_order_id (ALP-844) — the
        sentinel path the prior attempt introduced is gone."""
        position = dataclasses.replace(_options_position(), thesis_id=None)
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        with pytest.raises(ValueError, match="no thesis_id"):
            await _prepare_and_submit(
                position=position,
                bracket=_bracket(),
                trigger_reason=PositionExitMethod.STOP_TRIGGERED,
                submitter=submitter,
                activity_log=log,
                invocation_id_provider=_const_str("inv-20260511T143000Z-aabbccdd"),
                monitor_session_id="mon-20260511T143000Z-aabbccdd",
                trigger_id=42,
                now=_NOW,
                estimated_exit_price=10.0,
                realized_pnl_usd=-200.0,
            )
        assert submitter.options_calls == []


# ---------------------------------------------------------------------------
# cancel-on-monitor-fire — resolve the floor leg's OMS order_id → OrderRow's
# alpaca id → cancel_floor (ALP-856 / FS4)
# ---------------------------------------------------------------------------


class TestCancelRestingFloor:
    async def test_resolves_floor_order_id_to_alpaca_id_and_cancels(self) -> None:
        """A monitor-fired close resolves the floor leg's ``order_id`` (the floor's
        OMS order_id, the FK target) → the floor OrderRow's ``alpaca_order_id`` →
        ``submitter.cancel_floor``. The leg's ``order_id`` is NOT the alpaca id, so
        the closer must look it up (the resolver is the DB seam)."""
        resolved: list[str] = []

        async def _resolver(order_id: str) -> str | None:
            resolved.append(order_id)
            return "floor-alpaca-uuid" if order_id == "ORD-FLOOR-xyz" else None

        position = _options_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _prepare_and_submit(
            position=position,
            bracket=_bracket(floor_order_id="ORD-FLOOR-xyz"),
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-20260511T143000Z-aabbccdd",
            trigger_id=1,
            now=_NOW,
            estimated_exit_price=10.0,
            realized_pnl_usd=-200.0,
            floor_alpaca_id_resolver=_resolver,
        )
        # The OMS order_id was resolved, and the floor was cancelled by the
        # broker alpaca id the resolver returned (not the leg's order_id).
        assert resolved == ["ORD-FLOOR-xyz"]
        assert submitter.cancelled_floors == ["floor-alpaca-uuid"]

    async def test_cancels_resting_floor_before_submitting_the_close(self) -> None:
        """FL7 — the resting GTC floor is cancelled BEFORE the monitor close is
        submitted, not after. Cancelling first shrinks the double-SELL window
        (both the floor fill AND the monitor close executing) to near-zero: the
        floor is retracted, then the close goes to the broker."""

        async def _resolver(order_id: str) -> str | None:
            return "floor-alpaca-uuid" if order_id == "ORD-FLOOR-xyz" else None

        position = _options_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _prepare_and_submit(
            position=position,
            bracket=_bracket(floor_order_id="ORD-FLOOR-xyz"),
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-20260511T143000Z-aabbccdd",
            trigger_id=1,
            now=_NOW,
            estimated_exit_price=10.0,
            realized_pnl_usd=-200.0,
            floor_alpaca_id_resolver=_resolver,
        )
        # Cancel precedes the close submit.
        assert submitter.call_sequence == ["cancel_floor", "submit_options_close"]

    async def test_no_floor_leg_cancels_nothing(self) -> None:
        """A bracket with no BROKER_ENFORCED floor leg cancels no floor."""
        position = _options_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _prepare_and_submit(
            position=position,
            bracket=_bracket(),
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-20260511T143000Z-aabbccdd",
            trigger_id=1,
            now=_NOW,
            estimated_exit_price=10.0,
            realized_pnl_usd=-200.0,
            floor_alpaca_id_resolver=lambda _oid: _none(),
        )
        assert submitter.cancelled_floors == []

    async def test_unbackfilled_floor_id_skips_cancel_and_escalates(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """FL6 — a floor leg whose OrderRow exists (its ``order_id`` is the FK
        target) but whose broker-id backfill hasn't landed (resolver returns None)
        leaves an un-cancellable resting floor that will later double-fire. The
        closer still skips the bad-id cancel, but escalates this beyond a bare
        warning — an operator-alert-grade ERROR naming the position — so the
        lingering floor is surfaced rather than silently dropped. The close is
        never blocked."""

        async def _resolver(order_id: str) -> str | None:
            del order_id
            return None

        position = _options_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        with caplog.at_level(
            logging.WARNING,
            logger="alphamind.execution.continuous_monitor.bracket_stops.closer",
        ):
            await _prepare_and_submit(
                position=position,
                bracket=_bracket(floor_order_id="ORD-FLOOR-xyz"),
                trigger_reason=PositionExitMethod.STOP_TRIGGERED,
                submitter=submitter,
                activity_log=log,
                invocation_id_provider=_const_str("inv-001"),
                monitor_session_id="mon-20260511T143000Z-aabbccdd",
                trigger_id=1,
                now=_NOW,
                estimated_exit_price=10.0,
                realized_pnl_usd=-200.0,
                floor_alpaca_id_resolver=_resolver,
            )
        # No bad id passed to the broker, and the close still landed.
        assert submitter.cancelled_floors == []
        assert len(submitter.options_calls) == 1
        # The un-cancellable resting floor is surfaced at ERROR (operator alert),
        # naming the position so the lingering floor can be reconciled.
        floor_logs = [
            r
            for r in caplog.records
            if "ORD-FLOOR-xyz" in r.getMessage() or "pos-1" in r.getMessage()
        ]
        assert floor_logs, "expected the un-backfilled floor to be surfaced in a log"
        assert all(r.levelno == logging.ERROR for r in floor_logs), (
            "an un-cancellable resting floor must escalate beyond WARNING"
        )

    async def test_only_the_capital_floor_leg_is_cancelled_not_native_children(
        self,
    ) -> None:
        """FL11 — cancel-on-monitor-fire targets ONLY the capital-floor leg
        (``{bracket_id}-leg-floor``), never every BROKER_ENFORCED leg. An equity
        native bracket's take-profit / stop children are also BROKER_ENFORCED but
        the broker manages their OCO cancellation natively — cancelling them here
        would be a double-cancel. The closer resolves and cancels only the floor."""
        resolved: list[str] = []

        async def _resolver(order_id: str) -> str | None:
            resolved.append(order_id)
            return "floor-alpaca-uuid" if order_id == "ORD-FLOOR-xyz" else "native-alpaca-uuid"

        # A bracket carrying BOTH a BROKER_ENFORCED native-bracket take-profit
        # child (``brk-1-leg-target``) AND the capital floor (``brk-1-leg-floor``).
        native_child = BracketLeg(
            leg_id="brk-1-leg-target",
            leg_type=BracketLegType.TAKE_PROFIT,
            order_id=OrderId("ORD-NATIVE-TP"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("NVDA"), threshold_usd=900.0, direction="GTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            enforcement_binding=EnforcementBinding.BROKER_ENFORCED,
            status=BracketLegStatus.ACTIVE,
        )
        base = _bracket(floor_order_id="ORD-FLOOR-xyz")
        bracket = dataclasses.replace(base, protective_legs=(*base.protective_legs, native_child))

        position = _options_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _prepare_and_submit(
            position=position,
            bracket=bracket,
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-20260511T143000Z-aabbccdd",
            trigger_id=1,
            now=_NOW,
            estimated_exit_price=10.0,
            realized_pnl_usd=-200.0,
            floor_alpaca_id_resolver=_resolver,
        )
        # Only the floor leg's order_id is resolved + cancelled; the native child
        # is left to the broker's native OCO (never resolved, never cancelled).
        assert resolved == ["ORD-FLOOR-xyz"]
        assert submitter.cancelled_floors == ["floor-alpaca-uuid"]


async def _none() -> str | None:
    return None


# ---------------------------------------------------------------------------
# Strategy close
# ---------------------------------------------------------------------------


class TestStrategyClose:
    async def test_strategy_routes_through_strategy_close(self) -> None:
        position = _strategy_position()
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        await _prepare_and_submit(
            position=position,
            bracket=_bracket(position_id=PositionId("pos-st")),
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-20260511T143000Z-aabbccdd",
            trigger_id=3,
            now=_NOW,
            estimated_exit_price=2.0,
            realized_pnl_usd=-100.0,
        )
        assert submitter.options_calls == []
        assert len(submitter.strategy_calls) == 1
        assert submitter.strategy_calls[0][0] == "pos-st"

    async def test_strategy_per_leg_fallback_mode_captured(self) -> None:
        """If the submitter's strategy method returns mode='strategy_per_leg' (the
        fallback after Alpaca rejects the combined close), the closer still
        emits one POSITION_CLOSED entry and uses the first leg's order_id."""
        position = _strategy_position()
        submitter = FakeSubmitter(
            strategy_returns=CloseSubmissionResult(
                order_ids=("MON.mon-S.5.0", "MON.mon-S.5.1"),
                mode="strategy_per_leg",
            )
        )
        log = FakeActivityLog()
        await _prepare_and_submit(
            position=position,
            bracket=_bracket(position_id=PositionId("pos-st")),
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=log,
            invocation_id_provider=_const_str("inv-001"),
            monitor_session_id="mon-S",
            trigger_id=5,
            now=_NOW,
            estimated_exit_price=2.0,
            realized_pnl_usd=-100.0,
        )
        assert len(log.entries) == 1
        # First leg's order id flows through.
        assert log.entries[0].order_id == "MON.mon-S.5.0"


# ---------------------------------------------------------------------------
# Equity positions are not the closer's responsibility
# ---------------------------------------------------------------------------


class TestUnsupportedInstrument:
    async def test_equity_position_raises(self) -> None:
        from alphamind.portfolio_state.records.positions import EquityPositionDetails

        equity = PositionRecord(
            position_id=PositionId("pos-eq"),
            thesis_id=ThesisId("THE-AAPL-0123456789abcdef0123456789abcdef"),
            bracket_id=BracketId("brk-eq"),
            status=PositionStatus.OPEN,
            direction=Direction.LONG,
            entry_timestamp=_NOW,
            details=EquityPositionDetails(
                ticker=Symbol("AAPL"),
                share_count=100.0,
                average_cost_basis_per_share=150.0,
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
        submitter = FakeSubmitter()
        log = FakeActivityLog()
        with pytest.raises(TypeError):
            await _prepare_and_submit(
                position=equity,
                bracket=_bracket(position_id=PositionId("pos-eq")),
                trigger_reason=PositionExitMethod.STOP_TRIGGERED,
                submitter=submitter,
                activity_log=log,
                invocation_id_provider=_const_str("inv-001"),
                monitor_session_id="mon-S",
                trigger_id=1,
                now=_NOW,
                estimated_exit_price=148.0,
                realized_pnl_usd=-200.0,
            )
