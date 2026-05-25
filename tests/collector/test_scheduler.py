"""
Tests for alphamind.collector.scheduler and alphamind.collector.catchup.

All vendor collect_* functions are mocked — no real API calls or DB writes.
APScheduler jobs are registered but never started (no job fires).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_YAML_PATH = Path(__file__).parents[2] / "config" / "collector_schedule.yaml"


def _yaml_collector_keys() -> list[str]:
    with _YAML_PATH.open() as fh:
        data = yaml.safe_load(fh)
    return list(data["collectors"].keys())


# ---------------------------------------------------------------------------
# Slice 1 — COLLECTORS keys match collector_schedule.yaml
# ---------------------------------------------------------------------------


def test_collectors_keys_match_yaml() -> None:
    """COLLECTORS registry must have exactly the same keys as the YAML."""
    yaml_keys = _yaml_collector_keys()

    from alphamind.collector.scheduler import COLLECTORS

    assert sorted(COLLECTORS.keys()) == sorted(yaml_keys), (
        f"COLLECTORS keys {sorted(COLLECTORS.keys())} != YAML keys {sorted(yaml_keys)}"
    )


# ---------------------------------------------------------------------------
# Slice 2 — build_scheduler creates one executor per vendor
# ---------------------------------------------------------------------------


def test_build_scheduler_creates_one_executor_per_vendor() -> None:
    from alphamind.collector.scheduler import build_scheduler

    sched = build_scheduler()
    executor_names = set(sched._executors.keys())  # APScheduler 3.x internal
    expected_vendors = {
        "polygon",
        "fred",
        "eia",
        "bls",
        "treasury",
        "finnhub",
        "marketaux",
        "sec_edgar",
        "polymarket",
        "kalshi",
    }
    assert expected_vendors.issubset(executor_names), (
        f"Missing executors: {expected_vendors - executor_names}"
    )


# ---------------------------------------------------------------------------
# Slice 3 — register_jobs registers one job per YAML entry
# ---------------------------------------------------------------------------


def test_register_jobs_registers_one_job_per_yaml_entry() -> None:
    from alphamind.collector.scheduler import build_scheduler, register_jobs

    sched = build_scheduler()
    register_jobs(sched)

    yaml_keys = _yaml_collector_keys()
    job_ids = {job.id for job in sched.get_jobs()}
    assert job_ids == set(yaml_keys), f"Registered job IDs {job_ids} != YAML keys {set(yaml_keys)}"


# ---------------------------------------------------------------------------
# Slice 4 — every job has correct executor (vendor prefix) and max_instances=1
# ---------------------------------------------------------------------------


def test_every_job_has_vendor_executor_and_max_instances_1() -> None:
    from alphamind.collector.scheduler import build_scheduler, register_jobs

    sched = build_scheduler()
    register_jobs(sched)

    for job in sched.get_jobs():
        # executor name is the vendor prefix of the job id
        vendor = job.id.split(".")[0]
        assert job.executor == vendor, (
            f"Job {job.id!r}: expected executor={vendor!r}, got {job.executor!r}"
        )
        assert job.max_instances == 1, (
            f"Job {job.id!r}: expected max_instances=1, got {job.max_instances}"
        )


# ---------------------------------------------------------------------------
# Slice 5 — finnhub.calendar wrapper calls all four calendar functions in order
# ---------------------------------------------------------------------------


def test_finnhub_calendar_wrapper_calls_all_four_in_order() -> None:
    call_order: list[str] = []

    def _make_side(name: str) -> Callable[..., None]:
        def _fn(*args: object, **kwargs: object) -> None:
            call_order.append(name)

        return _fn

    # Patch in the scheduler module's namespace (where the names are bound)
    with (
        patch(
            "alphamind.collector.scheduler.collect_earnings_calendar",
            side_effect=_make_side("earnings"),
        ),
        patch(
            "alphamind.collector.scheduler.collect_economic_calendar",
            side_effect=_make_side("economic"),
        ),
        patch(
            "alphamind.collector.scheduler.collect_ipo_calendar",
            side_effect=_make_side("ipo"),
        ),
        patch(
            "alphamind.collector.scheduler.collect_fda_calendar",
            side_effect=_make_side("fda"),
        ),
    ):
        from alphamind.collector.scheduler import _finnhub_calendar_fanout

        _finnhub_calendar_fanout()

    assert call_order == ["earnings", "economic", "ipo", "fda"], (
        f"Expected ordered calendar calls, got {call_order}"
    )


# ---------------------------------------------------------------------------
# Slice 6 — catchup.run_all calls every COLLECTOR with since=None
# ---------------------------------------------------------------------------


def test_catchup_run_all_calls_every_collector_with_since_none() -> None:
    from alphamind.collector.scheduler import COLLECTORS

    # Build a mock registry with the same keys
    mock_fns: dict[str, MagicMock] = {key: MagicMock() for key in COLLECTORS}

    with patch("alphamind.collector.catchup.COLLECTORS", mock_fns):
        from alphamind.collector.catchup import run_all

        run_all()

    for mock in mock_fns.values():
        mock.assert_called_once_with(since=None)


# ---------------------------------------------------------------------------
# Slice 7 — register_jobs sets coalesce + misfire_grace_time on every job
# ---------------------------------------------------------------------------


def test_every_job_has_coalesce_and_misfire_grace_time() -> None:
    """ALP-289: backlog-of-missed-fires hygiene applies to every collector."""
    from alphamind.collector.scheduler import build_scheduler, register_jobs

    sched = build_scheduler()
    register_jobs(sched)

    for job in sched.get_jobs():
        assert job.coalesce is True, f"Job {job.id!r}: expected coalesce=True"
        assert job.misfire_grace_time == 60, (
            f"Job {job.id!r}: expected misfire_grace_time=60, got {job.misfire_grace_time}"
        )


# ---------------------------------------------------------------------------
# Slice 8 — mark_orphan_runs flips stale 'running' rows to 'failed'
# ---------------------------------------------------------------------------


def test_mark_orphan_runs_marks_running_rows_as_failed() -> None:
    """ALP-289: a hard-killed scheduler leaves zombie 'running' rows; the
    next startup must reconcile them so observability stays honest."""
    from alphamind.collector.scheduler import mark_orphan_runs
    from alphamind.persistence.models import Base, CollectionRuns
    from alphamind.persistence.session import make_engine, make_session_factory

    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)

    with sf() as sess:
        sess.add(
            CollectionRuns(
                run_id="orphan",
                collector="polygon.options",
                started_at="2026-01-01T00:00:00+00:00",
                status="running",
            )
        )
        sess.add(
            CollectionRuns(
                run_id="ok",
                collector="polygon.equity",
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:05:00+00:00",
                status="success",
            )
        )
        sess.add(
            CollectionRuns(
                run_id="prior_fail",
                collector="finnhub.news",
                started_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:30+00:00",
                status="failed",
            )
        )
        sess.commit()

    count = mark_orphan_runs(session_factory=sf)
    assert count == 1

    with sf() as sess:
        rows = {r.run_id: r for r in sess.query(CollectionRuns).all()}

    orphan = rows["orphan"]
    assert orphan.status == "failed"
    assert orphan.error_summary == "orphaned by scheduler restart"
    assert orphan.completed_at is not None
    # Pre-existing terminal rows are untouched.
    assert rows["ok"].status == "success"
    assert rows["prior_fail"].status == "failed"
    assert rows["prior_fail"].error_summary is None


def test_mark_orphan_runs_returns_zero_when_no_orphans() -> None:
    from alphamind.collector.scheduler import mark_orphan_runs
    from alphamind.persistence.models import Base
    from alphamind.persistence.session import make_engine, make_session_factory

    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)

    assert mark_orphan_runs(session_factory=sf) == 0


# ---------------------------------------------------------------------------
# Slice 9 — derive_vendor_workers scales with the long-running collector set
# ---------------------------------------------------------------------------
#
# ALP-652: per-vendor max_workers is derived from the long-running collector
# set, not a hand-tuned literal. The current live config has one long-runner
# pair under `polygon` (equity + equity_offhrs, both 6-13 min `collect_universe_bars`
# fires); every other vendor has no long-runners and stays at the default 1.


def test_derive_vendor_workers_matches_live_registry() -> None:
    """polygon=2 against today's 5-collector polygon set; every other vendor=1."""
    from alphamind.collector.scheduler import (
        _LONG_RUNNING_COLLECTORS,
        COLLECTORS,
        _derive_vendor_workers,
    )

    workers = _derive_vendor_workers(COLLECTORS, _LONG_RUNNING_COLLECTORS)

    assert workers["polygon"] == 2, (
        f"polygon should derive 2 workers (1 long-runner pair); got {workers['polygon']}"
    )
    for vendor in (
        "fred",
        "eia",
        "bls",
        "treasury",
        "finnhub",
        "marketaux",
        "sec_edgar",
        "polymarket",
        "kalshi",
        "finra",
        "iborrowdesk",
        "news",
    ):
        assert workers[vendor] == 1, (
            f"vendor {vendor!r} has no long-runners, should derive 1 worker; got {workers[vendor]}"
        )


