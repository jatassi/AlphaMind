"""Verb implementation tests for the monitor /control surface (ALP-665).

Each verb is a pure-ish function over injected dependencies — tests substitute
in-memory fakes for the protocol seams (order/position lookups, halt-mode
repo, broker-submission callables, OMS envelope submitter) and assert on the
returned ``VerbResult`` value and the side-effects visible through those
fakes.

The verbs encapsulate the schema's error envelope mapping — ``not_found``
(404), ``precondition_failed`` (409), ``broker_error`` (502) — by returning
a typed ``VerbError`` value. The route handlers translate that into HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.execution.continuous_monitor.control.halt_mode_repo import (
    HaltModeRecord,
)
from alphamind.execution.continuous_monitor.control.verbs import (
    BrokerErrorCancel,
    BrokerErrorClose,
    CancelOrderOutcome,
    ForceCloseOutcome,
    HaltModeOutcome,
    OrderState,
    PositionState,
    VerbError,
    VerbResult,
    cancel_order,
    force_close_position,
    set_halt_mode,
)

# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeOrderLookup:
    rows: dict[str, OrderState]

    async def fetch(self, order_id: str) -> OrderState | None:
        return self.rows.get(order_id)


@dataclass
class FakePositionLookup:
    rows: dict[str, PositionState]

    async def fetch(self, position_id: str) -> PositionState | None:
        return self.rows.get(position_id)


@dataclass
class FakeHaltModeRepo:
    record: HaltModeRecord = field(
        default_factory=lambda: HaltModeRecord(enabled=False, reason=None, applied_at=None)
    )
    writes: list[HaltModeRecord] = field(default_factory=list)

    async def read(self) -> HaltModeRecord:
        return self.record

    async def write(self, record: HaltModeRecord) -> None:
        self.writes.append(record)
        self.record = record


@dataclass
class FakeCancelEmitter:
    """Stand-in for the broker's cancel path + activity-log write."""

    outcomes_by_order: dict[str, CancelOrderOutcome | BrokerErrorCancel]
    submitted: list[str] = field(default_factory=list)

    async def submit_cancel(self, *, order_id: str) -> CancelOrderOutcome | BrokerErrorCancel:
        self.submitted.append(order_id)
        return self.outcomes_by_order[order_id]


@dataclass
class FakeCloseSubmitter:
    """Stand-in for the engine-envelope submission path."""

    envelope_id_to_return: str = "MON.session-1.42"
    captured_kwargs: list[dict[str, Any]] = field(default_factory=list)
    broker_error: BrokerErrorClose | None = None

    async def submit_close(
        self,
        *,
        position_id: str,
        position_selection_rationale: str,
        rule_breached: str,
        breach_details_current: float,
        breach_details_limit: float,
    ) -> ForceCloseOutcome | BrokerErrorClose:
        self.captured_kwargs.append(
            {
                "position_id": position_id,
                "position_selection_rationale": position_selection_rationale,
                "rule_breached": rule_breached,
                "breach_details_current": breach_details_current,
                "breach_details_limit": breach_details_limit,
            }
        )
        if self.broker_error is not None:
            return self.broker_error
        return ForceCloseOutcome(envelope_id=self.envelope_id_to_return)


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


def _now() -> datetime:
    return _NOW


# ===========================================================================
# cancel_order
# ===========================================================================


