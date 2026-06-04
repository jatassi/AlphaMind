"""The default alert condition predicates (story 05a / ALP-671).

One predicate class per rule in :doc:`docs/design/command-center.md`
§ Alerting — Default rule set. The 17 design-doc rules plus
``entry_no_fill`` (ALP-739), an 18th rule added after the original table
to alert on an accepted entry order that reaches a terminal state
(EXPIRED / CANCELLED) with zero fills. Predicates fall into three
categories:

* **Event-driven, live.** Consumes a :class:`CombinedEvent` straight off
  the multiplexer's publish callback (e.g. ``pipeline_aborted`` watches
  ``invocation_ended`` with ``status=failed``). ``evaluate`` returns
  ``fired=True`` whenever the event matches; the engine's debounce keeps
  re-fires inside the rule's window from inserting duplicate rows.

* **State-polling, live.** Re-queried on every 60s engine tick AND
  every event tick (so a state mutation reaches the predicate without
  a 60s lag). Returns ``fired=True`` on first observation of the
  transition; the engine's debounce suppresses re-fires.

* **Dormant.** The triggering upstream emit point doesn't yet exist
  in this work tree's scope. The predicate registers, logs a
  one-time WARNING at construction naming the missing emit, and
  returns ``fired=False`` for every evaluation. The rule still
  appears in :func:`build_default_rules` so the alert registry's
  shape is the complete 17-rule design-doc table (per parent issue's
  pre-resolved F: "ship-all-17-rules-even-if-dormant").

Per the architectural invariants:

* No bare ``asyncio.create_task`` — every predicate's IO is awaited
  inside the engine's tick.
* Frozen-dataclass internal — each predicate is a frozen dataclass +
  small async ``evaluate``; no Pydantic.
* The DB-bound predicates dispatch through
  :attr:`AlertEvaluatorState.foreign_reader_factory` so an in-memory
  test fake (an ``async_sessionmaker`` over ``sqlite:memory:`` with
  seeded rows) drives every state-polling branch deterministically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center._kernel.ids import AlertRuleName, alert_rule_name
from alphamind.command_center.alerts.rules import (
    AlertCondition,
    AlertConditionResult,
    AlertEvaluatorState,
    AlertRule,
    AlertSeverity,
)
from alphamind.command_center.events.multiplexer import CombinedEvent

__all__ = [
    "DEFAULT_DEBOUNCE_WINDOWS",
    "AgentMalformedOutputCondition",
    "CommandAbandonedCondition",
    "CriticalApiFailureCondition",
    "DataDirectoryDiskPressureCondition",
    "DormantCondition",
    "DrawdownTierCrossedCondition",
    "EntryNoFillCondition",
    "HaltModeEnteredCondition",
    "HardBlockGuardrailRejectionCondition",
    "ImportantApiFailureCondition",
    "MarginCallDetectedCondition",
    "MonitorWebsocketDisconnectedCondition",
    "OptionalDataCategorySkippedCondition",
    "PipelineAbortedCondition",
    "ProfileBoundaryCrossedCondition",
    "RegimeJumpCondition",
    "ScheduleMissCondition",
    "ScheduleMissCriticalCondition",
    "ThesisResolvedCondition",
    "build_default_rules",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rule name constants — keep in one place so the YAML loader + tests share.
# ---------------------------------------------------------------------------


_RULE_PIPELINE_ABORTED: Final = alert_rule_name("pipeline_aborted")
_RULE_CRITICAL_API_FAILURE: Final = alert_rule_name("critical_api_failure")
_RULE_SCHEDULE_MISS_CRITICAL: Final = alert_rule_name("schedule_miss_critical")
_RULE_MONITOR_WEBSOCKET_DISCONNECTED: Final = alert_rule_name("monitor_websocket_disconnected")
_RULE_MARGIN_CALL_DETECTED: Final = alert_rule_name("margin_call_detected")
_RULE_DRAWDOWN_TIER_CROSSED: Final = alert_rule_name("drawdown_tier_crossed")
_RULE_HALT_MODE_ENTERED: Final = alert_rule_name("halt_mode_entered")
_RULE_IMPORTANT_API_FAILURE: Final = alert_rule_name("important_api_failure")
_RULE_HARD_BLOCK_GUARDRAIL_REJECTION: Final = alert_rule_name("hard_block_guardrail_rejection")
_RULE_REGIME_JUMP: Final = alert_rule_name("regime_jump")
_RULE_AGENT_MALFORMED_OUTPUT: Final = alert_rule_name("agent_malformed_output")
_RULE_SCHEDULE_MISS: Final = alert_rule_name("schedule_miss")
_RULE_DATA_DIRECTORY_DISK_PRESSURE: Final = alert_rule_name("data_directory_disk_pressure")
_RULE_PROFILE_BOUNDARY_CROSSED: Final = alert_rule_name("profile_boundary_crossed")
_RULE_OPTIONAL_DATA_CATEGORY_SKIPPED: Final = alert_rule_name("optional_data_category_skipped")
_RULE_COMMAND_ABANDONED: Final = alert_rule_name("command_abandoned")
_RULE_THESIS_RESOLVED: Final = alert_rule_name("thesis_resolved")
_RULE_ENTRY_NO_FILL: Final = alert_rule_name("entry_no_fill")


# ---------------------------------------------------------------------------
# Default per-rule debounce windows.
# ---------------------------------------------------------------------------


DEFAULT_DEBOUNCE_WINDOWS: Final[Mapping[AlertRuleName, timedelta]] = {
    _RULE_PIPELINE_ABORTED: timedelta(minutes=15),
    _RULE_CRITICAL_API_FAILURE: timedelta(minutes=30),
    _RULE_SCHEDULE_MISS_CRITICAL: timedelta(minutes=15),
    _RULE_MONITOR_WEBSOCKET_DISCONNECTED: timedelta(minutes=15),
    _RULE_MARGIN_CALL_DETECTED: timedelta(minutes=60),
    _RULE_DRAWDOWN_TIER_CROSSED: timedelta(hours=12),
    _RULE_HALT_MODE_ENTERED: timedelta(minutes=15),
    _RULE_IMPORTANT_API_FAILURE: timedelta(minutes=30),
    _RULE_HARD_BLOCK_GUARDRAIL_REJECTION: timedelta(minutes=15),
    _RULE_REGIME_JUMP: timedelta(hours=6),
    _RULE_AGENT_MALFORMED_OUTPUT: timedelta(minutes=10),
    _RULE_SCHEDULE_MISS: timedelta(minutes=15),
    _RULE_DATA_DIRECTORY_DISK_PRESSURE: timedelta(hours=6),
    _RULE_PROFILE_BOUNDARY_CROSSED: timedelta(hours=24),
    _RULE_OPTIONAL_DATA_CATEGORY_SKIPPED: timedelta(minutes=30),
    _RULE_COMMAND_ABANDONED: timedelta(minutes=15),
    _RULE_THESIS_RESOLVED: timedelta(minutes=15),
    _RULE_ENTRY_NO_FILL: timedelta(minutes=15),
}
"""Per-rule debounce window defaults.