def test_derive_vendor_workers_scales_with_added_long_runner() -> None:
    """Adding a sixth long-running polygon collector bumps the derived count to >=3."""
    from alphamind.collector.scheduler import _derive_vendor_workers

    def _stub() -> None:
        return None

    synthetic_collectors: dict[str, Callable[..., object]] = {
        "polygon.equity": _stub,
        "polygon.equity_offhrs": _stub,
        "polygon.options": _stub,
        "polygon.corporate_actions": _stub,
        "polygon.reference": _stub,
        "polygon.new_long_runner": _stub,  # hypothetical sixth, also long-running
        "fred.macro": _stub,
    }
    synthetic_long_running = frozenset(
        {"polygon.equity", "polygon.equity_offhrs", "polygon.new_long_runner"}
    )

    workers = _derive_vendor_workers(synthetic_collectors, synthetic_long_running)

    assert workers["polygon"] >= 3, (
        f"polygon with 3 long-runners should derive >=3 workers; got {workers['polygon']}"
    )
    assert workers["fred"] == 1


def test_long_running_collectors_subset_of_registry() -> None:
    """Guard against typos: every long-running ID must exist in COLLECTORS."""
    from alphamind.collector.scheduler import _LONG_RUNNING_COLLECTORS, COLLECTORS

    unknown = _LONG_RUNNING_COLLECTORS - COLLECTORS.keys()
    assert not unknown, f"_LONG_RUNNING_COLLECTORS references unknown collector IDs: {unknown}"


def test_build_scheduler_polygon_executor_has_two_workers() -> None:
    """Behavior-preserving check against the prior literal `_VENDOR_WORKERS['polygon']=2`."""
    from alphamind.collector.scheduler import build_scheduler

    sched = build_scheduler()
    polygon_executor = sched._executors["polygon"]  # APScheduler 3.x internal
    # ThreadPoolExecutor stores its pool on _pool with _max_workers
    assert polygon_executor._pool._max_workers == 2, (
        f"polygon executor expected max_workers=2; got {polygon_executor._pool._max_workers}"
    )