@pytest.mark.asyncio
class TestCancelOrderVerb:
    async def test_happy_path_returns_accepted(self) -> None:
        order_lookup = FakeOrderLookup(
            rows={
                "ord-1": OrderState(order_id="ord-1", status="open"),
            }
        )
        cancel_emitter = FakeCancelEmitter(outcomes_by_order={"ord-1": CancelOrderOutcome()})
        result = await cancel_order(
            order_id="ord-1",
            order_lookup=order_lookup,
            cancel_emitter=cancel_emitter,
            now=_now,
        )
        assert isinstance(result, VerbResult)
        assert result.applied_at == _NOW
        assert cancel_emitter.submitted == ["ord-1"]

    async def test_partially_filled_order_is_cancellable(self) -> None:
        order_lookup = FakeOrderLookup(
            rows={"ord-1": OrderState(order_id="ord-1", status="partially_filled")}
        )
        cancel_emitter = FakeCancelEmitter(outcomes_by_order={"ord-1": CancelOrderOutcome()})
        result = await cancel_order(
            order_id="ord-1",
            order_lookup=order_lookup,
            cancel_emitter=cancel_emitter,
            now=_now,
        )
        assert isinstance(result, VerbResult)

    async def test_unknown_order_id_returns_not_found(self) -> None:
        order_lookup = FakeOrderLookup(rows={})
        cancel_emitter = FakeCancelEmitter(outcomes_by_order={})
        result = await cancel_order(
            order_id="ord-missing",
            order_lookup=order_lookup,
            cancel_emitter=cancel_emitter,
            now=_now,
        )
        assert isinstance(result, VerbError)
        assert result.code == "not_found"
        assert cancel_emitter.submitted == []

    async def test_filled_order_returns_precondition_failed(self) -> None:
        order_lookup = FakeOrderLookup(
            rows={"ord-1": OrderState(order_id="ord-1", status="filled")}
        )
        cancel_emitter = FakeCancelEmitter(outcomes_by_order={})
        result = await cancel_order(
            order_id="ord-1",
            order_lookup=order_lookup,
            cancel_emitter=cancel_emitter,
            now=_now,
        )
        assert isinstance(result, VerbError)
        assert result.code == "precondition_failed"
        assert result.details == {"current_status": "filled"}
        # No broker call on precondition failure.
        assert cancel_emitter.submitted == []

    async def test_cancelled_order_returns_precondition_failed(self) -> None:
        order_lookup = FakeOrderLookup(
            rows={"ord-1": OrderState(order_id="ord-1", status="cancelled")}
        )
        cancel_emitter = FakeCancelEmitter(outcomes_by_order={})
        result = await cancel_order(
            order_id="ord-1",
            order_lookup=order_lookup,
            cancel_emitter=cancel_emitter,
            now=_now,
        )
        assert isinstance(result, VerbError)
        assert result.code == "precondition_failed"
        assert result.details == {"current_status": "cancelled"}

    async def test_broker_error_surfaces_with_message(self) -> None:
        order_lookup = FakeOrderLookup(rows={"ord-1": OrderState(order_id="ord-1", status="open")})
        cancel_emitter = FakeCancelEmitter(
            outcomes_by_order={"ord-1": BrokerErrorCancel(broker_message="rate limited")}
        )
        result = await cancel_order(
            order_id="ord-1",
            order_lookup=order_lookup,
            cancel_emitter=cancel_emitter,
            now=_now,
        )
        assert isinstance(result, VerbError)
        assert result.code == "broker_error"
        assert result.details == {"broker_message": "rate limited"}


# ===========================================================================
# force_close_position
# ===========================================================================


@pytest.mark.asyncio
class TestForceClosePositionVerb:
    async def test_happy_path_returns_envelope_id(self) -> None:
        position_lookup = FakePositionLookup(
            rows={"pos-1": PositionState(position_id="pos-1", status="open")}
        )
        close_submitter = FakeCloseSubmitter(envelope_id_to_return="MON.session-1.42")
        result = await force_close_position(
            position_id="pos-1",
            rationale="vega too high",
            position_lookup=position_lookup,
            close_submitter=close_submitter,
            now=_now,
        )
        assert isinstance(result, VerbResult)
        assert result.envelope_id == "MON.session-1.42"
        assert result.applied_at == _NOW

    async def test_rationale_prefixed_with_operator_console_attribution(self) -> None:
        position_lookup = FakePositionLookup(
            rows={"pos-1": PositionState(position_id="pos-1", status="open")}
        )
        close_submitter = FakeCloseSubmitter()
        await force_close_position(
            position_id="pos-1",
            rationale="vega too high",
            position_lookup=position_lookup,
            close_submitter=close_submitter,
            now=_now,
        )
        kwargs = close_submitter.captured_kwargs[0]
        # The rationale is persisted onto
        # ``guardrail_trigger_record.position_selection_rationale`` prefixed
        # with operator-console attribution per the schema.
        rendered = kwargs["position_selection_rationale"]
        assert "operator_console" in rendered
        assert "vega too high" in rendered

    async def test_unknown_position_id_returns_not_found(self) -> None:
        position_lookup = FakePositionLookup(rows={})
        close_submitter = FakeCloseSubmitter()
        result = await force_close_position(
            position_id="pos-missing",
            rationale="reason",
            position_lookup=position_lookup,
            close_submitter=close_submitter,
            now=_now,
        )
        assert isinstance(result, VerbError)
        assert result.code == "not_found"
        assert close_submitter.captured_kwargs == []

    async def test_closed_position_returns_precondition_failed(self) -> None:
        position_lookup = FakePositionLookup(
            rows={"pos-1": PositionState(position_id="pos-1", status="closed")}
        )
        close_submitter = FakeCloseSubmitter()
        result = await force_close_position(
            position_id="pos-1",
            rationale="reason",
            position_lookup=position_lookup,
            close_submitter=close_submitter,
            now=_now,
        )
        assert isinstance(result, VerbError)
        assert result.code == "precondition_failed"
        assert result.details == {"current_status": "closed"}
        assert close_submitter.captured_kwargs == []

    async def test_broker_error_surfaces_with_message(self) -> None:
        position_lookup = FakePositionLookup(
            rows={"pos-1": PositionState(position_id="pos-1", status="open")}
        )
        close_submitter = FakeCloseSubmitter(
            broker_error=BrokerErrorClose(broker_message="insufficient liquidity")
        )
        result = await force_close_position(
            position_id="pos-1",
            rationale="reason",
            position_lookup=position_lookup,
            close_submitter=close_submitter,
            now=_now,
        )
        assert isinstance(result, VerbError)
        assert result.code == "broker_error"
        assert result.details == {"broker_message": "insufficient liquidity"}