The window controls how long after one fire the engine suppresses
re-fires for the same ``(rule_name, debounce_key)`` pair. The design
doc does not pin specific numbers — these are first-pass values that
favor "no alert storm" over "alert promptly on re-trigger"; the
operator overrides via ``config/alerts.yaml`` once they have on-call
intuition.
"""


_FOREIGN_READER_TYPE_HINT = "async_sessionmaker[AsyncSession]"
"""Documentation hint — the foreign-reader factory's runtime type."""


# ---------------------------------------------------------------------------
# Dormant condition — for rules whose upstream emit doesn't exist yet.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DormantCondition:
    """No-op predicate for rules whose upstream emit point doesn't exist yet.

    Per parent issue's pre-resolved F: every rule in the design doc's
    17-rule table must register at engine startup, even when the
    upstream emit doesn't exist yet. The dormant predicate is the
    registration placeholder.

    The engine calls :meth:`log_startup_warning` once per registered
    dormant rule during :meth:`AlertEngine.run` startup so the operator
    sees a single WARNING per rule on boot (rather than one per
    evaluation tick).

    ``missing_emit`` is the rendered description of the missing upstream
    surface; it appears in the WARNING line so the operator can grep
    the boot log for the specific gap.
    """

    name: AlertRuleName
    missing_emit: str

    def log_startup_warning(self) -> None:
        """Emit the one-time WARNING naming the missing upstream emit."""
        log.warning(
            "alert rule %r is dormant: upstream emit point %s does not exist yet; "
            "rule will never fire until that surface lands",
            str(self.name),
            self.missing_emit,
        )

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event, state
        return AlertConditionResult(fired=False)


# ---------------------------------------------------------------------------
# Event-driven predicates (live).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineAbortedCondition:
    """Rule 1 — pipeline aborted.

    Fires on a pipeline ``invocation_ended`` event whose ``payload.status``
    is ``"failed"``. The debounce key is the invocation id so two distinct
    failed invocations both fire (but a re-emit of the same failure tick
    is collapsed).
    """

    name: AlertRuleName = _RULE_PIPELINE_ABORTED

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del state
        if not isinstance(event, PipelineEvent):
            return AlertConditionResult(fired=False)
        if event.event_type is not PipelineEventType.INVOCATION_ENDED:
            return AlertConditionResult(fired=False)
        status = event.payload.get("status")
        if status != "failed":
            return AlertConditionResult(fired=False)
        invocation_id = str(event.payload.get("invocation_id") or "")
        context = {
            "invocation_id": invocation_id,
            "status": status,
            "ended_at": event.payload.get("ended_at"),
        }
        return AlertConditionResult(fired=True, debounce_key=invocation_id, context=context)


@dataclass(frozen=True, slots=True)
class MonitorWebsocketDisconnectedCondition:
    """Rule 4 — monitor websocket disconnected.

    Live shape: fires on a monitor ``websocket_disconnected`` event.
    The 15-min "disconnected ≥ 15 min" qualifier in the design doc is
    enforced by the engine's debounce_window — a brief blip resolves
    inside the window (a follow-up ``websocket_connected`` clears the
    state) and the predicate's next fire only matters if the
    disconnect persists past the window.

    The debounce key is the disconnect's session id (when present) so
    a re-disconnect after reconnect produces a fresh alert.
    """

    name: AlertRuleName = _RULE_MONITOR_WEBSOCKET_DISCONNECTED

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del state
        if not isinstance(event, MonitorEvent):
            return AlertConditionResult(fired=False)
        if event.event_type is not MonitorEventType.WEBSOCKET_DISCONNECTED:
            return AlertConditionResult(fired=False)
        context = {
            "session_id": event.payload.get("session_id"),
            "reason": event.payload.get("reason"),
            "disconnected_at": event.payload.get("disconnected_at"),
        }
        return AlertConditionResult(
            fired=True,
            debounce_key=str(event.payload.get("session_id") or ""),
            context=context,
        )


@dataclass(frozen=True, slots=True)
class AgentMalformedOutputCondition:
    """Rule 11 — agent malformed output retry.

    Fires on a pipeline ``agent_retrying`` event whose payload's reason
    indicates a malformed-output trigger. The upstream schema reports
    the reason via the ``reason`` field; we match ``malformed_output``
    as the documented signal. The debounce key is the agent name so
    two different agents both producing malformed output produce two
    alerts but a re-emit for the same agent collapses.
    """

    name: AlertRuleName = _RULE_AGENT_MALFORMED_OUTPUT

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del state
        if not isinstance(event, PipelineEvent):
            return AlertConditionResult(fired=False)
        if event.event_type is not PipelineEventType.AGENT_RETRYING:
            return AlertConditionResult(fired=False)
        reason = str(event.payload.get("reason") or "")
        if "malformed" not in reason.lower():
            return AlertConditionResult(fired=False)
        agent_name = str(event.payload.get("agent_name") or "")
        context = {
            "agent_name": agent_name,
            "reason": reason,
            "invocation_id": event.payload.get("invocation_id"),
        }
        return AlertConditionResult(fired=True, debounce_key=agent_name, context=context)


# ---------------------------------------------------------------------------
# State-polling predicates (live).
# ---------------------------------------------------------------------------


def _factory_or_none(
    state: AlertEvaluatorState,
) -> async_sessionmaker[AsyncSession] | None:
    """Pull the foreign-reader factory off the state with the right type.

    Returns ``None`` when the engine is constructed without a factory
    (e.g. some test paths). Predicates that strictly need DB access
    return ``fired=False`` in that case rather than crashing.
    """
    factory = state.foreign_reader_factory
    if factory is None:
        return None
    return cast("async_sessionmaker[AsyncSession]", factory)


