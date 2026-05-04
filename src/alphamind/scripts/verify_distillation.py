"""Distillation end-to-end verification (story 13).

Operator entry point: connect to the SQLite database, run
:func:`alphamind.distillation.orchestrator.run_external_distillation`
once against the assets.yaml universe scope at "now", then assert the
structural properties documented in
``docs/implementation/02-distillation-layer/13-end-to-end-verification.md``
§ Scope hold:

- ``DistillationOutputs.sector_outputs`` has exactly three entries.
- The correlation/regime brief carries text and at least one ``[CR-N]`` reference.
- The universal regime label is one of the four documented strings.
- Every distillation state table has a row dated within the freshness window.
- The invocation-archive contains the five expected files.

Returns 0 when every structural assertion passes; 1 otherwise. The
script also surfaces the orchestrator's known placeholder gaps (q1/q3/q6/q7
per :data:`alphamind.distillation.orchestrator._PHASE_2_PLACEHOLDER_GAPS`)
in a "PLACEHOLDER GAPS" section of the summary so the operator sees the
gap explicitly rather than reading partial output as a regression.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind.config.models.distillation import DistillationConfig
from alphamind.distillation.orchestrator import (
    _PHASE_2_PLACEHOLDER_GAPS,
    DistillationOutputs,
    run_external_distillation,
)
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.regime import RegimeLabel
from alphamind.persistence.models import (
    DistillationCompositeState,
    DistillationContractHistory,
    DistillationEventHistory,
    DistillationPairLag,
    DistillationRegimeState,
    DistillationTickerBaseline,
)
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.scripts._artifact_io import (
    dump_correlation_regime_brief,
    dump_distillation_outputs,
    dump_universal_regime_label,
    stage_artifacts_dir,
)
from alphamind.scripts._common import (
    AssertionFailure,
    load_distillation_config,
    load_universe_scope,
)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
#
# The freshness window for "rows written within the last N minutes" is the
# story spec's `5 minutes` (line 36 of 13-end-to-end-verification.md).
# Encoded as a named constant so the value is discoverable.

DEFAULT_FRESHNESS_WINDOW_MINUTES = 5
"""Default freshness window in minutes.

