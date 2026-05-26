"""Verb implementations for the monitor /control surface (ALP-665).

Three verbs, each a pure-ish async function over injected dependencies:

* :func:`cancel_order` — synthesizes a CANCEL through the OMS broker-dispatch
  path. Engine-originated envelopes are CLOSE-only per
  ``docs/design/05-execution-layer/engine-envelope-schema.md``; cancel routes
  via :class:`CancelEmitterProtocol` (the production seam wraps the broker
  adapter's :func:`submit_cancel` + an activity-log entry).
* :func:`force_close_position` — synthesizes a CLOSE engine envelope per
  ``engine-envelope-schema.md`` and submits via :class:`CloseSubmitterProtocol`
  (the production seam wraps :func:`submit_engine_envelope`). The envelope's
  ``guardrail_trigger_record.position_selection_rationale`` carries operator-
  console attribution prefixed with ``operator_console:``.
* :func:`set_halt_mode` — flips the operator-set halt-mode flag and persists
  to ``monitor_halt_mode``. Idempotent on same-value re-set per the schema.

Each verb returns either :class:`VerbResult` on success or :class:`VerbError`
on a documented error envelope. The route handler in ``routes.py`` translates
the typed outcomes into HTTP responses.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from alphamind.execution.continuous_monitor.control.halt_mode_repo import (
    HaltModeRecord,
    HaltModeRepository,
)


# ---------------------------------------------------------------------------
# Result + error types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerbResult:
    """Successful outcome for any ``POST /control/*`` verb.

    ``envelope_id`` is populated only by ``force_close_position`` — every
    other verb returns ``None``.
    """

    applied_at: datetime
    envelope_id: str | None = None


@dataclass(frozen=True, slots=True)
class VerbError:
    """Documented error-envelope outcome.

    The route handler maps ``code`` to HTTP status per the schema's per-verb
    table; ``details`` is the structured shape for ``current_status`` /
    ``broker_message`` etc.
    """

    code: str
    detail: str
    details: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Verb-specific state seams (loaded from the existing DB shapes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderState:
    """Minimal projection of ``orders`` the cancel verb consults."""

    order_id: str
    status: str  # lowercase wire-format — open / partially_filled / filled / cancelled / expired / rejected


@dataclass(frozen=True, slots=True)
class PositionState:
    """Minimal projection of ``positions`` the force-close verb consults."""

    position_id: str
    status: str  # lowercase wire-format — pending / open / closed


# ---------------------------------------------------------------------------
# Broker-side outcomes — what the cancel / close submitters return.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CancelOrderOutcome:
    """Broker accepted the cancel request."""


@dataclass(frozen=True, slots=True)
class BrokerErrorCancel:
    """The broker rejected the cancel; surfaces the broker's message."""

    broker_message: str


@dataclass(frozen=True, slots=True)
class ForceCloseOutcome:
    """The OMS accepted the synthesized CLOSE envelope; carries its envelope_id."""

    envelope_id: str


@dataclass(frozen=True, slots=True)
class BrokerErrorClose:
    """The broker rejected the close; surfaces the broker's message."""

    broker_message: str


# ---------------------------------------------------------------------------
# Injected collaborator protocols
# ---------------------------------------------------------------------------


class OrderLookup(Protocol):
    """Read-only projection of the ``orders`` table the cancel verb reads."""

    async def fetch(self, order_id: str) -> OrderState | None: ...


class PositionLookup(Protocol):
    """Read-only projection of the ``positions`` table the force-close verb reads."""

    async def fetch(self, position_id: str) -> PositionState | None: ...


class CancelEmitterProtocol(Protocol):
    """Submits a synthesized cancel for *order_id* via the broker-dispatch path."""

    async def submit_cancel(
        self, *, order_id: str
    ) -> CancelOrderOutcome | BrokerErrorCancel: ...


class CloseSubmitterProtocol(Protocol):
    """Synthesizes the engine CLOSE envelope and submits via the OMS write path."""

    async def submit_close(
        self,
        *,
        position_id: str,
        position_selection_rationale: str,
        rule_breached: str,
        breach_details_current: float,
        breach_details_limit: float,
    ) -> ForceCloseOutcome | BrokerErrorClose: ...


class HaltModeRepoProtocol(Protocol):
    """Subset of :class:`HaltModeRepository` the verb consumes."""

    async def read(self) -> HaltModeRecord: ...

    async def write(self, record: HaltModeRecord) -> None: ...


# ---------------------------------------------------------------------------
# halt-mode outcome convenience (used by the route handler when rendering)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HaltModeOutcome:
    """Bound (enabled, applied_at) the route renders into a response envelope."""

    enabled: bool
    applied_at: datetime


# ---------------------------------------------------------------------------
# Status-set constants per the schema's per-verb error table
# ---------------------------------------------------------------------------


_CANCELLABLE_ORDER_STATUSES = frozenset({"open", "partially_filled"})
_CLOSEABLE_POSITION_STATUSES = frozenset({"open"})


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


_OPERATOR_CONSOLE_PREFIX = "operator_console: "
# The synthetic rule_breached for operator-driven closes. The schema's
# ``breach_detected.rule`` field is a free string keyed to rules-and-limits.md,
# but operator-console closes don't correspond to a guardrail breach — they're
# a deliberate manual close. We surface this as ``operator_console_force_close``
# so downstream consumers (activity log, command center audit) can distinguish
# it from rule-driven closes.
_OPERATOR_CONSOLE_RULE = "operator_console_force_close"


async def cancel_order(
    *,
    order_id: str,
    order_lookup: OrderLookup,
    cancel_emitter: CancelEmitterProtocol,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> VerbResult | VerbError:
    """Cancel a pending order through the broker-dispatch path.

    Returns :class:`VerbResult` on broker-accepted cancel,
    :class:`VerbError` for any documented error envelope per the schema:

    * ``not_found`` — ``order_id`` does not match a row in the orders table.
    * ``precondition_failed`` — the order is already filled, cancelled, or
      expired (status not in ``{open, partially_filled}``).
    * ``broker_error`` — the broker rejected the cancel.
    """
    state = await order_lookup.fetch(order_id)
    if state is None:
        return VerbError(
            code="not_found",
            detail=f"order_id {order_id!r} does not match an order",
        )
    if state.status not in _CANCELLABLE_ORDER_STATUSES:
        return VerbError(
            code="precondition_failed",
            detail=f"order is not cancellable in status {state.status!r}",
            details={"current_status": state.status},
        )
    outcome = await cancel_emitter.submit_cancel(order_id=order_id)
    if isinstance(outcome, BrokerErrorCancel):
        return VerbError(
            code="broker_error",
            detail="broker rejected the cancel request",
            details={"broker_message": outcome.broker_message},
        )
    return VerbResult(applied_at=now())


async def force_close_position(
    *,
    position_id: str,
    rationale: str,
    position_lookup: PositionLookup,
    close_submitter: CloseSubmitterProtocol,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> VerbResult | VerbError:
    """Submit a synthesized engine-originated CLOSE for *position_id*.

    Returns :class:`VerbResult` with the synthesized ``envelope_id`` on
    success, or :class:`VerbError` for the documented error envelopes per the
    schema:

    * ``not_found`` — ``position_id`` does not match a row in the positions
      table.
    * ``precondition_failed`` — the position is already closed (status not in
      ``{open}``).
    * ``broker_error`` — the broker rejected the close.

    The rationale is prefixed with operator-console attribution before being
    persisted to the envelope's
    ``guardrail_trigger_record.position_selection_rationale`` field, per the
    schema's ``force_close_position`` description.
    """
    state = await position_lookup.fetch(position_id)
    if state is None:
        return VerbError(
            code="not_found",
            detail=f"position_id {position_id!r} does not match a position",
        )
    if state.status not in _CLOSEABLE_POSITION_STATUSES:
        return VerbError(
            code="precondition_failed",
            detail=f"position is not closeable in status {state.status!r}",
            details={"current_status": state.status},
        )
    annotated = _OPERATOR_CONSOLE_PREFIX + rationale
    # Operator-console closes are not driven by a guardrail breach; the
    # synthetic breach-details numerically mirror "no overage" so the
    # downstream activity-log row is well-formed without inventing fake
    # rule-breach values.
    outcome = await close_submitter.submit_close(
        position_id=position_id,
        position_selection_rationale=annotated,
        rule_breached=_OPERATOR_CONSOLE_RULE,
        breach_details_current=0.0,
        breach_details_limit=0.0,
    )
    if isinstance(outcome, BrokerErrorClose):
        return VerbError(
            code="broker_error",
            detail="broker rejected the close",
            details={"broker_message": outcome.broker_message},
        )
    return VerbResult(applied_at=now(), envelope_id=outcome.envelope_id)


async def set_halt_mode(
    *,
    enabled: bool,
    reason: str,
    repo: HaltModeRepoProtocol,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> VerbResult | VerbError:
    """Flip the operator-set halt-mode flag.

    Per the schema:

    * Same-value re-set is idempotent and returns ``accepted`` with the
      original ``applied_at`` (no write). When no prior write exists (the
      disengaged default with ``applied_at=None``), the response carries the
      current ``now()`` so the envelope always renders a valid timestamp.
    * A real transition writes ``(enabled, reason, applied_at=now())`` to the
      singleton row and returns ``accepted`` with ``now()``.
    """
    current = await repo.read()
    if current.enabled == enabled:
        # Idempotent no-op: do not re-write the row.
        return VerbResult(applied_at=current.applied_at or now())
    applied_at = now()
    record = HaltModeRecord(enabled=enabled, reason=reason, applied_at=applied_at)
    await repo.write(record)
    return VerbResult(applied_at=applied_at)


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------


__all__ = [
    "BrokerErrorCancel",
    "BrokerErrorClose",
    "CancelEmitterProtocol",
    "CancelOrderOutcome",
    "CloseSubmitterProtocol",
    "ForceCloseOutcome",
    "HaltModeOutcome",
    "HaltModeRepoProtocol",
    "OrderLookup",
    "OrderState",
    "PositionLookup",
    "PositionState",
    "VerbError",
    "VerbResult",
    "cancel_order",
    "force_close_position",
    "set_halt_mode",
]