# ===========================================================================
# set_halt_mode
# ===========================================================================


@pytest.mark.asyncio
class TestSetHaltModeVerb:
    async def test_engaging_writes_new_state(self) -> None:
        repo = FakeHaltModeRepo()
        result = await set_halt_mode(enabled=True, reason="circuit breaker", repo=repo, now=_now)
        assert isinstance(result, VerbResult)
        assert result.applied_at == _NOW
        assert repo.record.enabled is True
        assert repo.record.reason == "circuit breaker"
        assert repo.record.applied_at == _NOW

    async def test_idempotent_same_value_returns_original_applied_at(self) -> None:
        prior_ts = datetime(2026, 5, 26, 8, 0, 0, tzinfo=UTC)
        repo = FakeHaltModeRepo(
            record=HaltModeRecord(enabled=True, reason="first", applied_at=prior_ts)
        )
        result = await set_halt_mode(enabled=True, reason="second", repo=repo, now=_now)
        assert isinstance(result, VerbResult)
        # Idempotent: original applied_at returned unchanged.
        assert result.applied_at == prior_ts
        # And the repo is NOT re-written (same-value no-op).
        assert repo.writes == []

    async def test_disengaging_writes_new_state(self) -> None:
        prior_ts = datetime(2026, 5, 26, 8, 0, 0, tzinfo=UTC)
        repo = FakeHaltModeRepo(
            record=HaltModeRecord(enabled=True, reason="prior", applied_at=prior_ts)
        )
        result = await set_halt_mode(enabled=False, reason="lifted", repo=repo, now=_now)
        assert isinstance(result, VerbResult)
        assert result.applied_at == _NOW
        assert repo.record.enabled is False
        assert repo.record.reason == "lifted"
        assert repo.record.applied_at == _NOW

    async def test_idempotent_disabled_to_disabled_returns_disengaged_default(
        self,
    ) -> None:
        """Same-value re-set against the disengaged default returns ``_now``.

        The disengaged default has ``applied_at=None`` — the repo never wrote
        it. Treating ``None`` as "the system has always been disengaged" means
        the same-value response returns the current ``now`` so the response
        envelope carries a valid timestamp even when no prior write exists.
        """
        repo = FakeHaltModeRepo()  # disengaged default
        result = await set_halt_mode(enabled=False, reason="confirm", repo=repo, now=_now)
        assert isinstance(result, VerbResult)
        # Same-value re-set: still treated as idempotent (no write).
        assert repo.writes == []
        # Response carries _now because there is no prior applied_at to echo.
        assert result.applied_at == _NOW


# ===========================================================================
# HaltMode outcome convenience
# ===========================================================================


class TestHaltModeOutcome:
    def test_outcome_carries_state_and_applied_at(self) -> None:
        outcome = HaltModeOutcome(enabled=True, applied_at=_NOW)
        assert outcome.enabled is True
        assert outcome.applied_at == _NOW
