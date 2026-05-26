"""17 default alert condition predicates (ALP-671 / story 05a).

One frozen class per rule in :doc:`docs/design/command-center.md`
§ Alerting — Default rule set. Each implements
:class:`alphamind.command_center.alerts.rules.AlertCondition`.

Two predicate shapes:

* **Event-driven** — the predicate consumes the multiplexer's
  :class:`CombinedEvent` argument and returns an outcome when the
  event matches its trigger shape. State snapshot is ignored.
  Examples: pipeline aborted (consumes ``invocation_ended``),
  monitor websocket disconnected (consumes
  ``websocket_disconnected``), agent malformed output (consumes
  ``agent_retrying``).
* **State-polling** — the predicate ignores the event argument and
  reads the pre-fetched :class:`AlertEvaluatorState` snapshot
  (recent activity-log rows / latest portfolio summary / latest
  invocation / on-disk data-directory size). Fires when the
  snapshot crosses the rule's threshold.

Per the python-architecture P1 functional-core principle, every
predicate's ``evaluate`` is a total synchronous function over plain
values. The engine performs all I/O (DB reads, filesystem stat)
before calling into the predicates.

Dormant predicates whose upstream emit point does not yet exist in
this work tree's scope still register (so the engine wires them once
the upstream lands) but always return ``None``. The engine logs a
one-time WARNING per dormant predicate at startup via
:func:`log_dormant_predicates`.

Each predicate is a frozen dataclass so the rule registry can be
deep-copied / serialized without aliasing. Predicates with tunable
thresholds (drawdown tier, data-directory size) carry the threshold
as a field; the YAML loader injects the operator-pinned value at
construction time.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center.alerts.rules import (
    AlertCondition,
    AlertEvaluatorState,
    AlertOutcome,
)

__all__ = [
    "ALL_DEFAULT_CONDITIONS",
    "DEFAULT_DATA_DIRECTORY_THRESHOLD_BYTES",
    "DEFAULT_DRAWDOWN_TIER_THRESHOLD",
    "AgentMalformedOutputCondition",
    "CommandAbandonedCondition",
    "CriticalApiFailureCondition",
    "DataDirectoryDiskPressureCondition",
    "DrawdownTierCrossedCondition",
    "HaltModeEnteredCondition",
    "HardBlockGuardrailRejectionCondition",
    "ImportantApiFailureCondition",
    "MarginCallCondition",
    "MonitorWebsocketDisconnectedCondition",
    "OptionalDataCategorySkippedCondition",
    "PipelineAbortedCondition",
    "ProfileBoundaryCrossedCondition",
    "RegimeJumpCondition",
    "ScheduleMissCondition",
    "ScheduleMissCriticalCondition",
    "ThesisResolvedCondition",
    "log_dormant_predicates",
]

log = logging.getLogger(__name__)


DEFAULT_DRAWDOWN_TIER_THRESHOLD = 0.05
"""Default progressive-tier drawdown trip (5%).

Aligns with :doc:`docs/design/06-risk-guardrails/breach-behavior.md`'s
first progressive tier. The operator can override via
``config/alerts.yaml`` once the YAML schema lands.
"""


DEFAULT_DATA_DIRECTORY_THRESHOLD_BYTES = 50 * 1024 * 1024 * 1024
"""Default on-disk data-directory pressure threshold (50 GB).

Mirrors the design doc's "50 GB or 25% of the volume" default
candidate. Story 05a hard-codes 50 GB; the percent-of-volume
refinement is deferred per the design doc's "operator-pinned at
landing" note.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_get(event: PipelineEvent | MonitorEvent, key: str) -> Any | None:
    """Return ``event.payload[key]`` or ``None``."""
    payload = event.payload
    return payload.get(key) if isinstance(payload, Mapping) else None


def _activity_log_rows(
    state: AlertEvaluatorState, *, event_types: tuple[str, ...]
) -> tuple[Mapping[str, Any], ...]:
    """Filter the snapshot's activity-log rows by event_type.

    The engine pre-fetches a single batch of recent activity-log
    rows per evaluation tick (event_types tuple is the union of every
    state-polling predicate's interest set); each predicate filters
    the batch down to its own interest. Keeps the snapshot small
    and avoids per-predicate DB round-trips.
    """
    return tuple(r for r in state.recent_activity_log_rows if r.get("event_type") in event_types)


# ---------------------------------------------------------------------------
# Event-driven predicates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineAbortedCondition:
    """Fire when the most recent invocation ends with ``status=failed``.

    Triggered by an ``invocation_ended`` SSE frame whose payload
    carries ``status="failed"``. Primary entity is the invocation_id
    so re-fires for the *same* invocation debounce, but a fresh
    failed invocation refires immediately.
    """

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del state
        if not isinstance(event, PipelineEvent):
            return None
        if event.event_type is not PipelineEventType.INVOCATION_ENDED:
            return None
        status = _payload_get(event, "status")
        if status != "failed":
            return None
        invocation_id = _payload_get(event, "invocation_id") or "<unknown>"
        return AlertOutcome(
            primary_entity=str(invocation_id),
            context={"invocation_id": invocation_id, "status": status},
        )


