"""Schedule-collision invariant for ``config/scheduler.yaml`` (ALP-745).

Encodes "ALP-745 cannot recur" as a durable guard: the deliberative
pipeline's cron triggers must never fire two distinct runs in the same
minute. The ``as_of`` is second-resolution (``%Y-%m-%dT%H:%M:%SZ``) derived
from the fire time, and cron fires always land on the ``:00`` second — so two
triggers in the same minute produce an identical ``as_of`` and collide on
``distillation_ticker_baseline``'s ``UNIQUE(ticker, baseline_kind, as_of)``,
hard-crashing one invocation in distillation.

The original prod failure was the 15:30-ET pair: ``market_hours_rolling``
(``30 9,11,13,15``) included hour 15, and ``pre_close`` (``30 15``) anchored
the same minute. The Tier B schedule gives every trigger a distinct minute
(and >= the dedup lookback window between any two), so the collision is
structurally unreachable on the scheduled path.

These tests parse every cron expression in the shipped ``scheduler.yaml``,
enumerate fire times across a representative week, and assert the no-shared-
minute / dedup-clearance invariants. A deliberately-colliding fixture proves
the detection has teeth.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast
from zoneinfo import ZoneInfo

import yaml
from apscheduler.triggers.cron import CronTrigger

from alphamind.config.models.scheduler import SchedulerConfig

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"

# A representative week anchored on a Monday in June 2026 — comfortably
# inside US Eastern Daylight Time (DST flips happen in March / November), so
# the enumeration is free of fall-back / spring-forward wall-clock skew.
# Stored as (year, month, day) and materialized tz-aware inside the
# enumerator so the module constant isn't a naive ``datetime``.
_WEEK_START_DATE = (2026, 6, 1)
_WEEK_DAYS = 7
# A week of any sane cron fires far fewer than this; exceeding it means the
# enumeration is not converging (e.g., a fixed-`now` floor bug).
_MAX_FIRES_PER_WEEK = 1000


def _enumerate_week_fires(triggers: dict[str, str], *, tz_name: str) -> list[tuple[datetime, str]]:
    """Return every ``(fire_time, trigger_key)`` across one representative week.

    Fire times are produced by the same ``CronTrigger.from_crontab`` the
    driver registers, so this enumeration matches what APScheduler would
    actually schedule.
    """
    tz = ZoneInfo(tz_name)
    week_start = datetime(*_WEEK_START_DATE, tzinfo=tz)
    week_end = week_start + timedelta(days=_WEEK_DAYS)
    fires: list[tuple[datetime, str]] = []
    for trigger_key, cron_expression in triggers.items():
        trigger = CronTrigger.from_crontab(cron_expression, timezone=tz_name)
        previous: datetime | None = None
        cursor = week_start
        # APScheduler's get_next_fire_time uses min(now, previous+1us) as the
        # search floor, so `now` must advance with `previous` — a fixed `now`
        # in the past makes it return the same first fire forever. The cap is
        # a belt-and-suspenders guard: a week of any sane cron yields far
        # fewer than this many fires, so hitting it means a malformed schedule.
        for _ in range(_MAX_FIRES_PER_WEEK):
            fire = trigger.get_next_fire_time(previous, cursor)
            if fire is None or fire >= week_end:
                break
            fires.append((fire, trigger_key))
            previous = fire
            cursor = fire
        else:  # pragma: no cover - safety net for a pathological cron
            raise AssertionError(
                f"trigger {trigger_key!r} ({cron_expression!r}) produced more than "
                f"{_MAX_FIRES_PER_WEEK} fires in a week — enumeration did not converge"
            )
    return fires


def _same_minute_collisions(
    fires: list[tuple[datetime, str]],
) -> list[tuple[str, str, datetime]]:
    """Return ``(key_a, key_b, minute)`` for every pair sharing a minute.

    Grouping by minute mirrors the collision condition: cron fires land on
    the ``:00`` second, so two fires in the same minute share the identical
    second-resolution ``as_of`` that produced the prod ``UNIQUE`` collision.
    """
    by_minute: dict[datetime, list[str]] = {}
    for fire, key in fires:
        minute = fire.replace(second=0, microsecond=0)
        by_minute.setdefault(minute, []).append(key)
    collisions: list[tuple[str, str, datetime]] = []
    for minute, keys in by_minute.items():
        if len(keys) > 1:
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    collisions.append((keys[i], keys[j], minute))
    return collisions


def _within_window_pairs(
    fires: list[tuple[datetime, str]], *, window_minutes: int
) -> list[tuple[str, str, float]]:
    """Return cross-trigger fire pairs closer than ``window_minutes`` apart.

    Two *different* triggers firing inside the dedup lookback of each other
    re-creates the failure mode the window was meant to handle (one fire
    eating or being eaten by the other). Same-trigger fires are excluded —
    a trigger is allowed to fire on its own cadence.
    """
    window = timedelta(minutes=window_minutes)
    close: list[tuple[str, str, float]] = []
    for i in range(len(fires)):
        for j in range(i + 1, len(fires)):
            (fire_a, key_a), (fire_b, key_b) = fires[i], fires[j]
            if key_a == key_b:
                continue
            gap = abs(fire_a - fire_b)
            if gap < window:
                close.append((key_a, key_b, gap.total_seconds() / 60.0))
    return close


def _load_shipped_config() -> SchedulerConfig:
    raw = cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / "scheduler.yaml").read_text()))
    return SchedulerConfig.model_validate(raw)


class TestShippedScheduleHasNoCollisions:
    """The shipped Tier B ``scheduler.yaml`` is collision-free."""

    def test_no_two_triggers_share_a_minute(self) -> None:
        config = _load_shipped_config()
        fires = _enumerate_week_fires(config.triggers, tz_name=config.timezone)
        assert fires, "expected the shipped schedule to fire at least once in a week"
        collisions = _same_minute_collisions(fires)
        assert collisions == [], f"triggers share a minute (ALP-745 regression): {collisions}"

    def test_no_two_triggers_within_dedup_window(self) -> None:
        config = _load_shipped_config()
        fires = _enumerate_week_fires(config.triggers, tz_name=config.timezone)
        close = _within_window_pairs(fires, window_minutes=config.overlap_dedup_lookback_minutes)
        assert close == [], (
            "distinct triggers fire within the dedup lookback window "
            f"({config.overlap_dedup_lookback_minutes} min): {close}"
        )

    def test_exactly_the_four_tier_b_triggers_are_scheduled(self) -> None:
        config = _load_shipped_config()
        assert set(config.triggers) == {
            "market_open",
            "market_hours_rolling",
            "pre_close",
            "weekend_sunday",
        }
        # off_hours_rolling / weekend_saturday remain valid run types but
        # are deliberately unscheduled (continuous monitor owns that risk).
        assert "off_hours_rolling" not in config.triggers
        assert "weekend_saturday" not in config.triggers


class TestCollisionDetectionHasTeeth:
    """A deliberately-colliding fixture must trip the same-minute detector.

    This is the AC's "would fail if any two triggers were made to coincide"
    guard — it pins the *pre-ALP-745* schedule (``market_hours_rolling``
    sweeping 09:30/11:30/13:30/15:30 plus ``pre_close`` at 15:30) and proves
    the invariant catches the shared 15:30 minute.
    """

    _COLLIDING_TRIGGERS: ClassVar[dict[str, str]] = {
        "market_hours_rolling": "30 9,11,13,15 * * mon-fri",
        "pre_close": "30 15 * * mon-fri",
        "market_open": "0 9 * * mon-fri",
    }

    def test_colliding_schedule_is_flagged(self) -> None:
        fires = _enumerate_week_fires(self._COLLIDING_TRIGGERS, tz_name="US/Eastern")
        collisions = _same_minute_collisions(fires)
        assert collisions, "expected the 15:30 market_hours_rolling/pre_close pair to collide"
        colliding_keys = {frozenset((a, b)) for a, b, _ in collisions}
        assert frozenset(("market_hours_rolling", "pre_close")) in colliding_keys

    def test_colliding_schedule_collisions_land_on_the_close_minute(self) -> None:
        fires = _enumerate_week_fires(self._COLLIDING_TRIGGERS, tz_name="US/Eastern")
        collisions = _same_minute_collisions(fires)
        # Every flagged collision is the 15:30 ET close slot.
        assert all(minute.hour == 15 and minute.minute == 30 for _, _, minute in collisions)