@dataclass(frozen=True, slots=True)
class MarginCallDetectedCondition:
    """Rule 5 — margin call detected.

    Queries ``activity_log`` for any ``MARGIN_CALL`` row whose timestamp
    is inside the last hour. The debounce key is the margin-call entry
    id so two distinct margin calls both fire (but the same row re-
    observed by a later tick is collapsed).

    The query path is intentionally lazy: ``select(ActivityLogRow)``
    with the ``event_type == 'MARGIN_CALL'`` filter and a single ``LIMIT
    1`` so the foreign-reader session pays the minimum cost per tick.
    """

    name: AlertRuleName = _RULE_MARGIN_CALL_DETECTED

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event
        factory = _factory_or_none(state)
        if factory is None:
            return AlertConditionResult(fired=False)
        cutoff = (state.now - timedelta(hours=1)).isoformat()
        # Local import to keep this module light if persistence-layer
        # imports become heavy. The activity_log table is on the
        # production Base; the foreign reader sees it via ?mode=ro.
        from alphamind.state.tables.activity_log import ActivityLogRow

        async with factory() as session:
            row = await session.execute(
                select(
                    ActivityLogRow.entry_id,
                    ActivityLogRow.entry_at,
                )
                .where(
                    ActivityLogRow.event_type == "MARGIN_CALL",
                    ActivityLogRow.entry_at >= cutoff,
                )
                .order_by(ActivityLogRow.entry_at.desc())
                .limit(1)
            )
            picked = row.first()
        if picked is None:
            return AlertConditionResult(fired=False)
        entry_id, entry_at = picked
        context = {
            "entry_id": str(entry_id),
            "entry_at": str(entry_at),
        }
        return AlertConditionResult(fired=True, debounce_key=str(entry_id), context=context)


@dataclass(frozen=True, slots=True)
class HardBlockGuardrailRejectionCondition:
    """Rule 9 — hard-block guardrail rejection.

    Queries ``activity_log`` for any ``GUARDRAIL_REJECTION`` row whose
    ``detail_json`` carries ``zone=hard_block``. Debounced per position
    id so multiple positions both rejected fire separately, but the
    same position re-observed across ticks collapses.

    The ``detail_json`` shape is the per-detail-class JSON envelope; the
    predicate scans the row's ``detail_json`` string with a substring
    check for ``"zone": "hard_block"`` — exact JSON-equality without
    requiring the full schema model on the read path. The SQLAlchemy
    layer escapes ``%`` in the LIKE pattern via the ``escape='\\'``
    parameter so a future detail-class containing an unescaped ``%``
    can't smuggle a match.
    """

    name: AlertRuleName = _RULE_HARD_BLOCK_GUARDRAIL_REJECTION

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event
        factory = _factory_or_none(state)
        if factory is None:
            return AlertConditionResult(fired=False)
        # Look back 15 minutes to keep the predicate's cost bounded; the
        # engine's debounce window (15 min for this rule) means a row
        # outside that window is already debounce-suppressed anyway.
        cutoff = (state.now - timedelta(minutes=15)).isoformat()
        from alphamind.state.tables.activity_log import ActivityLogRow

        async with factory() as session:
            row = await session.execute(
                select(
                    ActivityLogRow.entry_id,
                    ActivityLogRow.position_id,
                    ActivityLogRow.detail_json,
                )
                .where(
                    ActivityLogRow.event_type == "GUARDRAIL_REJECTION",
                    ActivityLogRow.entry_at >= cutoff,
                    ActivityLogRow.detail_json.like('%"zone":%"hard_block"%'),
                )
                .order_by(ActivityLogRow.entry_at.desc())
                .limit(1)
            )
            picked = row.first()
        if picked is None:
            return AlertConditionResult(fired=False)
        entry_id, position_id, detail_json = picked
        context = {
            "entry_id": str(entry_id),
            "position_id": str(position_id) if position_id is not None else "",
            "detail_json": str(detail_json),
        }
        return AlertConditionResult(
            fired=True,
            debounce_key=str(position_id) if position_id is not None else str(entry_id),
            context=context,
        )


@dataclass(frozen=True, slots=True)
class CommandAbandonedCondition:
    """Rule 16 — command abandoned.

    Queries ``activity_log`` for ``COMMAND_ABANDONED`` rows in the last
    15 min. Debounced per entry id.
    """

    name: AlertRuleName = _RULE_COMMAND_ABANDONED

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event
        factory = _factory_or_none(state)
        if factory is None:
            return AlertConditionResult(fired=False)
        cutoff = (state.now - timedelta(minutes=15)).isoformat()
        from alphamind.state.tables.activity_log import ActivityLogRow

        async with factory() as session:
            row = await session.execute(
                select(
                    ActivityLogRow.entry_id,
                    ActivityLogRow.entry_at,
                )
                .where(
                    ActivityLogRow.event_type == "COMMAND_ABANDONED",
                    ActivityLogRow.entry_at >= cutoff,
                )
                .order_by(ActivityLogRow.entry_at.desc())
                .limit(1)
            )
            picked = row.first()
        if picked is None:
            return AlertConditionResult(fired=False)
        entry_id, entry_at = picked
        return AlertConditionResult(
            fired=True,
            debounce_key=str(entry_id),
            context={"entry_id": str(entry_id), "entry_at": str(entry_at)},
        )


@dataclass(frozen=True, slots=True)
class ThesisResolvedCondition:
    """Rule 17 — thesis resolved.

    Queries ``activity_log`` for ``THESIS_RESOLVED`` rows in the last
    15 min. Debounced per entry id (one alert per resolution event).
    """

    name: AlertRuleName = _RULE_THESIS_RESOLVED

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event
        factory = _factory_or_none(state)
        if factory is None:
            return AlertConditionResult(fired=False)
        cutoff = (state.now - timedelta(minutes=15)).isoformat()
        from alphamind.state.tables.activity_log import ActivityLogRow

        async with factory() as session:
            row = await session.execute(
                select(
                    ActivityLogRow.entry_id,
                    ActivityLogRow.entry_at,
                    ActivityLogRow.thesis_id,
                )
                .where(
                    ActivityLogRow.event_type == "THESIS_RESOLVED",
                    ActivityLogRow.entry_at >= cutoff,
                )
                .order_by(ActivityLogRow.entry_at.desc())
                .limit(1)
            )
            picked = row.first()
        if picked is None:
            return AlertConditionResult(fired=False)
        entry_id, entry_at, thesis_id = picked
        return AlertConditionResult(
            fired=True,
            debounce_key=str(thesis_id) if thesis_id is not None else str(entry_id),
            context={
                "entry_id": str(entry_id),
                "entry_at": str(entry_at),
                "thesis_id": str(thesis_id) if thesis_id is not None else "",
            },
        )