@dataclass(frozen=True, slots=True)
class MonitorWebsocketDisconnectedCondition:
    """Fire when the monitor reports a websocket disconnect.

    Triggered by a ``websocket_disconnected`` SSE frame; primary
    entity is the venue so a re-disconnect on the *same* venue
    debounces. A reconnect followed by a fresh disconnect refires
    after the debounce window.
    """

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del state
        if not isinstance(event, MonitorEvent):
            return None
        if event.event_type is not MonitorEventType.WEBSOCKET_DISCONNECTED:
            return None
        venue = _payload_get(event, "venue") or "<unknown>"
        return AlertOutcome(
            primary_entity=str(venue),
            context={"venue": venue},
        )


@dataclass(frozen=True, slots=True)
class AgentMalformedOutputCondition:
    """Fire when a pipeline agent retries due to malformed output.

    Triggered by an ``agent_retrying`` SSE frame whose ``reason``
    field is ``"malformed_output"``. Primary entity is the agent
    name so re-retries for the same agent within the window
    debounce; a malformed-output retry on a different agent refires.
    """

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del state
        if not isinstance(event, PipelineEvent):
            return None
        if event.event_type is not PipelineEventType.AGENT_RETRYING:
            return None
        if _payload_get(event, "reason") != "malformed_output":
            return None
        agent_name = _payload_get(event, "agent_name") or "<unknown>"
        return AlertOutcome(
            primary_entity=str(agent_name),
            context={
                "agent_name": agent_name,
                "invocation_id": _payload_get(event, "invocation_id"),
            },
        )


# ---------------------------------------------------------------------------
# State-polling predicates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarginCallCondition:
    """Fire when a ``MARGIN_CALL`` activity-log row arrived in the last hour.

    Primary entity is the affected ``account_id`` so re-calls on
    the same account debounce. The engine pre-fetches the hour
    window; this predicate filters the batch by event_type.
    """

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del event
        rows = _activity_log_rows(state, event_types=("MARGIN_CALL",))
        if not rows:
            return None
        row = rows[0]
        account_id = row.get("account_id") or "<unknown>"
        return AlertOutcome(
            primary_entity=str(account_id),
            context={"account_id": account_id, "entry_id": row.get("entry_id")},
        )


@dataclass(frozen=True, slots=True)
class DrawdownTierCrossedCondition:
    """Fire when ``portfolio_summary.cumulative_drawdown`` crosses a tier.

    The first progressive tier is the default (5% drawdown); the
    operator can override via ``threshold``. The drawdown is
    negative-signed in the snapshot (a 7% drawdown reads as
    ``-0.07``); the predicate compares the absolute value against
    the threshold.
    """

    threshold: float = DEFAULT_DRAWDOWN_TIER_THRESHOLD

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del event
        summary = state.portfolio_summary
        if summary is None:
            return None
        drawdown = summary.get("cumulative_drawdown")
        if drawdown is None:
            return None
        if abs(float(drawdown)) < self.threshold:
            return None
        account_id = summary.get("account_id") or "<unknown>"
        return AlertOutcome(
            primary_entity=str(account_id),
            context={
                "account_id": account_id,
                "cumulative_drawdown": drawdown,
                "tier_threshold": self.threshold,
            },
        )


@dataclass(frozen=True, slots=True)
class HaltModeEnteredCondition:
    """Fire on a recent ``HALT_ACTIVATED`` activity-log row.

    Triggered by the activity_log emitter — the underlying halt-mode
    flag transition is recorded as a HALT_ACTIVATED row by the
    monitor's halt-mode path. Primary entity is the account so re-
    halt events on the same account debounce.
    """

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del event
        rows = _activity_log_rows(state, event_types=("HALT_ACTIVATED",))
        if not rows:
            return None
        row = rows[0]
        account_id = row.get("account_id") or "<unknown>"
        return AlertOutcome(
            primary_entity=str(account_id),
            context={"account_id": account_id, "entry_id": row.get("entry_id")},
        )


@dataclass(frozen=True, slots=True)
class HardBlockGuardrailRejectionCondition:
    """Fire on a recent ``GUARDRAIL_REJECTION`` row with zone ``hard_block``.

    Primary entity is the ``position_id`` so re-rejections on the
    same position debounce.
    """

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del event
        rows = _activity_log_rows(state, event_types=("GUARDRAIL_REJECTION",))
        for row in rows:
            detail = row.get("detail", {})
            if isinstance(detail, Mapping) and detail.get("zone") == "hard_block":
                position_id = row.get("position_id") or "<unknown>"
                return AlertOutcome(
                    primary_entity=str(position_id),
                    context={
                        "position_id": position_id,
                        "entry_id": row.get("entry_id"),
                    },
                )
        return None


