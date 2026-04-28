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