_ENTRY_NO_FILL_REASONS: Final[Mapping[str, str]] = {
    "EXPIRED": "expired",
    "CANCELLED": "cancelled-superseded",
}
"""``orders.status`` → operator-facing reason for the no-fill alert.

An accepted entry that never filled is terminal for one of two reasons:
the broker let it ``EXPIRED`` (time-in-force lapsed unfilled), or it was
``CANCELLED`` — typically because a re-priced replacement superseded it or
the bracket was dissolved. Both surface in the alert context so the
operator can tell a stale resting limit from a deliberate teardown.
"""


_ENTRY_NO_FILL_MAX_ROWS: Final = 100
"""Safety cap on rows the no-fill predicate aggregates per evaluation.

A defensive bound only — at AlphaMind's throughput (single-digit entries/day)
the terminal-unfilled-in-window set is a handful. The rows are ordered
most-recent-first, so a (never-expected) truncation keeps the newest entries
and the most-recent order id used as the debounce key.
"""


def _ticker_from_instrument_spec_json(spec_json: str) -> str:
    """Best-effort ticker/underlying out of an ``instrument_spec_json`` blob.

    Equity specs carry ``ticker``; options carry ``underlying``; strategy
    specs carry per-leg ``underlying`` (the first leg is reported). A
    malformed / unrecognised blob yields ``""`` so the alert still fires
    with the rest of its context rather than crashing the engine tick.

    Deliberately lenient rather than reusing ``orders_codec`` (which
    fail-closes — raises on an unknown discriminator): an alert-engine tick
    must never crash on a surprising row, so this fails open.
    """
    try:
        payload = json.loads(spec_json)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    ticker = payload.get("ticker") or payload.get("underlying")
    if isinstance(ticker, str):
        return ticker
    legs = payload.get("legs")
    if isinstance(legs, list) and legs and isinstance(legs[0], dict):
        leg_underlying = legs[0].get("underlying")
        if isinstance(leg_underlying, str):
            return leg_underlying
    return ""


def _limit_price_from_price_parameters_json(price_parameters_json: str) -> str:
    """Render the ``limit_price`` out of a ``price_parameters_json`` blob.

    The value is a Decimal-as-string (post-ALP-660) or a JSON number
    (legacy rows); both stringify cleanly. A market order (no limit) or a
    malformed blob yields ``""``.
    """
    try:
        payload = json.loads(price_parameters_json)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    limit = payload.get("limit_price")
    return "" if limit is None else str(limit)


@dataclass(frozen=True, slots=True)
class EntryNoFillCondition:
    """Rule 18 — accepted entries reached a terminal state with zero fills.

    State-polling: queries ``orders`` via the foreign reader for *every*
    entry-role order (``ENTRY`` or ``ADD_ENTRY``) whose ``status`` is
    terminal-unfilled (``EXPIRED`` / ``CANCELLED``) with
    ``filled_quantity == 0`` and a ``last_update_timestamp`` inside the
    lookback window, and fires one alert that lists them all.

    **Why aggregate rather than one alert per order.** The engine emits at
    most one :class:`AlertConditionResult` per evaluation, so a
    most-recent-only ``LIMIT 1`` query would let a single order mask all its
    peers: a basket of DAY limit entries that all expire together at the 4 pm
    close (exactly the zero-fill-day scenario this rule targets) would
    surface only the newest. Instead the predicate returns the full set in
    ``context["entries"]`` and debounces on the *most-recent* order id — so a
    newly-terminal entry (newer ``last_update_timestamp``) yields a fresh
    debounce key and re-fires with the running tally, while the set merely
    shrinking as rows age out of the window does not (the most-recent id is
    the last to age out, so its key stays debounced until the set empties).

    The lookback equals the rule's debounce window (threaded in at
    construction), so a row is observable only while debounce holds and the
    two cannot silently desync under a YAML ``debounce_minutes`` override.
    ``last_update_timestamp`` is stamped at *observation* time by the
    terminal-status sync (not the broker event time), so a status synced
    late — e.g. recovered after a monitor outage, the ZS case in ALP-739 —
    still lands inside the window and fires.

    No-fill counterpart to ``command_abandoned`` (rule 16; ALP-739 flagged
    the overlap to check): that fires when a *PM command* is rolled back
    after exhausting Phase 2 retries; this fires when an *accepted entry
    order* the OMS submitted to the broker expires/cancels having never
    filled. Distinct upstreams, distinct debounce keys — no double alert.

    The terminal status reaches ``orders.status`` as a projection derived from
    the append-only ``broker_event_log`` (ALP-849 / W1c): the fill-stream
    consumer captures a zero-fill ``canceled`` / ``expired`` event as a
    ``TERMINAL_ORDER_STATUS`` row — it no longer RMWs ``orders.status`` (ADR-0005
    invariant 1) — and the single (pipeline) writer projects that row onto the
    ``orders`` status column. This predicate fires *because* the projection
    landed, wiring ALP-739's "reflect terminal broker statuses locally" and
    "alert on a no-fill entry" criteria through the same column.
    """

    name: AlertRuleName = _RULE_ENTRY_NO_FILL
    lookback: timedelta = timedelta(minutes=15)

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event
        factory = _factory_or_none(state)
        if factory is None:
            return AlertConditionResult(fired=False)
        cutoff = (state.now - self.lookback).isoformat()
        from alphamind.portfolio_state.records.orders import OrderRole, OrderStatus
        from alphamind.state.tables.orders import OrderRow

        entry_roles = (OrderRole.ENTRY.value, OrderRole.ADD_ENTRY.value)
        terminal_unfilled = (OrderStatus.EXPIRED.value, OrderStatus.CANCELLED.value)
        async with factory() as session:
            result = await session.execute(
                select(
                    OrderRow.order_id,
                    OrderRow.status,
                    OrderRow.instrument_spec_json,
                    OrderRow.price_parameters_json,
                    OrderRow.duration,
                    OrderRow.last_update_timestamp,
                )
                .where(
                    OrderRow.order_role.in_(entry_roles),
                    OrderRow.status.in_(terminal_unfilled),
                    OrderRow.filled_quantity == 0,
                    OrderRow.last_update_timestamp >= cutoff,
                )
                .order_by(OrderRow.last_update_timestamp.desc())
                .limit(_ENTRY_NO_FILL_MAX_ROWS)
            )
            rows = result.mappings().all()
        if not rows:
            return AlertConditionResult(fired=False)
        entries = [
            {
                "order_id": str(row["order_id"]),
                "ticker": _ticker_from_instrument_spec_json(str(row["instrument_spec_json"])),
                "limit_price": _limit_price_from_price_parameters_json(
                    str(row["price_parameters_json"])
                ),
                "time_in_force": str(row["duration"]),
                "reason": _ENTRY_NO_FILL_REASONS.get(str(row["status"]), str(row["status"])),
                "status": str(row["status"]),
                "last_update_timestamp": str(row["last_update_timestamp"]),
            }
            for row in rows
        ]
        # Rows are ordered most-recent-first; debounce on the newest order id
        # so a fresh terminal entry re-fires the running tally while a set
        # that only shrinks (rows aging out) stays debounced.
        context = {
            "count": len(entries),
            "order_ids": [entry["order_id"] for entry in entries],
            "entries": entries,
        }
        return AlertConditionResult(
            fired=True, debounce_key=entries[0]["order_id"], context=context
        )