@dataclass(frozen=True, slots=True)
class DataDirectoryDiskPressureCondition:
    """Fire when the data-directory on-disk size exceeds the threshold.

    Threshold defaults to 50 GB per the design doc. The size is
    sourced from the :class:`AlertEvaluatorState` snapshot's
    ``data_directory_size_bytes`` field; the engine populates it
    from the configured data-directory before calling into
    predicates.
    """

    threshold_bytes: int = DEFAULT_DATA_DIRECTORY_THRESHOLD_BYTES

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del event
        size = state.data_directory_size_bytes
        if size < self.threshold_bytes:
            return None
        return AlertOutcome(
            primary_entity="data_directory",
            context={
                "size_bytes": size,
                "threshold_bytes": self.threshold_bytes,
            },
        )


@dataclass(frozen=True, slots=True)
class ProfileBoundaryCrossedCondition:
    """Fire when total_equity falls outside the active profile's range.

    Primary entity is the active profile name so re-crossings for
    the same profile debounce; a profile switch followed by a fresh
    crossing on the new profile refires immediately.
    """

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del event
        summary = state.portfolio_summary
        if summary is None:
            return None
        total_equity = summary.get("total_equity")
        min_usd = summary.get("active_profile_min_usd")
        max_usd = summary.get("active_profile_max_usd")
        profile = summary.get("active_profile") or "<unknown>"
        if total_equity is None or min_usd is None or max_usd is None:
            return None
        if float(min_usd) <= float(total_equity) <= float(max_usd):
            return None
        direction = (
            "downgrade_candidate"
            if float(total_equity) < float(min_usd)
            else "graduation_candidate"
        )
        return AlertOutcome(
            primary_entity=str(profile),
            context={
                "active_profile": profile,
                "total_equity": total_equity,
                "min_usd": min_usd,
                "max_usd": max_usd,
                "direction": direction,
            },
        )


@dataclass(frozen=True, slots=True)
class CommandAbandonedCondition:
    """Fire on a recent ``COMMAND_ABANDONED`` activity-log row.

    Primary entity is the entry_id so each abandonment fires once
    (no natural debounce key — every COMMAND_ABANDONED row is a
    distinct event).
    """

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del event
        rows = _activity_log_rows(state, event_types=("COMMAND_ABANDONED",))
        if not rows:
            return None
        row = rows[0]
        entry_id = row.get("entry_id") or "<unknown>"
        return AlertOutcome(
            primary_entity=str(entry_id),
            context={"entry_id": entry_id},
        )


@dataclass(frozen=True, slots=True)
class ThesisResolvedCondition:
    """Fire on a recent ``THESIS_RESOLVED`` activity-log row.

    Operational severity — purely informational for feedback-loop
    tracking. Primary entity is the thesis_id so a re-resolution
    of the same thesis (rare; should not happen) debounces.
    """

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del event
        rows = _activity_log_rows(state, event_types=("THESIS_RESOLVED",))
        if not rows:
            return None
        row = rows[0]
        thesis_id = row.get("thesis_id") or "<unknown>"
        return AlertOutcome(
            primary_entity=str(thesis_id),
            context={"thesis_id": thesis_id, "entry_id": row.get("entry_id")},
        )


# ---------------------------------------------------------------------------
# Dormant predicates — upstream emit point does not yet exist.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DormantCondition:
    """Base shape for dormant predicates (no upstream emit yet).

    Each subclass pins a ``MISSING_UPSTREAM`` class attribute naming
    the upstream signal that, once it exists, lets the predicate be
    promoted to a real implementation. The engine logs the missing
    upstream once at startup; the predicate then returns ``None`` on
    every evaluation.
    """

    MISSING_UPSTREAM: ClassVar[str] = "<override in subclass>"

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None:
        del event, state
        return None


@dataclass(frozen=True, slots=True)
class CriticalApiFailureCondition(_DormantCondition):
    """**Dormant.** Q1/Q6/Q8 data category failed in most recent invocation.

    Upstream emit point: a per-category status field on the
    ``invocations`` row (or a sibling categorical-failure event)
    that the distillation layer would populate. Not yet in place;
    predicate stays dormant.
    """

    MISSING_UPSTREAM: ClassVar[str] = (
        "invocations.{category}_status field per Q1/Q6/Q8 category — "
        "see docs/design/02-distillation-layer/"
    )