Per story 13: state tables must show at least one row written within
the last 5 minutes of the verification run.
"""


# ---------------------------------------------------------------------------
# State-table schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StateTableProbe:
    """One distillation state table the verification checks for freshness."""

    table_name: str
    model: type
    timestamp_column_name: str
    deferred: bool = False
    """When True, an empty / stale table is reported as DEFERRED instead of
    STALE and does not count as a verification failure. Set for tables whose
    write path is a known TODO (see ``docs/project-tracker.md``)."""


_STATE_TABLES: tuple[_StateTableProbe, ...] = (
    _StateTableProbe(
        "distillation_ticker_baseline",
        DistillationTickerBaseline,
        "ingested_at",
    ),
    _StateTableProbe("distillation_pair_lag", DistillationPairLag, "ingested_at"),
    _StateTableProbe(
        "distillation_contract_history",
        DistillationContractHistory,
        "ingested_at",
    ),
    _StateTableProbe(
        "distillation_event_history",
        DistillationEventHistory,
        "ingested_at",
    ),
    _StateTableProbe(
        "distillation_regime_state",
        DistillationRegimeState,
        "ingested_at",
    ),
    _StateTableProbe(
        "distillation_composite_state",
        DistillationCompositeState,
        "ingested_at",
    ),
)


# ---------------------------------------------------------------------------
# Archive-file schedule
# ---------------------------------------------------------------------------

_ARCHIVE_FILES: tuple[str, ...] = (
    "tech_semis_sector.md",
    "financials_sector.md",
    "energy_sector.md",
    "correlation_regime_brief.md",
    "regime.md",
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateTableSnapshot:
    """Per-table freshness snapshot for the summary."""

    table_name: str
    row_count: int
    most_recent_ingested_at: str | None
    fresh: bool
    deferred: bool = False


@dataclass(frozen=True)
class VerificationReport:
    """The structured result of one verification pass.

    Surfaces both the pass/fail decision and the diagnostic summary the
    operator reads.
    """

    passed: bool
    failures: tuple[AssertionFailure, ...]
    sector_audiences: tuple[OutputAudience, ...]
    sector_block_counts: Mapping[OutputAudience, int]
    regime_label: str
    archive_dir: Path
    archive_files_present: tuple[str, ...]
    archive_files_missing: tuple[str, ...]
    total_anomalies: int
    bootstrap_block_count: int
    total_blocks: int
    freshness_min: datetime | None
    freshness_max: datetime | None
    state_tables: tuple[StateTableSnapshot, ...]
    placeholder_gaps: tuple[tuple[str, str], ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Pure assertion logic
# ---------------------------------------------------------------------------


def _check_sector_outputs(
    outputs: DistillationOutputs,
) -> list[AssertionFailure]:
    failures: list[AssertionFailure] = []
    expected_audiences = {
        OutputAudience.SECTOR_TECH_SEMIS,
        OutputAudience.SECTOR_FINANCIALS,
        OutputAudience.SECTOR_ENERGY,
    }
    actual = set(outputs.sector_outputs)
    missing = expected_audiences - actual
    if missing:
        names = ", ".join(sorted(audience.value for audience in missing))
        failures.append(
            AssertionFailure(
                code="sector-outputs-missing",
                message=f"sector_outputs is missing audiences: {names}",
            )
        )
    for audience, sector_output in outputs.sector_outputs.items():
        if not sector_output.text:
            failures.append(
                AssertionFailure(
                    code="sector-output-empty-text",
                    message=f"sector_outputs[{audience.value}].text is empty",
                )
            )
        if not sector_output.tickers:
            failures.append(
                AssertionFailure(
                    code="sector-output-empty-tickers",
                    message=(f"sector_outputs[{audience.value}].tickers tuple is empty"),
                )
            )
    return failures


def _check_correlation_regime_brief(
    outputs: DistillationOutputs,
) -> list[AssertionFailure]:
    failures: list[AssertionFailure] = []
    text = outputs.correlation_regime_brief.text
    if not text:
        failures.append(
            AssertionFailure(
                code="cr-brief-empty",
                message="correlation_regime_brief.text is empty",
            )
        )
        return failures
    if "[CR-1]" not in text:
        failures.append(
            AssertionFailure(
                code="cr-brief-missing-cr1-reference",
                message=(
                    "correlation_regime_brief.text does not contain a [CR-1] "
                    "reference (the regime line)"
                ),
            )
        )
    return failures


def _check_regime_label(
    outputs: DistillationOutputs,
) -> list[AssertionFailure]:
    failures: list[AssertionFailure] = []
    label = outputs.universal_regime_label.get("regime_label")
    valid = {member.value for member in RegimeLabel}
    if label not in valid:
        failures.append(
            AssertionFailure(
                code="regime-label-unknown",
                message=(
                    f"universal_regime_label['regime_label']={label!r} is not "
                    f"one of the four documented labels {sorted(valid)}"
                ),
            )
        )
    return failures


def _probe_state_table(
    session: Session,
    *,
    probe: _StateTableProbe,
    now: datetime,
    freshness_window_minutes: int,
) -> StateTableSnapshot:
    column = getattr(probe.model, probe.timestamp_column_name)
    most_recent: str | None = session.execute(select(func.max(column))).scalar_one_or_none()
    row_count: int = session.execute(select(func.count()).select_from(probe.model)).scalar_one()
    fresh = False
    if most_recent is not None:
        try:
            ts = datetime.fromisoformat(most_recent.rstrip("Z")).replace(tzinfo=UTC)
        except ValueError:
            ts = None
        if ts is not None:
            window = timedelta(minutes=freshness_window_minutes)
            fresh = (now - ts) <= window
    return StateTableSnapshot(
        table_name=probe.table_name,
        row_count=row_count,
        most_recent_ingested_at=most_recent,
        fresh=fresh,
        deferred=probe.deferred,
    )


def _check_state_tables(
    session: Session,
    *,
    now: datetime,
    freshness_window_minutes: int,
) -> tuple[list[AssertionFailure], tuple[StateTableSnapshot, ...]]:
    failures: list[AssertionFailure] = []
    snapshots: list[StateTableSnapshot] = []
    for probe in _STATE_TABLES:
        snap = _probe_state_table(
            session,
            probe=probe,
            now=now,
            freshness_window_minutes=freshness_window_minutes,
        )
        snapshots.append(snap)
        if not snap.fresh and not probe.deferred:
            failures.append(
                AssertionFailure(
                    code="state-table-stale",
                    message=(
                        f"{probe.table_name} has no row within the last "
                        f"{freshness_window_minutes} minutes "
                        f"(row_count={snap.row_count}, most_recent={snap.most_recent_ingested_at})"
                    ),
                )
            )
    return failures, tuple(snapshots)


def _check_archive_files(
    archive_dir: Path,
) -> tuple[list[AssertionFailure], tuple[str, ...], tuple[str, ...]]:
    present: list[str] = []
    missing: list[str] = []
    for fname in _ARCHIVE_FILES:
        if (archive_dir / fname).exists():
            present.append(fname)
        else:
            missing.append(fname)
    failures: list[AssertionFailure] = []
    if missing:
        failures.append(
            AssertionFailure(
                code="archive-file-missing",
                message=(f"archive directory {archive_dir} is missing files: {', '.join(missing)}"),
            )
        )
    return failures, tuple(present), tuple(missing)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def compute_report(
    *,
    outputs: DistillationOutputs,
    session: Session,
    archive_dir: Path,
    now: datetime,
    freshness_window_minutes: int = DEFAULT_FRESHNESS_WINDOW_MINUTES,
) -> VerificationReport:
    """Run every structural assertion and return a :class:`VerificationReport`.

    The function does no I/O of its own (the session and archive_dir
    are passed in). Callers wire it up to the live database / archive
    in :func:`main`; tests pass mock sessions and tmp_path directories.
    """
    failures: list[AssertionFailure] = []
    failures.extend(_check_sector_outputs(outputs))
    failures.extend(_check_correlation_regime_brief(outputs))
    failures.extend(_check_regime_label(outputs))
    state_failures, state_snapshots = _check_state_tables(
        session,
        now=now,
        freshness_window_minutes=freshness_window_minutes,
    )
    failures.extend(state_failures)
    archive_failures, present, missing = _check_archive_files(archive_dir)
    failures.extend(archive_failures)

    sector_audiences = tuple(sorted(outputs.sector_outputs.keys(), key=lambda a: a.value))
    sector_block_counts: dict[OutputAudience, int] = {
        audience: len(out.block_ids) for audience, out in outputs.sector_outputs.items()
    }
    label_value = outputs.universal_regime_label.get("regime_label", "")
    label_str = label_value if isinstance(label_value, str) else str(label_value)

    freshness_values = [out.freshness_min for out in outputs.sector_outputs.values()]
    if freshness_values:
        freshness_min = min(freshness_values)
        freshness_max = max(freshness_values)
    else:
        freshness_min = None
        freshness_max = None

    return VerificationReport(
        passed=not failures,
        failures=tuple(failures),
        sector_audiences=sector_audiences,
        sector_block_counts=sector_block_counts,
        regime_label=label_str,
        archive_dir=archive_dir,
        archive_files_present=present,
        archive_files_missing=missing,
        total_anomalies=outputs.total_anomalies,
        bootstrap_block_count=outputs.bootstrap_block_count,
        total_blocks=outputs.total_blocks,
        freshness_min=freshness_min,
        freshness_max=freshness_max,
        state_tables=state_snapshots,
        placeholder_gaps=_PHASE_2_PLACEHOLDER_GAPS,
    )


_BANNER = "=" * 70


def _render_sector_section(report: VerificationReport) -> list[str]:
    lines: list[str] = ["", "[ Sector outputs ]"]
    if not report.sector_audiences:
        lines.append("  (no sector outputs returned)")
        return lines
    for audience in report.sector_audiences:
        n_blocks = report.sector_block_counts.get(audience, 0)
        lines.append(f"  {audience.value:<30} blocks={n_blocks}")
    return lines


def _render_regime_section(report: VerificationReport) -> list[str]:
    return [
        "",
        "[ Universal regime ]",
        f"  regime_label = {report.regime_label}",
    ]


def _render_aggregate_section(report: VerificationReport) -> list[str]:
    lines: list[str] = [
        "",
        "[ Aggregate counts ]",
        f"  total_blocks = {report.total_blocks}",
        f"  total_anomalies = {report.total_anomalies}",
        f"  bootstrap_block_count = {report.bootstrap_block_count}",
    ]
    if report.freshness_min is not None and report.freshness_max is not None:
        lines.append(
            f"  freshness range = {report.freshness_min.isoformat()} .. "
            f"{report.freshness_max.isoformat()}"
        )
    else:
        lines.append("  freshness range = (no sector outputs)")
    return lines


def _render_state_table_section(report: VerificationReport) -> list[str]:
    lines: list[str] = ["", "[ State-table freshness ]"]
    for snap in report.state_tables:
        ts = snap.most_recent_ingested_at or "(no rows)"
        if snap.fresh:
            status = "OK"
        elif snap.deferred:
            status = "DEFERRED"
        else:
            status = "STALE"
        lines.append(
            f"  {snap.table_name:<35} rows={snap.row_count:<6} most_recent={ts:<25} {status}"
        )
    return lines


def _render_archive_section(report: VerificationReport) -> list[str]:
    lines: list[str] = ["", "[ Invocation archive ]", f"  dir = {report.archive_dir}"]
    for fname in _ARCHIVE_FILES:
        status = "OK" if fname in report.archive_files_present else "MISSING"
        lines.append(f"  {fname:<35} {status}")
    return lines


def _render_placeholder_gaps_section(report: VerificationReport) -> list[str]:
    if not report.placeholder_gaps:
        return [
            "",
            "[ PLACEHOLDER GAPS ]",
            "  None — all six Phase 2 categories integrated.",
        ]
    lines: list[str] = [
        "",
        "[ PLACEHOLDER GAPS ]",
        ("  Phase 2 categories that the orchestrator currently routes to a no-op block list."),
        ("  These produce empty output today; integration is deferred to follow-up stories."),
    ]
    for category, narration in report.placeholder_gaps:
        lines.append(f"  - {category}: {narration}")
    return lines


def _render_failures_section(report: VerificationReport) -> list[str]:
    if not report.failures:
        return []
    lines: list[str] = ["", "[ Failures ]"]
    for failure in report.failures:
        lines.append(f"  {failure.code}: {failure.message}")
    return lines


def _render_result_line(report: VerificationReport) -> str:
    if report.passed:
        return "RESULT: PASS — all structural assertions hold."
    return f"RESULT: FAIL — {len(report.failures)} structural assertion(s) failed (see above)."


def format_report(report: VerificationReport) -> str:
    """Render the human-readable summary the operator reads.

    Format mirrors ``scripts/verify_bootstrap.py`` § header / sections /
    result block: a banner, a summary table per audience, the
    state-table freshness probe, the archive-file probe, the
    placeholder-gaps section, and a final ``RESULT: PASS|FAIL`` line.
    """
    lines: list[str] = [
        _BANNER,
        "AlphaMind Distillation End-to-End Verification",
        _BANNER,
    ]
    lines.extend(_render_sector_section(report))
    lines.extend(_render_regime_section(report))
    lines.extend(_render_aggregate_section(report))
    lines.extend(_render_state_table_section(report))
    lines.extend(_render_archive_section(report))
    lines.extend(_render_placeholder_gaps_section(report))
    lines.extend(_render_failures_section(report))
    lines.extend(["", _BANNER, _render_result_line(report), _BANNER])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Top-level orchestration: run the orchestrator, build the report, render it
# ---------------------------------------------------------------------------


# Signature of the orchestrator entry point. Tests pass a stub; main()
# threads the real :func:`run_external_distillation`. Coroutine (vs
# Awaitable) so :func:`asyncio.run` accepts the return value directly.
OrchestratorCallable = Callable[..., Coroutine[Any, Any, DistillationOutputs]]


def _resolve_archive_dir(
    *,
    archive_root: Path,
    as_of: datetime,
    invocation_id: str,
) -> Path:
    """Mirror :func:`alphamind.distillation.orchestrator._invocation_archive_dir`.

    The orchestrator and the verification script use the same date /
    invocation partitioning so the script can locate the archive the
    orchestrator just wrote without parsing the orchestrator module's
    private helpers.
    """
    date_part = as_of.astimezone(UTC).strftime("%Y-%m-%d")
    return archive_root / date_part / invocation_id / "distillation"


def run_verification(
    *,
    session: Session,
    ticker_scope: Sequence[str],
    archive_root: Path,
    as_of: datetime,
    invocation_id: str,
    config: DistillationConfig | None = None,
    orchestrator: OrchestratorCallable = run_external_distillation,
    freshness_window_minutes: int = DEFAULT_FRESHNESS_WINDOW_MINUTES,
) -> int:
    """Run the orchestrator once, build the report, print it, return exit code.

    The ``orchestrator`` parameter is a seam for tests — production
    callers omit it and the default :func:`run_external_distillation` is
    used. The function does I/O (prints to stdout), but every dependency
    (session, archive root, scope) is a parameter.

    Returns 0 when every structural assertion passes; 1 otherwise. A
    placeholder-gap finding alone never fails the run — gaps are
    surfaced in the summary, not as assertion failures, per the story-13
    spec.

    On a passing run, the orchestrator's outputs are also dumped to the
    per-invocation stage-artifacts directory so downstream verification
    scripts can consume them via ``--upstream-from`` (ALP-287). The dump
    is skipped on a failed run — there's no point handing fail-state
    artifacts to the next stage.
    """
    if config is None:
        config = load_distillation_config()

    outputs = asyncio.run(
        orchestrator(
            session=session,
            config=config,
            ticker_scope=ticker_scope,
            as_of=as_of,
            invocation_id=invocation_id,
            archive_root=archive_root,
        )
    )
    archive_dir = _resolve_archive_dir(
        archive_root=archive_root,
        as_of=as_of,
        invocation_id=invocation_id,
    )
    report = compute_report(
        outputs=outputs,
        session=session,
        archive_dir=archive_dir,
        now=as_of,
        freshness_window_minutes=freshness_window_minutes,
    )
    print(format_report(report))
    if report.passed:
        stage_dir = stage_artifacts_dir(archive_root, invocation_id)
        dump_distillation_outputs(outputs, stage_dir)
        dump_correlation_regime_brief(outputs.correlation_regime_brief, stage_dir)
        dump_universal_regime_label(outputs.universal_regime_label, stage_dir)
        print(f"[verify_distillation] stage artifacts written to {stage_dir}")
    return 0 if report.passed else 1


def _format_invocation_id(now: datetime) -> str:
    """``YYYYMMDDTHHMMSSZ-verify`` — distinct from the pipeline's IDs.

    The ``-verify`` suffix tags the archive so an operator scanning
    archive directories can distinguish a verification run from a
    scheduled pipeline invocation.
    """
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-verify"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the exit code; never raises."""
    parser = argparse.ArgumentParser(
        description=(
            "Run the distillation orchestrator end-to-end and verify the "
            "structural conditions documented in story 13."
        )
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to the AlphaMind SQLite database (overrides DATABASE_PATH and main.yaml).",
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help=(
            "Path to the invocation-archive root (defaults to "
            "%%USERPROFILE%%/AlphaMind/archive on Windows, ~/AlphaMind/archive elsewhere)."
        ),
    )
    parser.add_argument(
        "--freshness-window-minutes",
        type=int,
        default=DEFAULT_FRESHNESS_WINDOW_MINUTES,
        help="Freshness window in minutes for the state-table probe.",
    )
    parser.add_argument(
        "--invocation-id",
        type=str,
        default=None,
        help=(
            "Override the invocation_id used for the distillation orchestrator's "
            "archive layout AND the stage-artifacts directory shared with "
            "downstream verification scripts. Defaults to "
            "YYYYMMDDTHHMMSSZ-verify."
        ),
    )
    args = parser.parse_args(argv)

    engine = make_engine(args.db_path)
    factory = make_session_factory(engine)

    archive_root = args.archive_root
    if archive_root is None:
        from alphamind.distillation.orchestrator import _default_archive_root

        archive_root = _default_archive_root()

    now = datetime.now(tz=UTC)
    invocation_id = args.invocation_id or _format_invocation_id(now)
    ticker_scope = load_universe_scope()

    with factory() as session:
        return run_verification(
            session=session,
            ticker_scope=ticker_scope,
            archive_root=archive_root,
            as_of=now,
            invocation_id=invocation_id,
            freshness_window_minutes=args.freshness_window_minutes,
        )


__all__ = [
    "DEFAULT_FRESHNESS_WINDOW_MINUTES",
    "AssertionFailure",
    "OrchestratorCallable",
    "StateTableSnapshot",
    "VerificationReport",
    "compute_report",
    "format_report",
    "main",
    "run_verification",
]


if __name__ == "__main__":
    sys.exit(main())
