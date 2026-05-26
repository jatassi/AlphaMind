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
* :class:`AlertEvaluatorState` — frozen snapshot the engine pre-fetches
  (recent activity-log rows, latest portfolio summary, latest
  invocation, on-disk data-directory size) + passes to every
  predicate. Plain values; predicates stay pure / synchronous (P1
  functional-core).
* :class:`AlertCondition` — Protocol every per-rule predicate
  satisfies. Each predicate consumes an optional event + the snapshot
  and returns an :class:`AlertOutcome` when the rule's underlying
  condition becomes true, or ``None`` otherwise.
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
from dataclasses import dataclass, field
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


@dataclass(frozen=True, slots=True)
class AlertEvaluatorState:
    """Frozen snapshot the engine threads into every predicate.

    Pre-fetched by the engine on each evaluation tick (either an
    event arrival or the 60s periodic timer); predicates read from
    the snapshot synchronously and stay pure. Per the
    python-architecture P1 functional-core principle: the I/O lives
    in the engine's pre-fetch path; the predicates are total
    functions over plain values.

    Per :doc:`docs/design/command-center.md` § Persistence boundary
    the snapshot reflects foreign-table state via the cc
    ``foreign_reader`` factory. The engine's writes (the ``alerts``
    table) go through the cc_writer factory, not through this
    snapshot.

    Fields default to empty / None so tests can construct a
    minimal snapshot with only the fields the predicate under test
    consumes.
    """

    recent_activity_log_rows: tuple[Mapping[str, Any], ...] = ()
    portfolio_summary: Mapping[str, Any] | None = None
    latest_invocation: Mapping[str, Any] | None = None
    data_directory_size_bytes: int = 0
    extras: Mapping[str, Any] = field(default_factory=dict)


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
        The :class:`AlertEvaluatorState` snapshot for state-polling
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
