"""AlertEngine — long-running task that drives the alert rule registry.

Story 05a / ALP-671. The engine:

1. Subscribes to the :class:`EventMultiplexer` for every published
   pipeline / monitor / cc event. On each event, the engine evaluates
   every registered :class:`AlertRule` against the event.
2. On a 60-second periodic timer, re-evaluates every state-polling
   rule against the foreign-reader session. The timer + event flows
   share one evaluator (:meth:`_evaluate_all`) so a state-polling rule
   isn't constrained to wait for the next 60s tick — a relevant event
   re-evaluates immediately.
3. Debounces re-fires per ``(rule_name, debounce_key)`` for the rule's
   ``debounce_window``: two fires inside the window collapse to one
   persisted row.
4. On fire: writes the row via :func:`insert_fired` + dispatches to
   each rule's configured channels (``in_app`` always first; ``discord``
   when configured).

Per the ALP-128 invariants:

* The engine's :meth:`run` is registered on the supervisor's outer
  ``asyncio.TaskGroup`` — no bare ``asyncio.create_task`` calls
  internally. The subscription + the 60s timer cooperate via an
  ``asyncio.Event`` shutdown flag; the engine returns cleanly on
  cancellation.
* Pydantic at boundaries only — internal state is frozen dataclasses.
* Channels are Protocols — production wiring uses the Real
  implementations; tests inject Fake.

Single deep module per P9: the operator-facing API surface is just
:meth:`run` + :meth:`evaluate_once` (the latter for tests). The
debounce table, channel fanout, and persistence orchestration are all
private to the engine.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.ids import AlertId, AlertRuleName
from alphamind.command_center.alerts.channels.discord import DiscordChannel
from alphamind.command_center.alerts.channels.in_app import InAppChannel
from alphamind.command_center.alerts.conditions import DormantCondition
from alphamind.command_center.alerts.persistence import insert_fired
from alphamind.command_center.alerts.rules import (
    AlertConditionResult,
    AlertEvaluatorState,
    AlertRule,
    AlertSeverity,
)
from alphamind.command_center.events.multiplexer import (
    CombinedEvent,
    EventMultiplexer,
)

__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "AlertEngine",
    "EvaluationOutcome",
]

log = logging.getLogger(__name__)


DEFAULT_POLL_INTERVAL_SECONDS: Final = 60.0
"""Engine timer interval — matches the parent issue's pre-resolved F
("60s fallback alongside SSE primary")."""


_DEFAULT_DEEP_LINK_TEMPLATE = "/#/alerts/{alert_id}"
"""Default click-through URL on the operator browser.

The hash-routed SPA at ``cc://#/alerts/<id>`` is the design's alert-
detail surface. Configured via the engine's ``deep_link_template``
kwarg so a future host change (post remote-access) lands here without
re-deploying.
"""


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    """The result of evaluating one rule on one tick.

    Surfaces for tests + the periodic-timer's per-tick log line. Carries
    enough state for the test harness to assert "the engine evaluated
    rule X and it fired" without touching the DB.
    """

    rule_name: AlertRuleName
    fired: bool
    debounce_key: str = ""
    suppressed_by_debounce: bool = False
    alert_id_: AlertId | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class _DebounceEntry:
    """Per-fire tracking entry — the engine's debounce table."""

    last_fired_at: datetime


