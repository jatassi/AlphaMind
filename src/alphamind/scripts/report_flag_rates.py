"""Class A flag-rate empirical reporter (story 16 / ALP-96).

Reads every ``DISTILLATION_ANOMALY_FLAG`` activity-log entry over a trailing
window, groups by ``(threshold_class, threshold_key)``, and reports the
empirical firing rate, the calibration-state split, universe coverage, and a
structural universe-wide-silence signal. The operator compares the observed
rate against the design-doc expected rate
(``threshold-calibration.md`` § Static configuration thresholds) and decides
whether a threshold needs tuning — step 2 of the Class A review procedure.

The tool informs; it does not verdict or tune. There is no per-flag
expected-rate constant and no saturation verdict (operator decision at
refinement: empirical + silence only). Read-only; touches no pipeline path.

Exit code is 0 regardless of silence findings; non-zero is reserved for
script-internal errors (DB unreachable, corrupt fixture).

Usage:
  uv run python scripts/report_flag_rates.py \\
      [--window-days N] [--db-path PATH] [--threshold-class CLASS] \\
      [--ticker T] [--output text|json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind._kernel.calibration import CalibrationState
from alphamind.config.models.distillation import DistillationConfig
from alphamind.distillation.flag_event_types import (
    all_threshold_classes,
    flag_keys_for_class,
    resolve_flag_taxonomy,
)
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.events.activity_log import (
    DistillationAnomalyFlagDetail,
    EventType,
)
from alphamind.scripts._common import load_distillation_config, load_universe_scope
from alphamind.scripts._stdio import configure_utf8_stdio
from alphamind.state.invocation_context.activity_log import activity_log_entry_from_row
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow

DEFAULT_WINDOW_DAYS = 28
"""Trailing window default — a four-week view matching the Class A review
procedure's "2-4 weeks of paper-trading data" (``threshold-calibration.md``
section Update process, step 1)."""

ALL_CLASSES_CHOICE = "all"
"""``--threshold-class`` sentinel selecting every registered class."""

_STRUCTURAL_CONFIG_LABEL = "structural (no config knob)"
"""Shown in place of a configured value for the structural classes."""

_MARKET_WIDE_COVERAGE_LABEL = "market-wide n/a"
"""Shown for coverage on market-wide keys (every ``detail.ticker`` is ``None``)."""


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationSplit:
    """Per-key counts of the three calibration states."""

    calibrated: int
    accumulating: int
    unavailable: int


@dataclass(frozen=True)
class ThresholdKeyMetrics:
    """Empirical metrics for one ``(threshold_class, threshold_key)`` pair.

    ``coverage`` is ``None`` for market-wide keys (rendered as
    ``market-wide n/a`` / JSON ``null``); ``configured_value`` is ``None`` for
    structural keys (rendered as ``structural (no config knob)`` / JSON
    ``null``). ``is_market_wide`` carries the rate basis to the renderers.
    """

    threshold_key: str
    flag_count: int
    rate: float
    calibration_split: CalibrationSplit
    coverage: int | None
    is_market_wide: bool
    configured_value: float | int | None
    silence: bool


@dataclass(frozen=True)
class ThresholdClassReport:
    """One class section — its keys plus the tunability label."""

    threshold_class: str
    is_config_gated: bool
    keys: tuple[ThresholdKeyMetrics, ...]


@dataclass(frozen=True)
class FlagRateReport:
    """Top-level report shape consumed by both renderers."""

    window_start: datetime
    window_end: datetime
    window_days: int
    universe_size: int
    invocation_count: int
    ticker_filter: str | None
    classes: tuple[ThresholdClassReport, ...]


# ---------------------------------------------------------------------------
# Data load — sync, mirroring report_emergency_invocations.py
# ---------------------------------------------------------------------------


def _parse_iso(text: str) -> datetime:
    """Parse the ``Z``-suffixed ISO 8601 string the writers emit."""
    parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _load_window_invocation_ids(
    session: Session, *, now: datetime, window_days: int
) -> tuple[str, ...]:
    """Return the IDs of invocations whose ``start_at`` is in ``[now - window, now)``."""
    cutoff = now - timedelta(days=window_days)
    stmt = select(InvocationRow.invocation_id, InvocationRow.start_at)
    ids: list[str] = []
    for invocation_id, start_at in session.execute(stmt).all():
        parsed = _parse_iso(start_at)
        if cutoff <= parsed < now:
            ids.append(invocation_id)
    return tuple(ids)


def _load_anomaly_details(
    session: Session, invocation_ids: Sequence[str]
) -> tuple[DistillationAnomalyFlagDetail, ...]:
    """Load and rehydrate every in-window anomaly-flag detail payload.

    Sync ``select(ActivityLogRow)`` filtered to ``DISTILLATION_ANOMALY_FLAG``
    over the in-window invocation IDs, rehydrated via
    ``activity_log_entry_from_row`` so each ``entry.detail`` is a
    ``DistillationAnomalyFlagDetail`` (not the async analytics-spine reader).
    """
    if not invocation_ids:
        return ()
    stmt = select(ActivityLogRow).where(
        ActivityLogRow.event_type == EventType.DISTILLATION_ANOMALY_FLAG.value,
        ActivityLogRow.invocation_id.in_(invocation_ids),
    )
    details: list[DistillationAnomalyFlagDetail] = []
    for row in session.execute(stmt).scalars():
        detail = activity_log_entry_from_row(row).detail
        if isinstance(detail, DistillationAnomalyFlagDetail):
            details.append(detail)
    return tuple(details)


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------


def _calibration_split(details: Sequence[DistillationAnomalyFlagDetail]) -> CalibrationSplit:
    return CalibrationSplit(
        calibrated=sum(1 for d in details if d.calibration_state is CalibrationState.CALIBRATED),
        accumulating=sum(
            1 for d in details if d.calibration_state is CalibrationState.ACCUMULATING
        ),
        unavailable=sum(1 for d in details if d.calibration_state is CalibrationState.UNAVAILABLE),
    )


def _is_config_gated(config: DistillationConfig, threshold_class: str) -> bool:
    """A class is config-gated iff it names a section on the distillation config.

    Derived from the config object itself rather than a hand-maintained set, so a
    class reclassified config<->structural (or a new registry class) can't drift a
    second source of truth. The four gated classes (``anomaly_detection`` /
    ``narrative_lag`` / ``lead_lag`` / ``prediction_market``) are sections on
    :class:`DistillationConfig`; the three structural classes are not.
    """
    return hasattr(config, threshold_class)


def _configured_value(
    config: DistillationConfig, threshold_class: str, threshold_key: str
) -> float | int | None:
    """Resolve ``config.<class>.<key>`` for config-gated classes; ``None`` otherwise."""
    if not _is_config_gated(config, threshold_class):
        return None
    value: float | int = getattr(getattr(config, threshold_class), threshold_key)
    return value


@dataclass(frozen=True)
class _ReportContext:
    """Report-level invariants shared by every per-key computation."""

    config: DistillationConfig
    universe_size: int
    window_days: int
    invocation_count: int
    ticker: str | None


def _metrics_for_key(
    ctx: _ReportContext,
    *,
    threshold_class: str,
    threshold_key: str,
    all_details: Sequence[DistillationAnomalyFlagDetail],
) -> ThresholdKeyMetrics:
    """Compute one key's metrics.

    ``rate`` and ``silence`` are universe-wide signals — they describe the whole
    universe and are deliberately unaffected by ``--ticker`` (structured trigger
    #2 is *universe-wide* silence, not per-ticker silence). ``--ticker`` scopes
    only the drill-in observables ``flag_count``, ``coverage``, and the
    calibration split (spec E restricts "counts and coverage" to the ticker).

    Market-wide-ness is decided from the full observed set (every flag carries
    no ticker) so ``--ticker`` never reclassifies a ticker-bearing key.
    """
    is_market_wide = bool(all_details) and all(d.ticker is None for d in all_details)
    universe_count = len(all_details)

    if is_market_wide or ctx.ticker is None:
        scoped: list[DistillationAnomalyFlagDetail] = list(all_details)
    else:
        scoped = [d for d in all_details if d.ticker == ctx.ticker]

    if is_market_wide:
        coverage: int | None = None
        rate = universe_count / ctx.invocation_count if ctx.invocation_count else 0.0
    else:
        # If a key ever emits both ticker-bearing and ticker-None flags it is
        # ticker-bearing here (not every ticker is None). The None flags count
        # toward flag_count/rate (spec D: flag_count is the total) but not
        # coverage, which is distinct observed tickers by definition.
        coverage = len({d.ticker for d in scoped if d.ticker is not None})
        denominator = ctx.universe_size * ctx.window_days
        rate = universe_count / denominator if denominator else 0.0

    return ThresholdKeyMetrics(
        threshold_key=threshold_key,
        flag_count=len(scoped),
        rate=rate,
        calibration_split=_calibration_split(scoped),
        coverage=coverage,
        is_market_wide=is_market_wide,
        configured_value=_configured_value(ctx.config, threshold_class, threshold_key),
        silence=universe_count == 0 and ctx.invocation_count > 0,
    )


def compute_flag_rate_report(
    *,
    session: Session,
    config: DistillationConfig,
    universe_size: int,
    now: datetime,
    window_days: int,
    threshold_class: str = ALL_CLASSES_CHOICE,
    ticker: str | None = None,
) -> FlagRateReport:
    """Build the flag-rate report from ``invocations`` and ``activity_log`` rows."""
    window_start = now - timedelta(days=window_days)
    invocation_ids = _load_window_invocation_ids(session, now=now, window_days=window_days)
    invocation_count = len(invocation_ids)
    details = _load_anomaly_details(session, invocation_ids)

    grouped: dict[tuple[str, str], list[DistillationAnomalyFlagDetail]] = {}
    for detail in details:
        grouped.setdefault((detail.threshold_class, detail.threshold_key), []).append(detail)

    selected_classes = (
        sorted(all_threshold_classes())
        if threshold_class == ALL_CLASSES_CHOICE
        else [threshold_class]
    )
    ctx = _ReportContext(
        config=config,
        universe_size=universe_size,
        window_days=window_days,
        invocation_count=invocation_count,
        ticker=ticker,
    )

    class_reports: list[ThresholdClassReport] = []
    for cls in selected_classes:
        known_keys = sorted(
            {resolve_flag_taxonomy(prefix).threshold_key for prefix in flag_keys_for_class(cls)}
        )
        key_metrics = tuple(
            _metrics_for_key(
                ctx,
                threshold_class=cls,
                threshold_key=key,
                all_details=grouped.get((cls, key), []),
            )
            for key in known_keys
        )
        class_reports.append(
            ThresholdClassReport(
                threshold_class=cls,
                is_config_gated=_is_config_gated(config, cls),
                keys=key_metrics,
            )
        )

    return FlagRateReport(
        window_start=window_start,
        window_end=now,
        window_days=window_days,
        universe_size=universe_size,
        invocation_count=invocation_count,
        ticker_filter=ticker,
        classes=tuple(class_reports),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _rate_basis_label(metrics: ThresholdKeyMetrics) -> str:
    return "per invocation" if metrics.is_market_wide else "per ticker/day"


def _coverage_label(metrics: ThresholdKeyMetrics) -> str:
    if metrics.coverage is None:
        return _MARKET_WIDE_COVERAGE_LABEL
    return f"{metrics.coverage} tickers"


def _configured_value_label(metrics: ThresholdKeyMetrics) -> str:
    if metrics.configured_value is None:
        return _STRUCTURAL_CONFIG_LABEL
    return f"{metrics.configured_value}"


def _related_tools_footer() -> list[str]:
    return [
        "=== RELATED TOOLS ===",
        "  This report computes structured trigger #2 (universe-wide silence) directly",
        "  and supplies the empirical rates the operator reads for trigger #1",
        "  (single-class saturation). Emergency-trigger false positives (#3) live in",
        "  report_emergency_invocations.py; anomaly-flag predictive value (#5) lives in",
        "  the feedback loop (ALP-131).",
    ]


def format_report_text(report: FlagRateReport) -> str:
    """Render the report as plain text — one section per class, labeled by tunability."""
    lines: list[str] = []
    lines.append("Distillation anomaly flag-rate report")
    lines.append(
        f"  Window: {report.window_start.date()} to "
        f"{report.window_end.date()} ({report.window_days} days)"
    )
    lines.append(f"  Universe size:          {report.universe_size}")
    lines.append(f"  In-window invocations:  {report.invocation_count}")
    if report.ticker_filter is not None:
        lines.append(f"  Ticker filter:          {report.ticker_filter}")
    lines.append("")

    for klass in report.classes:
        tunability = "config-gated" if klass.is_config_gated else "structural"
        lines.append(f"=== {klass.threshold_class.upper()} ({tunability}) ===")
        for key in klass.keys:
            marker = "  [UNIVERSE-WIDE SILENCE]" if key.silence else ""
            split = key.calibration_split
            lines.append(
                f"  {key.threshold_key}: {key.flag_count} flags | "
                f"rate {key.rate:.6f} {_rate_basis_label(key)} | "
                f"calib c={split.calibrated} a={split.accumulating} u={split.unavailable} | "
                f"coverage {_coverage_label(key)} | "
                f"configured {_configured_value_label(key)}{marker}"
            )
        lines.append("")

    lines.extend(_related_tools_footer())
    return "\n".join(lines) + "\n"


def format_report_json(report: FlagRateReport) -> str:
    """Render the report as JSON: class name → threshold key → metrics record."""
    payload: dict[str, dict[str, dict[str, object]]] = {}
    for klass in report.classes:
        payload[klass.threshold_class] = {
            key.threshold_key: {
                "flag_count": key.flag_count,
                "rate": key.rate,
                "calibration_split": {
                    "calibrated": key.calibration_split.calibrated,
                    "accumulating": key.calibration_split.accumulating,
                    "unavailable": key.calibration_split.unavailable,
                },
                "coverage": key.coverage,
                "configured_value": key.configured_value,
                "silence": key.silence,
            }
            for key in klass.keys
        }
    return json.dumps(payload, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--db-path", type=str, default=None)
    parser.add_argument(
        "--threshold-class",
        type=str,
        default=ALL_CLASSES_CHOICE,
        choices=[*sorted(all_threshold_classes()), ALL_CLASSES_CHOICE],
    )
    parser.add_argument("--ticker", type=str, default=None)
    parser.add_argument("--output", type=str, choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    config = load_distillation_config()
    universe_size = len(load_universe_scope())
    engine = make_engine(args.db_path)
    factory = make_session_factory(engine)
    now = datetime.now(tz=UTC)
    with factory() as session:
        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=now,
            window_days=args.window_days,
            threshold_class=args.threshold_class,
            ticker=args.ticker,
        )
    if args.output == "json":
        print(format_report_json(report))
    else:
        print(format_report_text(report))
    return 0


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "CalibrationSplit",
    "FlagRateReport",
    "ThresholdClassReport",
    "ThresholdKeyMetrics",
    "compute_flag_rate_report",
    "format_report_json",
    "format_report_text",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