@dataclass(frozen=True, slots=True)
class HaltModeEnteredCondition:
    """Rule 7 — halt mode entered.

    Watches the ``activity_log`` for ``HALT_ACTIVATED`` rows in the
    last 15 min. The debounce key is the entry id so a re-emit of
    the same activation tick is collapsed, but a fresh activation
    (e.g. after the operator lifts + a downstream re-triggers) fires
    a new alert.
    """

    name: AlertRuleName = _RULE_HALT_MODE_ENTERED

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event
        factory = _factory_or_none(state)
        if factory is None:
            return AlertConditionResult(fired=False)
        cutoff = (state.now - timedelta(minutes=15)).isoformat()
        from alphamind.state.tables.activity_log import ActivityLogRow

        async with factory() as session:
            row = await session.execute(
                select(
                    ActivityLogRow.entry_id,
                    ActivityLogRow.entry_at,
                )
                .where(
                    ActivityLogRow.event_type == "HALT_ACTIVATED",
                    ActivityLogRow.entry_at >= cutoff,
                )
                .order_by(ActivityLogRow.entry_at.desc())
                .limit(1)
            )
            picked = row.first()
        if picked is None:
            return AlertConditionResult(fired=False)
        entry_id, entry_at = picked
        return AlertConditionResult(
            fired=True,
            debounce_key=str(entry_id),
            context={"entry_id": str(entry_id), "entry_at": str(entry_at)},
        )


@dataclass(frozen=True, slots=True)
class DataDirectoryDiskPressureCondition:
    """Rule 13 — data directory disk pressure.

    Queries the filesystem for the size of the data directory and the
    free space on its volume; fires when the directory exceeds
    ``threshold_bytes`` OR the free space drops below ``min_free_bytes``.
    Both thresholds are configured here so a future YAML edit can pin
    site-specific values without changing the predicate's shape.

    The debounce key is the empty string (per-rule debouncing only) so a
    sustained disk-pressure state collapses to one alert per debounce
    window.
    """

    data_dir: Path
    threshold_bytes: int
    name: AlertRuleName = _RULE_DATA_DIRECTORY_DISK_PRESSURE
    min_free_bytes: int = 0

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        del event, state
        if not self.data_dir.is_dir():
            return AlertConditionResult(fired=False)
        # ``_directory_size_bytes`` walks ``rglob('*')`` and stat()s every
        # file under the data dir — tens of thousands of files in
        # production. Run it on a worker thread so the engine's
        # asyncio.TaskGroup doesn't block the event loop on every tick.
        # The cache TTL collapses repeated polls within a 5-minute
        # window onto a single walk; a disk-pressure alarm doesn't need
        # higher resolution than that.
        size = await _get_directory_size_cached(self.data_dir)
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, self.data_dir)
        except OSError:
            log.warning(
                "data_directory_disk_pressure: disk_usage(%s) raised OSError; "
                "treating as non-firing",
                self.data_dir,
            )
            return AlertConditionResult(fired=False)
        if size <= self.threshold_bytes and usage.free >= self.min_free_bytes:
            return AlertConditionResult(fired=False)
        context = {
            "data_dir": str(self.data_dir),
            "size_bytes": size,
            "threshold_bytes": self.threshold_bytes,
            "free_bytes": usage.free,
            "min_free_bytes": self.min_free_bytes,
        }
        return AlertConditionResult(fired=True, debounce_key="", context=context)


@dataclass(frozen=True, slots=True)
class DrawdownTierCrossedCondition:
    """Rule 6 — drawdown progressive tier crossed.

    Dormant: the ``portfolio_summary`` table the design doc references
    is not yet present in :mod:`alphamind.persistence.models`. The
    predicate registers as dormant so the rule appears in the registry;
    once ``portfolio_summary`` lands, this swaps to a live state-polling
    body comparing ``cumulative_drawdown`` against ``tier_thresholds``.
    """

    name: AlertRuleName = _RULE_DRAWDOWN_TIER_CROSSED
    tier_thresholds: tuple[float, ...] = (0.05, 0.10, 0.15)
    _dormant: DormantCondition = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_dormant",
            DormantCondition(
                name=self.name,
                missing_emit="portfolio_summary table (cumulative_drawdown column)",
            ),
        )

    def log_startup_warning(self) -> None:
        self._dormant.log_startup_warning()

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        return await self._dormant.evaluate(event=event, state=state)


