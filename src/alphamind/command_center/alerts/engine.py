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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.events import ConfigReloadRequiresRestartEvent
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
from alphamind.command_center.config import AlertsConfig
from alphamind.command_center.events.multiplexer import (
    CombinedEvent,
    EventMultiplexer,
)

__all__ = [
    "DEFAULT_CONFIG_WATCH_INTERVAL_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "AlertEngine",
    "EvaluationOutcome",
    "ReloadDiff",
]

log = logging.getLogger(__name__)


DEFAULT_POLL_INTERVAL_SECONDS: Final = 60.0
"""Engine timer interval — matches the parent issue's pre-resolved F
("60s fallback alongside SSE primary")."""


DEFAULT_CONFIG_WATCH_INTERVAL_SECONDS: Final = 5.0
"""Hot-reload watcher cadence (story 06b / ALP-683).

Polls ``config/alerts.yaml``'s mtime every 5 seconds. The watcher is a
peer task to :meth:`_consume_events` + :meth:`_poll_state` under the
engine's :class:`asyncio.TaskGroup` — no bare ``asyncio.create_task``.
Five seconds is short enough that an operator-edit-then-trigger flow
feels live, long enough that ``os.stat`` on a stable filesystem is
free."""


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


@dataclass(frozen=True, slots=True)
class ReloadDiff:
    """Outcome of one :meth:`AlertEngine._reload_rules_from_file` call.

    Surfaces for tests + the per-reload INFO log line so a developer can
    pin "the engine picked up *N* additions, *M* removals" from a
    deterministic in-process fake without scraping log output.

    Three name-buckets:

    * ``added`` — rule names present in the freshly-parsed config but
      not in the running engine; appended in the engine's order.
    * ``removed`` — rule names present in the running engine but no
      longer in the YAML; dropped along with their debounce entries.
    * ``updated`` — rule names present in both, but with at least one
      field (severity / debounce_window / channels) changed; replaced
      in place. Last-fired-at debounce state is preserved.

    ``non_rules_section_changed`` is ``True`` when the freshly-parsed
    :class:`AlertsConfig`'s non-rules section (currently
    :attr:`AlertsConfig.channels`) differs from the previously-loaded
    snapshot. The engine cannot hot-apply those edits — the Discord
    channel + multiplexer wiring is established at lifespan startup —
    so a ``ConfigReloadRequiresRestartEvent`` is published instead.
    """

    added: tuple[AlertRuleName, ...]
    removed: tuple[AlertRuleName, ...]
    updated: tuple[AlertRuleName, ...]
    non_rules_section_changed: bool


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

    def __init__(  # noqa: PLR0913 — composition root wires alerts dependencies + hot-reload knobs; collapsing under one kwargs bundle hides the contract.
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
        alerts_config_path: Path | None = None,
        rules_builder: Callable[[AlertsConfig], Sequence[AlertRule]] | None = None,
        initial_config: AlertsConfig | None = None,
        config_watch_interval_seconds: float = DEFAULT_CONFIG_WATCH_INTERVAL_SECONDS,
    ) -> None:
        if poll_interval_seconds <= 0:
            msg = "poll_interval_seconds must be positive"
            raise ValueError(msg)
        if config_watch_interval_seconds <= 0:
            msg = "config_watch_interval_seconds must be positive"
            raise ValueError(msg)
        if not rules:
            msg = "rules must be non-empty"
            raise ValueError(msg)
        # Hot-reload (story 06b / ALP-683) requires both the on-disk
        # YAML path AND a builder that re-renders rules from a fresh
        # :class:`AlertsConfig`. Either alone is incomplete; rather
        # than silently degrading to "no hot-reload" the constructor
        # demands both-or-neither so a wiring mistake fails loud.
        if (alerts_config_path is None) ^ (rules_builder is None):
            msg = "alerts_config_path and rules_builder must be wired together (both or neither)"
            raise ValueError(msg)
        # The list is mutated under :attr:`_rules_lock` by
        # :meth:`_reload_rules_from_file`; rule-set evaluation acquires
        # the same lock around the per-tick rule iteration so the
        # mutation is invisible to in-flight evaluation.
        self._rules: list[AlertRule] = list(rules)
        self._rules_lock = asyncio.Lock()
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
        # Hot-reload state.
        self._alerts_config_path = alerts_config_path
        self._rules_builder = rules_builder
        # Lazy-load the baseline ``AlertsConfig`` from disk when the
        # caller wired hot-reload (``alerts_config_path`` + ``rules_builder``)
        # but omitted ``initial_config`` (Wave-6 finding #15). Without
        # a baseline the first reload's :meth:`_non_rules_section_differs`
        # short-circuits to False and the channels-section change banner
        # is silently dropped. Loading at construction keeps the contract
        # explicit — production wires both, tests may rely on the lazy
        # path.
        self._previous_config: AlertsConfig | None = initial_config
        if (
            self._previous_config is None
            and alerts_config_path is not None
            and alerts_config_path.exists()
        ):
            # Import here so the module doesn't take a load-time
            # dependency on the loader chain.
            from alphamind.command_center.config import load_alerts_config

            self._previous_config = load_alerts_config(alerts_config_path.parent)
        self._config_watch_interval = config_watch_interval_seconds
        self._last_mtime_ns: int | None = (
            alerts_config_path.stat().st_mtime_ns
            if alerts_config_path is not None and alerts_config_path.exists()
            else None
        )

    @property
    def rules(self) -> tuple[AlertRule, ...]:
        # External read returns an immutable snapshot — the engine
        # mutates :attr:`_rules` in place under :attr:`_rules_lock`
        # during hot-reload; callers see a coherent view at the moment
        # they ask.
        return tuple(self._rules)

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
        """Subscribe + run the periodic timer + hot-reload watcher until stop.

        Cooperating async loops, all peers under one :class:`asyncio.TaskGroup`:

        * Event consumer: ``async for event in multiplexer.subscribe()``
          → evaluate every rule against the event.
        * Periodic timer: every ``poll_interval_seconds`` →
          :meth:`_evaluate_all`.
        * Hot-reload watcher (story 06b / ALP-683): every
          ``config_watch_interval_seconds`` → check
          ``config/alerts.yaml`` mtime; on change, re-parse + diff +
          apply per :meth:`_reload_rules_from_file`. Only spawned when
          :attr:`_alerts_config_path` + :attr:`_rules_builder` were
          both wired at construction.

        Cancellation: the supervisor delivers ``CancelledError`` to
        :meth:`run`; every loop honors the cancellation and returns
        cleanly. The engine returns from :meth:`run` once all sub-
        coroutines exit.
        """
        self._stop_event = asyncio.Event()
        self.log_dormant_startup_warnings()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._consume_events(), name="alerts_event_consumer")
            tg.create_task(self._poll_state(), name="alerts_state_poller")
            if self._alerts_config_path is not None and self._rules_builder is not None:
                tg.create_task(self._watch_config_mtime(), name="alerts_config_watcher")

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
        """Evaluate every rule against one event tick OR one timer tick.

        Snapshots ``self._rules`` under :attr:`_rules_lock` so a
        concurrent hot-reload (story 06b) doesn't mutate the iteration.
        Per-rule evaluation runs outside the snapshot lock — its own
        debounce critical section uses :attr:`_debounce_lock`. Holding
        the rules lock only for the snapshot keeps reload latency
        bounded.
        """
        now = self._clock()
        state = AlertEvaluatorState(
            now=now,
            foreign_reader_factory=self._foreign_reader_factory,
        )
        async with self._rules_lock:
            rules_snapshot: tuple[AlertRule, ...] = tuple(self._rules)
        outcomes: list[EvaluationOutcome] = []
        for rule in rules_snapshot:
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
            # Hot-reload race guard (Wave-6 finding #8): a concurrent
            # :meth:`_apply_rule_diff_locked` may have removed this rule
            # between the rules-snapshot at the top of
            # :meth:`_evaluate_all` and the debounce write below. The
            # removal also clears :attr:`_debounce_windows` for the rule
            # and prunes its existing debounce entries, but without this
            # guard the debounce-table write below would re-introduce
            # an orphan entry that :meth:`_prune_expired_for_rule`
            # cannot reap (its early-return on missing window leaves the
            # entry permanently stranded). Skip the write entirely when
            # the rule is no longer registered — the operator-visible
            # outcome (no persisted alert, no channel dispatch) is the
            # same as if the rule had been removed before the evaluate.
            if rule.name not in self._debounce_windows:
                return EvaluationOutcome(rule_name=rule.name, fired=False)
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

    # ------------------------------------------------------------------
    # Hot-reload (story 06b / ALP-683)
    # ------------------------------------------------------------------

    async def _watch_config_mtime(self) -> None:
        """Poll ``alerts.yaml`` mtime; trigger reload on change.

        Peer task to :meth:`_consume_events` + :meth:`_poll_state` under
        :meth:`run`'s :class:`asyncio.TaskGroup`. Sleeps
        ``config_watch_interval_seconds`` between polls; honors the
        shared :attr:`_stop_event` for orderly shutdown.

        Any exception raised by the reload (YAML parse error, rule
        builder failure, etc.) is logged and absorbed — the engine
        keeps running on the previous rule set rather than crashing
        the whole TaskGroup over an operator's malformed edit. A
        subsequent good edit drops the broken state.
        """
        if self._stop_event is None:  # pragma: no cover — set in run()
            return
        if self._alerts_config_path is None:  # pragma: no cover — gated in run()
            return
        while not self._stop_event.is_set():
            stopped = await self._sleep_until_config_check_or_stop()
            if stopped:
                return
            try:
                await self._reload_rules_from_file_if_changed()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "alert engine: alerts.yaml hot-reload raised %s; keeping existing rule set",
                    exc,
                )

    async def _sleep_until_config_check_or_stop(self) -> bool:
        """Sleep one watch interval OR until stop signalled."""
        if self._stop_event is None:  # pragma: no cover — set in run()
            return True
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=self._config_watch_interval,
            )
        except TimeoutError:
            return False
        except asyncio.CancelledError:
            return True
        return True

    async def _reload_rules_from_file_if_changed(self) -> ReloadDiff | None:
        """Reload alerts.yaml when mtime advances; return diff or ``None``.

        ``None`` is returned when the file's mtime is unchanged (no
        work to do) — the watcher uses the sentinel to skip the
        re-parse + diff overhead on every tick.
        """
        if self._alerts_config_path is None:
            return None
        try:
            mtime_ns = self._alerts_config_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        if self._last_mtime_ns is not None and mtime_ns == self._last_mtime_ns:
            return None
        self._last_mtime_ns = mtime_ns
        return await self._reload_rules_from_file()

    async def _reload_rules_from_file(self) -> ReloadDiff:
        """Re-parse alerts.yaml + diff against current rule set + apply.

        Steps:

        1. ``load_alerts_config`` re-parses the YAML (Pydantic ``extra='forbid'``
           catches typos).
        2. ``self._rules_builder`` renders the new
           :class:`AlertsConfig` into a fresh tuple of
           :class:`AlertRule`.
        3. Diff against ``self._rules`` by name → added / removed /
           updated / unchanged.
        4. Under :attr:`_rules_lock`: apply removals (drop from
           :attr:`_rules` + drop matching :attr:`_debounce` entries),
           apply additions, replace in-place for updates.
        5. If the non-rules section changed (``channels`` block), publish a
           :class:`ConfigReloadRequiresRestartEvent` on the multiplexer
           — the operator-facing banner surfaces the need for restart.

        Errors propagate to the caller; the watcher absorbs them so
        a malformed edit doesn't kill the engine.
        """
        if self._alerts_config_path is None or self._rules_builder is None:
            msg = "hot-reload requires alerts_config_path + rules_builder"
            raise RuntimeError(msg)
        # Import here so the engine module doesn't take a load-time
        # dependency on the loader (and through it, the alphamind.config
        # loader chain).
        from alphamind.command_center.config import load_alerts_config

        new_config = load_alerts_config(self._alerts_config_path.parent)
        new_rules = tuple(self._rules_builder(new_config))
        non_rules_changed = self._non_rules_section_differs(new_config)
        async with self._rules_lock:
            diff = self._apply_rule_diff_locked(new_rules)
        self._previous_config = new_config
        diff_full = ReloadDiff(
            added=diff.added,
            removed=diff.removed,
            updated=diff.updated,
            non_rules_section_changed=non_rules_changed,
        )
        if non_rules_changed:
            await self._multiplexer.publish(
                ConfigReloadRequiresRestartEvent(
                    filename=self._alerts_config_path.name,
                    reason="alerts.yaml channels section changed; restart required",
                ),
            )
        log.info(
            "alert engine: hot-reload diff added=%s removed=%s updated=%s non_rules=%s",
            diff_full.added,
            diff_full.removed,
            diff_full.updated,
            diff_full.non_rules_section_changed,
        )
        return diff_full

    def _non_rules_section_differs(self, new_config: AlertsConfig) -> bool:
        """Return ``True`` when the previous + new configs' channels differ."""
        if self._previous_config is None:
            # First load with no prior snapshot — engine was started
            # against ``new_config`` itself, so by definition nothing
            # changed.
            return False
        return self._previous_config.channels != new_config.channels

    def _apply_rule_diff_locked(self, new_rules: Sequence[AlertRule]) -> ReloadDiff:
        """Mutate :attr:`_rules` + :attr:`_debounce` to match *new_rules*.

        Caller holds :attr:`_rules_lock`. Returns the per-bucket name
        list so the watcher can publish + log the change set.
        """
        current_by_name: dict[AlertRuleName, AlertRule] = {r.name: r for r in self._rules}
        new_by_name: dict[AlertRuleName, AlertRule] = {r.name: r for r in new_rules}
        added: list[AlertRuleName] = []
        removed: list[AlertRuleName] = []
        updated: list[AlertRuleName] = []
        # Detect adds + updates.
        for name, new_rule in new_by_name.items():
            existing = current_by_name.get(name)
            if existing is None:
                added.append(name)
            elif _rule_tunables_differ(existing, new_rule):
                updated.append(name)
        # Detect removes.
        for name in current_by_name:
            if name not in new_by_name:
                removed.append(name)
        # Apply: preserve the engine's iteration order by rebuilding
        # :attr:`_rules` to match *new_rules* exactly.
        self._rules = list(new_rules)
        self._debounce_windows = {r.name: r.debounce_window for r in self._rules}
        # Drop debounce entries for removed rules — leaving them would
        # block re-additions from firing.
        if removed:
            removed_set = set(removed)
            keys_to_drop = [key for key in self._debounce if key[0] in removed_set]
            for key in keys_to_drop:
                del self._debounce[key]
        return ReloadDiff(
            added=tuple(added),
            removed=tuple(removed),
            updated=tuple(updated),
            non_rules_section_changed=False,  # caller fills the field
        )


def _rule_tunables_differ(left: AlertRule, right: AlertRule) -> bool:
    """Return True when two rules with the same name carry different tunables.

    Compares the YAML-overridable surface — severity, debounce window,
    channels. The condition predicate is rebuilt from scratch by
    :func:`build_default_rules` on every reload, so a different
    condition identity is expected and does not count as a change.
    """
    return (
        left.severity != right.severity
        or left.debounce_window != right.debounce_window
        or left.channels != right.channels
    )


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _format_title(rule_name: AlertRuleName, severity: AlertSeverity) -> str:
    return f"[{severity.value.upper()}] {rule_name!s}"


# Silence the unused-symbol warning for the timedelta re-export.
_ = timedelta