@dataclass(frozen=True, slots=True)
class ImportantApiFailureCondition(_DormantCondition):
    """**Dormant.** Q2/Q3/Q5/Q12/Qual 1/4/5/6 categorical failure.

    Same upstream gap as :class:`CriticalApiFailureCondition`.
    """

    MISSING_UPSTREAM: ClassVar[str] = (
        "invocations.{category}_status field per Q2/Q3/Q5/Q12/Qual 1/4/5/6 — "
        "see docs/design/02-distillation-layer/"
    )


@dataclass(frozen=True, slots=True)
class ScheduleMissCriticalCondition(_DormantCondition):
    """**Dormant.** Scheduled trigger for a Critical-tier category fires but
    no ``collection_runs`` row starts within five minutes.

    Upstream emit point: a "scheduled but unstarted" signal from
    the scheduler layer joined against ``collection_runs``. Not yet
    a discrete signal.
    """

    MISSING_UPSTREAM: ClassVar[str] = (
        "scheduler 'trigger fired without collection_runs row' event — "
        "see docs/design/01-data-layer/"
    )


@dataclass(frozen=True, slots=True)
class ScheduleMissCondition(_DormantCondition):
    """**Dormant.** A scheduled trigger fires but no invocation runs within 5m.

    Same upstream gap as :class:`ScheduleMissCriticalCondition`,
    one tier softer (Important rather than Critical).
    """

    MISSING_UPSTREAM: ClassVar[str] = (
        "scheduler 'trigger fired without invocation' event — see docs/design/01-data-layer/"
    )


@dataclass(frozen=True, slots=True)
class RegimeJumpCondition(_DormantCondition):
    """**Dormant.** Regime classification skips a level (e.g. normal→crisis).

    Upstream emit point: a "regime-jump detected" signal from the
    regime-detection layer. Currently the analyst layer produces
    only the latest regime; a level-skip detector is not in place.
    """

    MISSING_UPSTREAM: ClassVar[str] = (
        "regime-jump detector emitting 'regime level skip' — see docs/design/03-analysis-layer/"
    )


@dataclass(frozen=True, slots=True)
class OptionalDataCategorySkippedCondition(_DormantCondition):
    """**Dormant.** Q4/Qual 2/Qual 3 skipped by failure handler.

    Same upstream gap as the categorical-failure predicates — the
    failure-handler skip is not a discrete activity-log event today.
    """

    MISSING_UPSTREAM: ClassVar[str] = (
        "failure-handler 'category skipped' event for Q4/Qual 2/Qual 3 — "
        "see docs/design/llm-agent-failure-handling.md"
    )


# ---------------------------------------------------------------------------
# Public registry — 17 default conditions paired with their rule_name.
# ---------------------------------------------------------------------------


ALL_DEFAULT_CONDITIONS: tuple[tuple[str, AlertCondition], ...] = (
    ("pipeline_aborted", PipelineAbortedCondition()),
    ("critical_api_failure", CriticalApiFailureCondition()),
    ("schedule_miss_critical", ScheduleMissCriticalCondition()),
    ("monitor_websocket_disconnected", MonitorWebsocketDisconnectedCondition()),
    ("margin_call", MarginCallCondition()),
    ("drawdown_tier_crossed", DrawdownTierCrossedCondition()),
    ("halt_mode_entered", HaltModeEnteredCondition()),
    ("important_api_failure", ImportantApiFailureCondition()),
    ("hard_block_guardrail_rejection", HardBlockGuardrailRejectionCondition()),
    ("regime_jump", RegimeJumpCondition()),
    ("agent_malformed_output", AgentMalformedOutputCondition()),
    ("schedule_miss", ScheduleMissCondition()),
    ("data_directory_disk_pressure", DataDirectoryDiskPressureCondition()),
    ("profile_boundary_crossed", ProfileBoundaryCrossedCondition()),
    ("optional_data_category_skipped", OptionalDataCategorySkippedCondition()),
    ("command_abandoned", CommandAbandonedCondition()),
    ("thesis_resolved", ThesisResolvedCondition()),
)


def log_dormant_predicates(conditions: tuple[AlertCondition, ...]) -> None:
    """Emit one WARNING per dormant condition naming the missing upstream.

    The engine calls this at startup so the operator-log audit trail
    carries a permanent record of which predicates are not yet
    backed by a real upstream signal. The WARNING text names the
    missing emit point so the operator can wire it once the
    distillation / scheduler / analyst layer ships the signal.

    Dormant conditions are detected structurally — every subclass of
    :class:`_DormantCondition` carries a ``MISSING_UPSTREAM`` class
    attribute. Non-dormant conditions are not subclasses and so are
    not logged.
    """
    for condition in conditions:
        if isinstance(condition, _DormantCondition):
            log.warning(
                "alert predicate %s is dormant; upstream not yet wired: %s",
                type(condition).__name__,
                condition.MISSING_UPSTREAM,
            )