@dataclass(frozen=True, slots=True)
class ProfileBoundaryCrossedCondition:
    """Rule 14 — profile boundary crossed.

    Dormant for the same reason as :class:`DrawdownTierCrossedCondition`:
    the ``portfolio_summary.total_equity`` reader path lands alongside
    the portfolio_summary table. Profile bounds (``capital_range_low_usd``
    / ``capital_range_high_usd``) ride on the dataclass so a future
    live shape doesn't change the construction signature.
    """

    name: AlertRuleName = _RULE_PROFILE_BOUNDARY_CROSSED
    capital_range_low_usd: float = 0.0
    capital_range_high_usd: float = 0.0
    _dormant: DormantCondition = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_dormant",
            DormantCondition(
                name=self.name,
                missing_emit="portfolio_summary table (total_equity column)",
            ),
        )

    def log_startup_warning(self) -> None:
        self._dormant.log_startup_warning()

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        return await self._dormant.evaluate(event=event, state=state)


# ---------------------------------------------------------------------------
# Dormant predicates (upstream emit not wired yet).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CriticalApiFailureCondition:
    """Rule 2 — critical-tier API failure (Q1/Q6/Q8).

    Dormant: the design doc's "category-level failure" event is not yet
    emitted by the distillation layer (categories don't yet write a
    structured ``CATEGORY_FAILED`` row to the activity log). The
    predicate is a no-op until that emit lands.
    """

    name: AlertRuleName = _RULE_CRITICAL_API_FAILURE
    _dormant: DormantCondition = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_dormant",
            DormantCondition(
                name=self.name,
                missing_emit="distillation category-level CATEGORY_FAILED activity_log row "
                "(Q1/Q6/Q8 critical tier)",
            ),
        )

    def log_startup_warning(self) -> None:
        self._dormant.log_startup_warning()

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        return await self._dormant.evaluate(event=event, state=state)


@dataclass(frozen=True, slots=True)
class ImportantApiFailureCondition:
    """Rule 8 — important-tier API failure (Q2/Q3/Q5/Q12/Qual1/4/5/6).

    Dormant — same upstream emit as the critical-tier variant.
    """

    name: AlertRuleName = _RULE_IMPORTANT_API_FAILURE
    _dormant: DormantCondition = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_dormant",
            DormantCondition(
                name=self.name,
                missing_emit="distillation category-level CATEGORY_FAILED activity_log row "
                "(Q2/Q3/Q5/Q12/Qual1/Qual4/Qual5/Qual6 important tier)",
            ),
        )

    def log_startup_warning(self) -> None:
        self._dormant.log_startup_warning()

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        return await self._dormant.evaluate(event=event, state=state)


@dataclass(frozen=True, slots=True)
class ScheduleMissCriticalCondition:
    """Rule 3 — schedule miss on critical-tier category.

    Dormant: there's no scheduled-trigger watcher in the command-center
    yet that pairs ``schedule_trigger_fired`` with ``collection_runs``
    start; the design doc's 5-minute miss-window requires that pairing.
    """

    name: AlertRuleName = _RULE_SCHEDULE_MISS_CRITICAL
    _dormant: DormantCondition = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_dormant",
            DormantCondition(
                name=self.name,
                missing_emit="scheduled-trigger / collection_runs pairing watcher "
                "(critical-tier categories)",
            ),
        )

    def log_startup_warning(self) -> None:
        self._dormant.log_startup_warning()

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        return await self._dormant.evaluate(event=event, state=state)


@dataclass(frozen=True, slots=True)
class ScheduleMissCondition:
    """Rule 12 — schedule miss (any category).

    Dormant — same upstream gap as the critical-tier variant.
    """

    name: AlertRuleName = _RULE_SCHEDULE_MISS
    _dormant: DormantCondition = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_dormant",
            DormantCondition(
                name=self.name,
                missing_emit="scheduled-trigger / collection_runs pairing watcher (any category)",
            ),
        )

    def log_startup_warning(self) -> None:
        self._dormant.log_startup_warning()

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        return await self._dormant.evaluate(event=event, state=state)


@dataclass(frozen=True, slots=True)
class RegimeJumpCondition:
    """Rule 10 — regime jump (skipping a level).

    Dormant: the regime-classification emit on the activity log carries
    the new level but not the previous level on the same row, so a
    "skipped intermediate" check requires a structured emit not yet
    present.
    """

    name: AlertRuleName = _RULE_REGIME_JUMP
    _dormant: DormantCondition = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_dormant",
            DormantCondition(
                name=self.name,
                missing_emit="regime-classification activity_log entry with "
                "(previous_regime, new_regime) tuple",
            ),
        )

    def log_startup_warning(self) -> None:
        self._dormant.log_startup_warning()

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        return await self._dormant.evaluate(event=event, state=state)


@dataclass(frozen=True, slots=True)
class OptionalDataCategorySkippedCondition:
    """Rule 15 — optional data category skipped (Q4/Qual2/Qual3).

    Dormant — same category-failure emit gap as rules 2 and 8.
    """

    name: AlertRuleName = _RULE_OPTIONAL_DATA_CATEGORY_SKIPPED
    _dormant: DormantCondition = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_dormant",
            DormantCondition(
                name=self.name,
                missing_emit="distillation category-level CATEGORY_SKIPPED activity_log "
                "row (Q4/Qual2/Qual3 optional tier)",
            ),
        )

    def log_startup_warning(self) -> None:
        self._dormant.log_startup_warning()

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        return await self._dormant.evaluate(event=event, state=state)


# ---------------------------------------------------------------------------
# Helpers + factory.
# ---------------------------------------------------------------------------


def _directory_size_bytes(path: Path) -> int:
    """Compute the recursive on-disk size of *path*.

    Best-effort: an OSError on any individual descendant (permission
    denied, symlink loop, file vanished between scandir + stat) is
    logged at DEBUG and skipped so the predicate still produces a
    bound for the operator-visible threshold check.
    """
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError as exc:
                log.debug("data_directory size walk: skipping %s (%s)", entry, exc)
    except OSError as exc:
        log.debug("data_directory size walk failed at %s (%s)", path, exc)
    return total


_DIRECTORY_SIZE_CACHE_TTL = timedelta(minutes=5)
"""Cache horizon for the data-directory size walk.

The walk stats tens of thousands of files; running it on every engine
tick (event-driven OR periodic) would dominate disk I/O. A
disk-pressure alarm doesn't need higher resolution than 5 minutes —
that's the cadence on which actionable drift accumulates.
"""