class AlertEngine:
    """Long-running task evaluating + dispatching the alert rule registry.

    Construction wires the dependencies; :meth:`run` is the supervisor-
    registered task. The engine treats:

    * ``cc_writer_factory`` as the alerts-table writer (the engine
      writes ``alerts`` rows).
    * ``foreign_reader_factory`` as the read-only foreign-table reader
      every state-polling predicate consults (optional — when not
      provided, state-polling predicates return ``fired=False``).
    * ``multiplexer`` as the SSE subscription source AND the in-app
      channel's publish target.
    * ``discord_channel`` as the secondary fanout target.
    * ``rules`` as the registered :class:`AlertRule` list.
    """

    def __init__(
        self,
        *,
        rules: Sequence[AlertRule],
        multiplexer: EventMultiplexer,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        discord_channel: DiscordChannel,
        foreign_reader_factory: async_sessionmaker[AsyncSession] | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        deep_link_template: str = _DEFAULT_DEEP_LINK_TEMPLATE,
        clock: Any = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            msg = "poll_interval_seconds must be positive"
            raise ValueError(msg)
        if not rules:
            msg = "rules must be non-empty"
            raise ValueError(msg)
        self._rules = tuple(rules)
        self._multiplexer = multiplexer
        self._cc_writer_factory = cc_writer_factory
        self._foreign_reader_factory = foreign_reader_factory
        self._discord_channel = discord_channel
        self._in_app_channel = InAppChannel(multiplexer=multiplexer)
        self._poll_interval = poll_interval_seconds
        self._deep_link_template = deep_link_template
        self._clock = clock if clock is not None else _default_clock
        # Two TaskGroup tasks (event consumer + state poller) drive
        # :meth:`_evaluate_one` concurrently. The debounce table's
        # check-and-write must be atomic against the other task — without
        # the lock both tasks could suspend at ``await rule.condition.evaluate``,
        # then race past the debounce guard and double-fire. The lock
        # only covers the read+check+write critical section; the actual
        # dispatch (Discord post / SSE publish) is intentionally held
        # outside so its latency doesn't serialize other rules.
        self._debounce: dict[tuple[AlertRuleName, str], _DebounceEntry] = {}
        self._debounce_lock = asyncio.Lock()
        # Track the per-rule debounce window so eviction is parameterised
        # without re-scanning the rules tuple on every call.
        self._debounce_windows: dict[AlertRuleName, timedelta] = {
            rule.name: rule.debounce_window for rule in self._rules
        }
        self._stop_event: asyncio.Event | None = None

    @property
    def rules(self) -> tuple[AlertRule, ...]:
        return self._rules

    def request_stop(self) -> None:
        """Signal :meth:`run` to begin orderly shutdown."""
        if self._stop_event is None:
            log.warning("AlertEngine.request_stop called before run()")
            return
        self._stop_event.set()

    def log_dormant_startup_warnings(self) -> None:
        """Emit one WARNING per registered dormant rule.

        Called at :meth:`run` startup so the operator's boot log carries
        the one-time inventory of "these rules never fire until upstream
        emit X exists". Non-dormant rules are silent.
        """
        for rule in self._rules:
            condition = rule.condition
            if isinstance(condition, DormantCondition):
                condition.log_startup_warning()
                continue
            log_method = getattr(condition, "log_startup_warning", None)
            if log_method is None:
                continue
            log_method()

    async def run(self) -> None:
        """Subscribe + run the 60s timer until stop.

        Two cooperating async loops:

        * Event consumer: ``async for event in multiplexer.subscribe()``
          → evaluate every rule against the event.
        * Periodic timer: every ``poll_interval_seconds`` →
          :meth:`evaluate_state_polling`.

        Cancellation: the supervisor delivers ``CancelledError`` to
        :meth:`run`; the consumer + timer both honor the
        cancellation and return cleanly. The engine returns from
        :meth:`run` once both sub-coroutines exit.
        """
        self._stop_event = asyncio.Event()
        self.log_dormant_startup_warnings()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._consume_events(), name="alerts_event_consumer")
            tg.create_task(self._poll_state(), name="alerts_state_poller")

    async def _consume_events(self) -> None:
        """Subscribe to the multiplexer + evaluate every event."""
        if self._stop_event is None:  # pragma: no cover — set in run()
            return
        async with self._multiplexer.subscribe() as queue:
            while not self._stop_event.is_set():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    return
                await self._evaluate_all(event=event)

    async def _poll_state(self) -> None:
        """Run the periodic timer; evaluate all rules each tick."""
        if self._stop_event is None:  # pragma: no cover — set in run()
            return
        while not self._stop_event.is_set():
            stopped = await self._sleep_until_tick_or_stop()
            if stopped:
                return
            await self._evaluate_all(event=None)

    async def _sleep_until_tick_or_stop(self) -> bool:
        """Sleep one poll interval OR until stop signalled.

        Returns ``True`` if the stop event fired or the coroutine was
        cancelled (i.e. caller should exit); ``False`` on a normal
        tick timeout.
        """
        if self._stop_event is None:  # pragma: no cover — set in run()
            return True
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
        except TimeoutError:
            return False
        except asyncio.CancelledError:
            return True
        return True

    async def evaluate_once(
        self,
        *,
        event: CombinedEvent | None = None,
    ) -> tuple[EvaluationOutcome, ...]:
        """Evaluate every rule against one tick — test entry point.

        Production code does not call this directly; the supervisor
        registers :meth:`run`. Tests use this to exercise the
        evaluator + debounce + persistence + channel fanout without
        depending on the multiplexer + timer plumbing.
        """
        return await self._evaluate_all(event=event)

    async def _evaluate_all(
        self,
        *,
        event: CombinedEvent | None,
    ) -> tuple[EvaluationOutcome, ...]:
        """Evaluate every rule against one event tick OR one timer tick."""
        now = self._clock()
        state = AlertEvaluatorState(
            now=now,
            foreign_reader_factory=self._foreign_reader_factory,
        )
        outcomes: list[EvaluationOutcome] = []
        for rule in self._rules:
            outcome = await self._evaluate_one(rule, event=event, state=state, now=now)
            outcomes.append(outcome)
        return tuple(outcomes)

    async def _evaluate_one(
        self,
        rule: AlertRule,
        *,
        event: CombinedEvent | None,
        state: AlertEvaluatorState,
        now: datetime,
    ) -> EvaluationOutcome:
        try:
            result = await rule.condition.evaluate(event=event, state=state)
        except Exception as exc:
            log.warning(
                "alert engine: condition %r raised %s; treating as non-firing",
                str(rule.name),
                exc,
            )
            return EvaluationOutcome(rule_name=rule.name, fired=False)
        if not result.fired:
            return EvaluationOutcome(rule_name=rule.name, fired=False)
        key = (rule.name, result.debounce_key)
        async with self._debounce_lock:
            # Opportunistic eviction — drop expired entries belonging to
            # this rule so PipelineAbortedCondition (one entry per
            # invocation_id) doesn't grow without bound. Bounding the
            # walk to entries with the same rule name keeps the lock
            # critical section O(distinct debounce keys for this rule).
            self._prune_expired_for_rule(rule_name=rule.name, now=now)
            last = self._debounce.get(key)
            if last is not None and (now - last.last_fired_at) < rule.debounce_window:
                return EvaluationOutcome(
                    rule_name=rule.name,
                    fired=True,
                    debounce_key=result.debounce_key,
                    suppressed_by_debounce=True,
                    context=result.context,
                )
            self._debounce[key] = _DebounceEntry(last_fired_at=now)
        # Dispatch outside the lock — Discord posts and SSE publishes
        # can race safely on the multiplexer / httpx client; serializing
        # them under the debounce lock would back-pressure unrelated
        # rules.
        new_id = await self._persist_and_dispatch(rule, result=result, now=now)
        return EvaluationOutcome(
            rule_name=rule.name,
            fired=True,
            debounce_key=result.debounce_key,
            alert_id_=new_id,
            context=result.context,
        )

    def _prune_expired_for_rule(
        self,
        *,
        rule_name: AlertRuleName,
        now: datetime,
    ) -> None:
        """Drop debounce entries for *rule_name* whose window has passed.

        Caller holds :attr:`_debounce_lock`. The walk is restricted to
        entries belonging to one rule per call so the critical section
        stays bounded — PipelineAbortedCondition's per-invocation
        debounce key (one entry per ~200 invocations/day across 17 rules)
        would otherwise accumulate monotonically.
        """
        window = self._debounce_windows.get(rule_name)
        if window is None:
            return
        expired: list[tuple[AlertRuleName, str]] = [
            key
            for key, entry in self._debounce.items()
            if key[0] == rule_name and (now - entry.last_fired_at) >= window
        ]
        for key in expired:
            del self._debounce[key]

    async def _persist_and_dispatch(
        self,
        rule: AlertRule,
        *,
        result: AlertConditionResult,
        now: datetime,
    ) -> AlertId:
        import json

        context_json = json.dumps(dict(result.context), sort_keys=True)
        new_id = await insert_fired(
            self._cc_writer_factory,
            rule_name=rule.name,
            severity=rule.severity,
            context_json=context_json,
            fired_at=now,
        )
        if "in_app" in rule.channels:
            try:
                await self._in_app_channel.send(
                    alert_id_=new_id,
                    rule_name=rule.name,
                    severity=rule.severity,
                    context=result.context,
                )
            except Exception as exc:
                log.warning(
                    "alert engine: in_app channel raised %s for rule %r",
                    exc,
                    str(rule.name),
                )
        if "discord" in rule.channels:
            try:
                await self._discord_channel.send(
                    alert_id_=new_id,
                    rule_name=rule.name,
                    severity=rule.severity,
                    title=_format_title(rule.name, rule.severity),
                    context=result.context,
                    deep_link_url=self._deep_link_template.format(alert_id=new_id),
                )
            except Exception as exc:
                log.warning(
                    "alert engine: discord channel raised %s for rule %r",
                    exc,
                    str(rule.name),
                )
        return new_id


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _format_title(rule_name: AlertRuleName, severity: AlertSeverity) -> str:
    return f"[{severity.value.upper()}] {rule_name!s}"


# Silence the unused-symbol warning for the timedelta re-export.
_ = timedelta
