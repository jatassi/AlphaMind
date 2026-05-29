"""Frozen dataclasses for the alert rule + condition model (story 05a / ALP-671).

Three primitives:

* :class:`AlertSeverity` — re-exported from
  :mod:`alphamind.command_center.persistence.codecs` so the StrEnum
  vocabulary stays in one place (story 02 owns the persistence-layer
  enum; story 05a consumes it).
* :class:`AlertCondition` — :class:`typing.Protocol` the engine calls
  to decide whether a rule should fire. The Protocol carries a single
  :meth:`evaluate` method that returns an :class:`AlertConditionResult`
  bundle (fire-or-not plus a debounce-keying-handle plus the context
  payload the persistence row carries).
* :class:`AlertRule` — frozen dataclass tying a condition to a severity,
  debounce window, channel list, and a stable rule name. The default
  rules land in :mod:`.conditions` and bind
  via :func:`build_default_rules`.

Per ALP-128 invariants:

* Frozen dataclasses internal — no Pydantic on these types. The Pydantic
  boundary is the YAML loader in :mod:`alphamind.command_center.config`
  (story 02) plus the route-layer request / response envelopes
  (story 05a's :mod:`.routes`).
* The condition Protocol is structural so the engine can register both
  event-driven predicates (consumed straight off the multiplexer's
  publish callback) and state-polling predicates (re-evaluated by the
  engine's 60s timer against a foreign-reader session) without the
  engine knowing which kind it holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from alphamind.command_center._kernel.ids import AlertRuleName
from alphamind.command_center.events.multiplexer import CombinedEvent
from alphamind.command_center.persistence.codecs import AlertSeverity

__all__ = [
    "AlertCondition",
    "AlertConditionResult",
    "AlertEvaluatorState",
    "AlertRule",
    "AlertSeverity",
]


@dataclass(frozen=True, slots=True)
class AlertEvaluatorState:
    """Read-only handle the engine threads into every condition evaluation.

    The state object is the per-evaluation context: the wall-clock now,
    plus a placeholder for the foreign-reader session factory the
    state-polling predicates query (DB-bound predicates pull from
    ``portfolio_summary``, ``activity_log``, etc.).

    The session factory is typed ``object | None`` so the rules module
    stays free of any SQLAlchemy import — predicates that need DB
    access cast the value at use site to the
    ``async_sessionmaker[AsyncSession]`` they expect. Predicates that
    don't need DB access ignore the field.

    The ``now`` field is a tz-aware ``datetime`` the engine threads in
    from its single clock source so the 60s-timer tick + every
    event-driven evaluation share the same reading.
    """

    now: Any  # tz-aware datetime; Any to avoid datetime import at use sites
    foreign_reader_factory: object | None = None


@dataclass(frozen=True, slots=True)
class AlertConditionResult:
    """The output of :meth:`AlertCondition.evaluate`.

    Three fields:

    * ``fired`` — Boolean: should the engine record a fire?
    * ``debounce_key`` — Identifier for the per-fire debounce bucket. A
      condition that fires once per `(rule, primary_entity)` (e.g.
      ``hard_block_guardrail_rejection`` debounced per position) returns
      the entity identifier; a condition that debounces per rule only
      (no secondary keying) returns the empty string. Treated opaquely
      by the engine and combined with the rule name to form the full
      debounce-tracking key.
    * ``context`` — JSON-serializable mapping committed to the persisted
      :class:`AlertRow`'s ``context_json`` column. The dashboard's
      banner reads this back to render the click-through details.
    """

    fired: bool
    debounce_key: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize ``context`` to an immutable :class:`MappingProxyType`.

        Mirrors the kernel events' payload-freezing discipline
        (:class:`alphamind.command_center._kernel.events.PipelineEvent`):
        the engine fans the same :class:`AlertConditionResult` to the
        persistence write + the in-app channel; an inadvertent mutation
        in one consumer would otherwise corrupt the other's view.
        """
        if isinstance(self.context, MappingProxyType):
            return
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))


@runtime_checkable
class AlertCondition(Protocol):
    """Predicate the engine evaluates per rule.

    A condition has two trigger shapes — event-driven and state-polling —
    and both are exposed through a single :meth:`evaluate` method so the
    engine doesn't have to know which kind it holds:

    * **Event-driven.** ``event`` is the :class:`CombinedEvent` the
      multiplexer just published; ``state`` carries the wall-clock now
      + the foreign-reader session factory for any cross-reference reads
      the predicate needs. The engine invokes ``evaluate`` synchronously
      against every subscribed event.
    * **State-polling.** ``event`` is ``None``; the engine's 60s timer
      ticked. ``state`` carries the same now + factory. The predicate
      queries the foreign tables (e.g. ``portfolio_summary``) and returns
      its verdict.

    The Protocol is :func:`typing.runtime_checkable` so test fakes that
    duck-type the method shape pass ``isinstance`` checks (story 02 used
    the same convention on :class:`WebauthnVerifier`).

    The condition does NOT carry its own ``name`` — the rule does. Tying
    the predicate to a rule name happens at :class:`AlertRule`
    construction, not at predicate construction; this lets a single
    predicate class be reused under multiple rule names (the dormant
    placeholders use this).
    """

    async def evaluate(
        self,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
    ) -> AlertConditionResult:
        """Return the fire-or-not verdict for one engine tick."""


@dataclass(frozen=True, slots=True)
class AlertRule:
    """One row in the alert rule registry (per design doc § Alert rules).

    Fields:

    * ``name`` — the :class:`AlertRuleName` the registry + persistence
      row carries.
    * ``severity`` — the :class:`AlertSeverity` tier driving channel
      routing (critical + important → Discord + in-app; operational →
      in-app only).
    * ``debounce_window`` — re-fire suppression window. An event firing
      twice inside this window produces ONE persisted alert.
    * ``channels`` — the channel-name tuple the engine fans to on fire.
      Two channel names are recognized in this story: ``"in_app"`` and
      ``"discord"``; the design doc reserves the registry shape for
      future expansion (email, SMS).
    * ``condition`` — the :class:`AlertCondition` the engine evaluates
      per tick.
    """

    name: AlertRuleName
    severity: AlertSeverity
    debounce_window: timedelta
    channels: tuple[str, ...]
    condition: AlertCondition