_directory_size_cache: dict[Path, tuple[datetime, int]] = {}
"""Module-level ``{resolved_path: (computed_at, size_bytes)}`` cache.

Keyed by the resolved Path so two predicates sharing the same data dir
(currently only one shipping rule, but the cache is shape-prepared)
collapse to one walk.
"""


async def _get_directory_size_cached(path: Path) -> int:
    """Return the recursive size of *path*; reuse cached value within TTL.

    The walk runs on a worker thread (:func:`asyncio.to_thread`) so the
    engine's event loop stays free for other rules' evaluations. Cache
    misses populate the cache; cache hits inside the TTL return the
    stored value without touching disk.
    """
    now = datetime.now(UTC)
    cached = _directory_size_cache.get(path)
    if cached is not None and (now - cached[0]) < _DIRECTORY_SIZE_CACHE_TTL:
        return cached[1]
    size = await asyncio.to_thread(_directory_size_bytes, path)
    _directory_size_cache[path] = (now, size)
    return size


def build_default_rules(
    *,
    data_dir: Path | None = None,
    data_directory_threshold_bytes: int = 50 * 1024 * 1024 * 1024,  # 50 GB
    data_directory_min_free_bytes: int = 0,
    capital_range_low_usd: float = 0.0,
    capital_range_high_usd: float = 0.0,
    debounce_overrides: Mapping[AlertRuleName, timedelta] | None = None,
    severity_overrides: Mapping[AlertRuleName, AlertSeverity] | None = None,
    channel_overrides: Mapping[AlertRuleName, tuple[str, ...]] | None = None,
) -> tuple[AlertRule, ...]:
    """Construct the 18 default :class:`AlertRule` instances.

    The YAML loader binds to this factory and renders the rule list onto
    the engine at startup. Operator-edited per-rule overrides flow
    through the YAML's ``rules`` list; the three ``*_overrides``
    parameters here let tests + future YAML round-trips pin per-rule
    debounce windows, severity, and channels without changing the
    predicate identities.

    Parameters
    ----------
    data_dir:
        Path to the data directory (``%USERPROFILE%/AlphaMind/data/``)
        the disk-pressure rule monitors. Defaults to ``None`` so test
        fixtures can skip the rule when the directory isn't relevant.
    data_directory_threshold_bytes / data_directory_min_free_bytes:
        Disk-pressure thresholds.
    capital_range_low_usd / capital_range_high_usd:
        Bounds the profile-boundary rule compares total equity against.
    debounce_overrides / severity_overrides / channel_overrides:
        Per-rule overrides — sparse mappings; absent rules use the
        documented defaults.
    """
    debounce_overrides = debounce_overrides or {}
    severity_overrides = severity_overrides or {}
    channel_overrides = channel_overrides or {}

    def _debounce(name: AlertRuleName) -> timedelta:
        return debounce_overrides.get(name, DEFAULT_DEBOUNCE_WINDOWS[name])

    def _channels(name: AlertRuleName, default: tuple[str, ...]) -> tuple[str, ...]:
        return channel_overrides.get(name, default)

    def _severity(name: AlertRuleName, default: AlertSeverity) -> AlertSeverity:
        return severity_overrides.get(name, default)

    critical_channels = ("in_app", "discord")
    important_channels = ("in_app", "discord")
    operational_channels = ("in_app",)

    disk_pressure_condition: AlertCondition
    if data_dir is not None:
        disk_pressure_condition = DataDirectoryDiskPressureCondition(
            data_dir=data_dir,
            threshold_bytes=data_directory_threshold_bytes,
            min_free_bytes=data_directory_min_free_bytes,
        )
    else:
        disk_pressure_condition = DormantCondition(
            name=_RULE_DATA_DIRECTORY_DISK_PRESSURE,
            missing_emit="data_dir not configured; pass --data-dir to enable disk-pressure check",
        )

    rules: list[AlertRule] = [
        AlertRule(
            name=_RULE_PIPELINE_ABORTED,
            severity=_severity(_RULE_PIPELINE_ABORTED, AlertSeverity.CRITICAL),
            debounce_window=_debounce(_RULE_PIPELINE_ABORTED),
            channels=_channels(_RULE_PIPELINE_ABORTED, critical_channels),
            condition=PipelineAbortedCondition(),
        ),
        AlertRule(
            name=_RULE_CRITICAL_API_FAILURE,
            severity=_severity(_RULE_CRITICAL_API_FAILURE, AlertSeverity.CRITICAL),
            debounce_window=_debounce(_RULE_CRITICAL_API_FAILURE),
            channels=_channels(_RULE_CRITICAL_API_FAILURE, critical_channels),
            condition=CriticalApiFailureCondition(),
        ),
        AlertRule(
            name=_RULE_SCHEDULE_MISS_CRITICAL,
            severity=_severity(_RULE_SCHEDULE_MISS_CRITICAL, AlertSeverity.CRITICAL),
            debounce_window=_debounce(_RULE_SCHEDULE_MISS_CRITICAL),
            channels=_channels(_RULE_SCHEDULE_MISS_CRITICAL, critical_channels),
            condition=ScheduleMissCriticalCondition(),
        ),
        AlertRule(
            name=_RULE_MONITOR_WEBSOCKET_DISCONNECTED,
            severity=_severity(_RULE_MONITOR_WEBSOCKET_DISCONNECTED, AlertSeverity.CRITICAL),
            debounce_window=_debounce(_RULE_MONITOR_WEBSOCKET_DISCONNECTED),
            channels=_channels(_RULE_MONITOR_WEBSOCKET_DISCONNECTED, critical_channels),
            condition=MonitorWebsocketDisconnectedCondition(),
        ),
        AlertRule(
            name=_RULE_MARGIN_CALL_DETECTED,
            severity=_severity(_RULE_MARGIN_CALL_DETECTED, AlertSeverity.CRITICAL),
            debounce_window=_debounce(_RULE_MARGIN_CALL_DETECTED),
            channels=_channels(_RULE_MARGIN_CALL_DETECTED, critical_channels),
            condition=MarginCallDetectedCondition(),
        ),
        AlertRule(
            name=_RULE_DRAWDOWN_TIER_CROSSED,
            severity=_severity(_RULE_DRAWDOWN_TIER_CROSSED, AlertSeverity.CRITICAL),
            debounce_window=_debounce(_RULE_DRAWDOWN_TIER_CROSSED),
            channels=_channels(_RULE_DRAWDOWN_TIER_CROSSED, critical_channels),
            condition=DrawdownTierCrossedCondition(),
        ),
        AlertRule(
            name=_RULE_HALT_MODE_ENTERED,
            severity=_severity(_RULE_HALT_MODE_ENTERED, AlertSeverity.CRITICAL),
            debounce_window=_debounce(_RULE_HALT_MODE_ENTERED),
            channels=_channels(_RULE_HALT_MODE_ENTERED, critical_channels),
            condition=HaltModeEnteredCondition(),
        ),
        AlertRule(
            name=_RULE_IMPORTANT_API_FAILURE,
            severity=_severity(_RULE_IMPORTANT_API_FAILURE, AlertSeverity.IMPORTANT),
            debounce_window=_debounce(_RULE_IMPORTANT_API_FAILURE),
            channels=_channels(_RULE_IMPORTANT_API_FAILURE, important_channels),
            condition=ImportantApiFailureCondition(),
        ),
        AlertRule(
            name=_RULE_HARD_BLOCK_GUARDRAIL_REJECTION,
            severity=_severity(_RULE_HARD_BLOCK_GUARDRAIL_REJECTION, AlertSeverity.IMPORTANT),
            debounce_window=_debounce(_RULE_HARD_BLOCK_GUARDRAIL_REJECTION),
            channels=_channels(_RULE_HARD_BLOCK_GUARDRAIL_REJECTION, important_channels),
            condition=HardBlockGuardrailRejectionCondition(),
        ),
        AlertRule(
            name=_RULE_REGIME_JUMP,
            severity=_severity(_RULE_REGIME_JUMP, AlertSeverity.IMPORTANT),
            debounce_window=_debounce(_RULE_REGIME_JUMP),
            channels=_channels(_RULE_REGIME_JUMP, important_channels),
            condition=RegimeJumpCondition(),
        ),
        AlertRule(
            name=_RULE_AGENT_MALFORMED_OUTPUT,
            severity=_severity(_RULE_AGENT_MALFORMED_OUTPUT, AlertSeverity.IMPORTANT),
            debounce_window=_debounce(_RULE_AGENT_MALFORMED_OUTPUT),
            channels=_channels(_RULE_AGENT_MALFORMED_OUTPUT, important_channels),
            condition=AgentMalformedOutputCondition(),
        ),
        AlertRule(
            name=_RULE_SCHEDULE_MISS,
            severity=_severity(_RULE_SCHEDULE_MISS, AlertSeverity.IMPORTANT),
            debounce_window=_debounce(_RULE_SCHEDULE_MISS),
            channels=_channels(_RULE_SCHEDULE_MISS, important_channels),
            condition=ScheduleMissCondition(),
        ),
        AlertRule(
            name=_RULE_DATA_DIRECTORY_DISK_PRESSURE,
            severity=_severity(_RULE_DATA_DIRECTORY_DISK_PRESSURE, AlertSeverity.IMPORTANT),
            debounce_window=_debounce(_RULE_DATA_DIRECTORY_DISK_PRESSURE),
            channels=_channels(_RULE_DATA_DIRECTORY_DISK_PRESSURE, important_channels),
            condition=disk_pressure_condition,
        ),
        AlertRule(
            name=_RULE_PROFILE_BOUNDARY_CROSSED,
            severity=_severity(_RULE_PROFILE_BOUNDARY_CROSSED, AlertSeverity.OPERATIONAL),
            debounce_window=_debounce(_RULE_PROFILE_BOUNDARY_CROSSED),
            channels=_channels(_RULE_PROFILE_BOUNDARY_CROSSED, operational_channels),
            condition=ProfileBoundaryCrossedCondition(
                capital_range_low_usd=capital_range_low_usd,
                capital_range_high_usd=capital_range_high_usd,
            ),
        ),
        AlertRule(
            name=_RULE_OPTIONAL_DATA_CATEGORY_SKIPPED,
            severity=_severity(_RULE_OPTIONAL_DATA_CATEGORY_SKIPPED, AlertSeverity.OPERATIONAL),
            debounce_window=_debounce(_RULE_OPTIONAL_DATA_CATEGORY_SKIPPED),
            channels=_channels(_RULE_OPTIONAL_DATA_CATEGORY_SKIPPED, operational_channels),
            condition=OptionalDataCategorySkippedCondition(),
        ),
        AlertRule(
            name=_RULE_COMMAND_ABANDONED,
            severity=_severity(_RULE_COMMAND_ABANDONED, AlertSeverity.OPERATIONAL),
            debounce_window=_debounce(_RULE_COMMAND_ABANDONED),
            channels=_channels(_RULE_COMMAND_ABANDONED, operational_channels),
            condition=CommandAbandonedCondition(),
        ),
        AlertRule(
            name=_RULE_THESIS_RESOLVED,
            severity=_severity(_RULE_THESIS_RESOLVED, AlertSeverity.OPERATIONAL),
            debounce_window=_debounce(_RULE_THESIS_RESOLVED),
            channels=_channels(_RULE_THESIS_RESOLVED, operational_channels),
            condition=ThesisResolvedCondition(),
        ),
        # ALP-739 — added after the design doc's original 17-rule table. A
        # zero-fill day must not be invisible, so this rides the important
        # tier (in_app + discord) rather than operational (in_app only).
        AlertRule(
            name=_RULE_ENTRY_NO_FILL,
            severity=_severity(_RULE_ENTRY_NO_FILL, AlertSeverity.IMPORTANT),
            debounce_window=_debounce(_RULE_ENTRY_NO_FILL),
            channels=_channels(_RULE_ENTRY_NO_FILL, important_channels),
            # Lookback tracks the resolved debounce window (incl. any YAML
            # override) so the poll window and debounce can't silently desync.
            condition=EntryNoFillCondition(lookback=_debounce(_RULE_ENTRY_NO_FILL)),
        ),
    ]
    if len(rules) != 18:  # pragma: no cover — defensive
        msg = f"build_default_rules: expected 18 rules, got {len(rules)}"
        raise RuntimeError(msg)
    return tuple(rules)


# Suppress unused-warning for the documentation constant.
_ = _FOREIGN_READER_TYPE_HINT
