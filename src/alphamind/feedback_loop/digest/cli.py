"""Headless analytics-read CLI for the feedback-loop digest (ALP-888).

The imperative shell (P1) over the pure :mod:`.generator` core: opens a session, calls
:func:`~alphamind.feedback_loop.dataset.load_window` once per trailing week to build the
``(week_label, WindowDataset)`` sequence, loads the digest / feedback config, runs the
pure generator, and emits JSON to stdout. This is the surface the ``/feedback-review``
skill drives.

Subcommands::

    python -m alphamind.feedback_loop.digest.cli digest --week <ISO-date>
    python -m alphamind.feedback_loop.digest.cli metric <metric_id> --window <window>
    python -m alphamind.feedback_loop.digest.cli metrics-list

* ``digest`` — emit the :class:`~alphamind.feedback_loop.generator.WeeklyDigest` for the
  week containing ``--week`` (defaults to the current week) as JSON. Loads the trailing
  ``--trajectory-weeks`` weeks (default 12) so the trajectory and shift baselines are
  populated.
* ``metric`` — compute one metric by id over the trailing ``--window`` weeks and emit the
  :class:`~alphamind.feedback_loop.metrics.types.MetricResult` as JSON. Unknown id → exit 1.
* ``metrics-list`` — emit the registered metric ids (and their classification) as JSON.

Exit codes: ``0`` success, ``1`` argument / lookup error, ``2`` infrastructure error.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphamind.feedback_loop.digest.generator import WeekInput, generate_digest
from alphamind.feedback_loop.metrics import get_metric, list_metrics
from alphamind.feedback_loop.metrics.types import UNCONDITIONED, MetricId

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from alphamind.config.models.digest import DigestConfig
    from alphamind.config.models.feedback import FeedbackLoopConfig
    from alphamind.feedback_loop.metrics.types import ComputeFn, MetricResult

__all__ = ["main"]

log = logging.getLogger(__name__)

#: Default number of trailing weeks to load for the trajectory sparklines and shift
#: baselines (the design's 8-12-week trajectory; 12 covers the longest baseline span).
_DEFAULT_TRAJECTORY_WEEKS = 12

_DAYS_PER_WEEK = 7


# ---------------------------------------------------------------------------
# Week-range helpers (pure)
# ---------------------------------------------------------------------------


def _week_monday(reference: date) -> date:
    """The Monday on or before *reference* — the canonical week start."""
    return reference - timedelta(days=reference.weekday())


def _week_bounds(monday: date) -> tuple[datetime, datetime]:
    """The ``[start, end)`` UTC datetimes for the week beginning *monday*."""
    start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)
    return start, start + timedelta(days=_DAYS_PER_WEEK)


def _trailing_weeks(current_monday: date, count: int) -> list[date]:
    """The *count* week-Mondays ending at *current_monday*, oldest-first."""
    return [current_monday - timedelta(weeks=offset) for offset in range(count - 1, -1, -1)]


# ---------------------------------------------------------------------------
# JSON serialisation (pure)
# ---------------------------------------------------------------------------


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert a frozen-dataclass digest tree into JSON-native values.

    Dataclasses become dicts (slots-friendly via :func:`dataclasses.fields`); tuples
    and lists become lists; datetimes become ISO strings; everything else is passed
    through. ``property`` attributes are not serialised — only declared fields.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (tuple, list)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _metric_result_json(result: MetricResult) -> dict[str, Any]:
    """Render a :class:`MetricResult` as a JSON-native dict."""
    band = (
        None
        if result.posterior_band is None
        else {"lower": result.posterior_band.lower, "upper": result.posterior_band.upper}
    )
    return {
        "metric_id": str(result.metric_id),
        "value": result.value,
        "posterior_band": band,
        "sample_size": result.sample_size,
        "insufficient_sample": result.insufficient_sample,
    }


# ---------------------------------------------------------------------------
# Config loading (shell)
# ---------------------------------------------------------------------------


def _load_configs(config_dir: Path) -> tuple[DigestConfig, FeedbackLoopConfig]:
    """Load ``digest.yaml`` + ``feedback.yaml`` from *config_dir*."""
    from alphamind.config.loaders import read_yaml_file
    from alphamind.config.models.digest import DigestConfig
    from alphamind.config.models.feedback import FeedbackLoopConfig

    digest = DigestConfig.model_validate(read_yaml_file(config_dir / "digest.yaml"))
    feedback = FeedbackLoopConfig.model_validate(read_yaml_file(config_dir / "feedback.yaml"))
    return digest, feedback


# ---------------------------------------------------------------------------
# Window loading (shell)
# ---------------------------------------------------------------------------


async def _load_week_inputs(
    session: AsyncSession,
    mondays: list[date],
    feedback: FeedbackLoopConfig,
) -> list[WeekInput]:
    """Call ``load_window`` once per week-Monday, building the generator's input."""
    from alphamind.feedback_loop.dataset import load_window

    weeks: list[WeekInput] = []
    for monday in mondays:
        start, end = _week_bounds(monday)
        dataset = await load_window(session, start, end, config=feedback)
        weeks.append((monday.isoformat(), dataset))
    return weeks


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m alphamind.feedback_loop.digest.cli",
        description="Headless analytics-read CLI for the weekly feedback-loop digest.",
    )
    parser.add_argument(
        "--config-dir",
        metavar="PATH",
        default="config/",
        help="Config cascade root (default: config/).",
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        default=None,
        help="Explicit SQLite path; falls back to the configured production path when omitted.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    digest_p = sub.add_parser("digest", help="Emit the WeeklyDigest as JSON.")
    digest_p.add_argument(
        "--week",
        metavar="ISO-DATE",
        default=None,
        help="Any date in the target week (YYYY-MM-DD). Defaults to the current week.",
    )
    digest_p.add_argument(
        "--trajectory-weeks",
        type=int,
        default=_DEFAULT_TRAJECTORY_WEEKS,
        metavar="N",
        help=(
            "Trailing weeks to load for trajectory/baselines "
            f"(default: {_DEFAULT_TRAJECTORY_WEEKS})."
        ),
    )

    metric_p = sub.add_parser("metric", help="Emit one MetricResult (by id) as JSON.")
    metric_p.add_argument("metric_id", help="The registered metric id to compute.")
    metric_p.add_argument(
        "--window",
        type=int,
        default=1,
        metavar="WEEKS",
        help="Trailing weeks to aggregate the metric over (default: 1).",
    )
    metric_p.add_argument(
        "--week",
        metavar="ISO-DATE",
        default=None,
        help="Any date in the target end-week (YYYY-MM-DD). Defaults to the current week.",
    )

    sub.add_parser("metrics-list", help="Emit the registered metric ids as JSON.")
    return parser


def _resolve_current_monday(week_arg: str | None) -> date | None:
    """Resolve ``--week`` to its week-Monday, or ``None`` on a parse error."""
    if week_arg is None:
        return _week_monday(datetime.now(UTC).date())
    try:
        return _week_monday(date.fromisoformat(week_arg))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _run_metrics_list() -> int:
    payload = [
        {
            "metric_id": str(metric.metric_id),
            "po_type": metric.po_type,
            "default_window": metric.default_window.value,
        }
        for metric in list_metrics()
    ]
    print(json.dumps(payload, indent=2))
    return 0


def _run_digest(args: argparse.Namespace) -> int:
    current_monday = _resolve_current_monday(args.week)
    if current_monday is None:
        print(f"--week: {args.week!r} is not a valid ISO date (YYYY-MM-DD).")
        return 1
    if args.trajectory_weeks < 1:
        print("--trajectory-weeks must be >= 1.")
        return 1

    try:
        digest_config, feedback_config = _load_configs(Path(args.config_dir))
    except Exception:
        log.exception("Configuration load failed")
        return 2

    mondays = _trailing_weeks(current_monday, args.trajectory_weeks)
    try:
        weeks = _open_and_load(args.db_path, mondays, feedback_config)
    except Exception:
        log.exception("Window load failed")
        return 2

    digest = generate_digest(weeks, digest_config)
    print(json.dumps(_to_jsonable(digest), indent=2))
    return 0


def _run_metric(args: argparse.Namespace) -> int:
    metric = get_metric(MetricId(args.metric_id))
    if metric is None:
        print(f"unknown metric id: {args.metric_id!r}")
        return 1
    current_monday = _resolve_current_monday(args.week)
    if current_monday is None:
        print(f"--week: {args.week!r} is not a valid ISO date (YYYY-MM-DD).")
        return 1
    if args.window < 1:
        print("--window must be >= 1.")
        return 1

    try:
        _, feedback_config = _load_configs(Path(args.config_dir))
    except Exception:
        log.exception("Configuration load failed")
        return 2

    # Aggregate over the trailing --window weeks ending at the target week: one
    # contiguous window, not a sequence, so the metric sees the whole span.
    oldest = current_monday - timedelta(weeks=args.window - 1)
    start, _ = _week_bounds(oldest)
    _, end = _week_bounds(current_monday)
    try:
        result = _open_and_compute(args.db_path, start, end, feedback_config, metric.compute)
    except Exception:
        log.exception("Metric computation failed")
        return 2

    print(json.dumps(_metric_result_json(result), indent=2))
    return 0


# ---------------------------------------------------------------------------
# Session orchestration (shell)
# ---------------------------------------------------------------------------


def _open_and_load(
    db_path: str | None, mondays: list[date], feedback: FeedbackLoopConfig
) -> list[WeekInput]:
    """Open an async session and build the per-week input sequence."""

    async def _run() -> list[WeekInput]:
        from alphamind.persistence.session import make_async_engine, make_async_session_factory

        engine = make_async_engine(db_path)
        try:
            factory = make_async_session_factory(engine)
            async with factory() as session:
                return await _load_week_inputs(session, mondays, feedback)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _open_and_compute(
    db_path: str | None,
    start: datetime,
    end: datetime,
    feedback: FeedbackLoopConfig,
    compute: ComputeFn,
) -> MetricResult:
    """Open an async session, load one contiguous window, and compute the metric."""

    async def _run() -> MetricResult:
        from alphamind.feedback_loop.dataset import load_window
        from alphamind.persistence.session import make_async_engine, make_async_session_factory

        engine = make_async_engine(db_path)
        try:
            factory = make_async_session_factory(engine)
            async with factory() as session:
                dataset = await load_window(session, start, end, config=feedback)
                return compute(dataset, UNCONDITIONED)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the exit code (0 success, 1 arg error, 2 runtime)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args = _build_parser().parse_args(argv)

    if args.db_path is None:
        log.warning(
            "No --db-path provided; falling back to the configured production database path."
        )

    if args.command == "metrics-list":
        return _run_metrics_list()
    if args.command == "digest":
        return _run_digest(args)
    if args.command == "metric":
        return _run_metric(args)
    # argparse's required=True subparsers guarantee one of the above.
    return 1


if __name__ == "__main__":
    sys.exit(main())
