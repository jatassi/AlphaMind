"""Alert rule / condition / outcome typed value objects (ALP-671).

Three frozen-dataclass + Protocol value objects shape the rule
vocabulary:

* :class:`AlertSeverity` — re-exported from
  :mod:`alphamind.command_center.persistence.codecs` so the alert
  engine and the persistence layer share the same StrEnum identity.
  The vocabulary (``critical`` / ``important`` / ``operational``) is
  the design doc's three-tier table.
* :class:`AlertOutcome` — the value a fired condition returns. Carries
  the ``primary_entity`` (debounce key — e.g. the failing
  invocation_id, the breached position_id, the affected account) and
  the ``context`` payload (encoded into the
  ``alerts.context_json`` column).
* :class:`AlertCondition` — Protocol every per-rule predicate
  satisfies. Each predicate consumes an optional event + a
  state-access handle and returns an :class:`AlertOutcome` when the
  rule's underlying condition becomes true, or ``None`` otherwise.
* :class:`AlertRule` — bundles the name, severity tier, debounce
  window, channel list, and condition into the typed handle the
  engine iterates over.

Per the ALP-128 architectural invariants the dataclasses are frozen +
slotted so the engine's internal collaborators cannot mutate a rule's
shape mid-iteration; the Protocol stays structural so test stubs and
the 17 production predicates satisfy it without inheritance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from alphamind.command_center._kernel.ids import AlertRuleName
from alphamind.command_center.persistence.codecs import AlertSeverity

__all__ = [
    "AlertCondition",
    "AlertEvaluatorState",
    "AlertOutcome",
    "AlertRule",
    "AlertSeverity",
]


@dataclass(frozen=True, slots=True)
class AlertOutcome:
    """A predicate firing — primary entity + context payload.

    Returned by :meth:`AlertCondition.evaluate` when the rule's
    underlying condition becomes true. The engine uses
    ``primary_entity`` as the second half of the debounce key
    (``(rule_name, primary_entity)``) so e.g. a
    :class:`HardBlockGuardrailRejectionCondition` debounces per-position
    rather than globally. The ``context`` dict is JSON-encoded into
    the ``alerts.context_json`` column for the dashboard's
    click-through rendering.
    """

    primary_entity: str
    context: Mapping[str, Any]


class AlertEvaluatorState(Protocol):
    """Handle the engine threads into state-polling predicates.

    The engine constructs one of these against the cc's
    ``foreign_reader`` factory + a clock; predicates that need to
    query the production tables (margin call activity, drawdown tier,
    on-disk data-directory size, etc.) call into the handle's
    methods. The Protocol keeps the predicate impls free of direct
    session-factory imports.

    Per :doc:`docs/design/command-center.md` § Persistence boundary
    the alert engine reads foreign state via this handle and writes
    only the ``alerts`` table via the cc_writer factory; the handle
    intentionally does not expose a write surface.
    """

    async def fetch_recent_activity_log_rows(
        self,
        *,
        event_types: tuple[str, ...],
        within: timedelta,
    ) -> tuple[Mapping[str, Any], ...]:
        """Return ``activity_log`` rows of the given types within the window.

        Used by predicates that watch for activity-log-driven
        conditions (margin call, guardrail rejection, command
        abandoned, thesis resolved, profile boundary crossed).
        """

    async def fetch_latest_portfolio_summary(self) -> Mapping[str, Any] | None:
        """Return the most recent ``portfolio_summary`` row or ``None``.

        Used by the drawdown-tier + profile-boundary predicates.
        """

    async def fetch_latest_invocation(self) -> Mapping[str, Any] | None:
        """Return the most recent ``invocations`` row or ``None``.

        Used by the pipeline-aborted + critical-API-failure +
        important-API-failure + schedule-miss predicates.
        """


@runtime_checkable
class AlertCondition(Protocol):
    """Per-rule predicate the engine evaluates on each tick.

    Each of the 17 default rules ships a concrete implementation in
    :mod:`alphamind.command_center.alerts.conditions`. The Protocol
    is structural so test stubs and production predicates satisfy it
    without a base class.

    Parameters
    ----------
    event:
        The :class:`CombinedEvent` that triggered evaluation
        (multiplexer-driven path) or ``None`` (60s timer path).
        Event-driven predicates return ``None`` when ``event is None``;
        state-polling predicates return ``None`` when the event is
        not relevant to them.
    state:
        The :class:`AlertEvaluatorState` handle for state-polling
        predicates. Event-driven predicates ignore it.

    Returns
    -------
    AlertOutcome | None:
        An outcome when the condition fires; ``None`` otherwise.
    """

    def evaluate(
        self,
        *,
        event: Any | None,
        state: AlertEvaluatorState,
    ) -> AlertOutcome | None: ...


@dataclass(frozen=True, slots=True)
class AlertRule:
    """One row in the rule registry.

    The engine iterates these on every event + every 60s timer tick.
    Per-rule ``debounce_window`` suppresses re-fires within the
    window (keyed by ``(rule_name, primary_entity)``); the
    ``channels`` tuple drives the per-fire fanout.

    Fields
    ------
    name:
        Stable identifier; written verbatim to the
        ``alerts.rule_name`` column and visible in the operator's
        dashboard list view.
    severity:
        Drives the in-app banner color + the Discord-webhook
        eligibility (only ``critical`` and ``important`` post to
        Discord per the design doc's Notification channels table).
    debounce_window:
        Minimum time between re-fires of the same ``(rule_name,
        primary_entity)`` pair. The design doc's default rule set
        pins this per rule; ``config/alerts.yaml`` lets the
        operator override.
    channels:
        Subset of the channel registry vocabulary the engine
        fans the fire out to. Production values:
        ``("in_app", "discord")`` for critical+important;
        ``("in_app",)`` for operational.
    condition:
        The per-rule :class:`AlertCondition` predicate. The 17
        defaults are constructed at startup from the YAML; an
        operator-added rule registers a new predicate via the future
        rule-registry editor (story 06b).
    """

    name: AlertRuleName
    severity: AlertSeverity
    debounce_window: timedelta
    channels: tuple[str, ...]
    condition: AlertCondition
