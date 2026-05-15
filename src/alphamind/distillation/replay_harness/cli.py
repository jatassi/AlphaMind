"""Command-line interface for the distillation replay harness.

`main()` runs the documented twelve-step procedure end-to-end: parse args,
load configs, discover fixtures, replay each regime's slices, aggregate, and
write a Markdown report under `data/replay_reports/{report_id}/`. See
`docs/design/02-distillation-layer/replay-harness.md` § Activation.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from alphamind.config.models.regimes import Regime
from alphamind.distillation.replay_harness.aggregation import (
    AggregatedReport,
    AggregationInputError,
    aggregate_replay_results,
)
from alphamind.distillation.replay_harness.candidate_config import (
    CandidateConfigError,
    LoadedCandidateConfig,
    compute_report_id,
    load_candidate_config,
)
from alphamind.distillation.replay_harness.engine import (
    SliceReplayResult,
    replay_slice,
)
from alphamind.distillation.replay_harness.fixtures import (
    FixtureSlice,
    discover_fixture_store,
)
from alphamind.distillation.replay_harness.report import (
    ReportAlreadyExistsError,
    ReportProvenance,
    write_report,
)
from alphamind.distillation.replay_harness.version import HARNESS_VERSION

logger = logging.getLogger("alphamind")

CANONICAL_REGIMES: tuple[str, ...] = tuple(r.value for r in Regime)
"""Canonical regime labels the harness supports for replay."""

DEFAULT_FIXTURE_STORE_ROOT: Path = Path("data/replay_fixtures")
"""Project-relative default for the fixture store root."""

DEFAULT_REPORT_ROOT: Path = Path("data/replay_reports")
"""Project-relative default for the report output root."""

GIT_SHA_FALLBACK: str = "(not in a git checkout)"
"""Sentinel returned when ``git rev-parse HEAD`` cannot resolve the SHA."""

_GIT_REV_PARSE_TIMEOUT_S: float = 5.0
"""Wall-clock cap on ``git rev-parse HEAD`` — rev-parse takes milliseconds in
any healthy repo. Per ``feedback_avoid_numeric_anchors.md``, this is structural,
not a tunable threshold."""

EXIT_OK: int = 0
EXIT_ERROR: int = 1


def _split_regimes(value: str) -> list[str]:
    return value.split(",")


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the replay harness CLI."""
    parser = argparse.ArgumentParser(
        prog="python -m alphamind.distillation.replay_harness",
        description=(
            "Re-run the deterministic distillation layer against archived "
            "fixtures under a candidate config and emit a per-regime "
            "flag-rate report. See docs/design/02-distillation-layer/"
            "replay-harness.md."
        ),
    )
    parser.add_argument(
        "--candidate-config",
        required=True,
        metavar="PATH",
        help="Path to a config/distillation.yaml snapshot to replay against.",
    )
    parser.add_argument(
        "--baseline-config",
        default=None,
        metavar="PATH",
        help=(
            "Optional path to a second config/distillation.yaml snapshot. "
            "When supplied, the harness runs both configs against the same "
            "fixture and emits a side-by-side diff report."
        ),
    )
    parser.add_argument(
        "--regimes",
        type=_split_regimes,
        default=list(CANONICAL_REGIMES),
        metavar="LABELS",
        help=(
            "Comma-separated subset of canonical regime labels "
            "(low_vol,normal,elevated,crisis). Default: all four."
        ),
    )
    parser.add_argument(
        "--fixture-store-root",
        type=Path,
        default=DEFAULT_FIXTURE_STORE_ROOT,
        metavar="PATH",
        help=(
            "Root directory of the regime-stratified fixture store (default: data/replay_fixtures)."
        ),
    )
    parser.add_argument(
        "--report-root",
        type=Path,
        default=DEFAULT_REPORT_ROOT,
        metavar="PATH",
        help=(
            "Root directory the report subdirectory is written under "
            "(default: data/replay_reports)."
        ),
    )
    return parser


class _CliError(Exception):
    """Internal: an operator-facing error captured for ``main`` to surface."""


def _emit_error(message: str) -> int:
    """Print an operator-facing error to stderr and return :data:`EXIT_ERROR`."""
    print(f"error: {message}", file=sys.stderr)
    return EXIT_ERROR


def _validate_regimes(requested: list[str]) -> None:
    """Raise :class:`_CliError` naming the first non-canonical label, if any."""
    for label in requested:
        if label not in CANONICAL_REGIMES:
            raise _CliError(
                f"unknown regime label '{label}'; choose from {','.join(CANONICAL_REGIMES)}"
            )


def _load_one_config(path: str) -> LoadedCandidateConfig:
    """Load and validate one candidate-config snapshot, translating errors."""
    try:
        return load_candidate_config(path)
    except (FileNotFoundError, CandidateConfigError) as exc:
        raise _CliError(str(exc)) from exc


def _resolve_git_sha() -> str:
    """Return ``git rev-parse HEAD`` output, or :data:`GIT_SHA_FALLBACK` on any failure.

    Never raises — the CLI prefers the recorded sentinel over an unhandled
    exception in the provenance pipeline.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_REV_PARSE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return GIT_SHA_FALLBACK
    if completed.returncode != 0:
        return GIT_SHA_FALLBACK
    sha = completed.stdout.strip()
    return sha or GIT_SHA_FALLBACK


def _replay_regimes(
    *,
    fixtures_by_regime: dict[str, list[FixtureSlice]],
    requested_regimes: list[str],
    config: LoadedCandidateConfig,
) -> dict[str, list[SliceReplayResult]]:
    """Replay every slice in every requested regime under ``config``."""
    results: dict[str, list[SliceReplayResult]] = {}
    for regime in sorted(requested_regimes):
        regime_results: list[SliceReplayResult] = []
        for fixture_slice in fixtures_by_regime[regime]:
            logger.info(
                "replay slice start: regime=%s slice=%s",
                regime,
                fixture_slice.manifest.slice_id,
            )
            regime_results.append(replay_slice(fixture_slice, config))
        results[regime] = regime_results
    return results


def _resolve_inputs(
    args: argparse.Namespace,
) -> tuple[LoadedCandidateConfig, LoadedCandidateConfig | None, dict[str, list[FixtureSlice]]]:
    """Validate args, load configs, discover fixtures, and check curation coverage.

    Surfaces every operator-facing failure in this prelude as a :class:`_CliError`
    so the runner shape stays a single try/except in ``main``.
    """
    _validate_regimes(args.regimes)

    candidate = _load_one_config(args.candidate_config)
    baseline = _load_one_config(args.baseline_config) if args.baseline_config else None
    logger.info(
        "configs loaded: candidate=%s baseline=%s",
        candidate.path,
        baseline.path if baseline is not None else "(none)",
    )

    fixture_store = discover_fixture_store(args.fixture_store_root)
    for warning in fixture_store.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    logger.info("regimes resolved: %s", ",".join(sorted(args.regimes)))

    for regime in args.regimes:
        if not fixture_store.slices.get(regime):
            raise _CliError(f"no fixture slices for regime '{regime}'; curate one before replay")

    return candidate, baseline, fixture_store.slices


def _build_provenance(
    *,
    generated_at: datetime,
    candidate: LoadedCandidateConfig,
    baseline: LoadedCandidateConfig | None,
    candidate_results: dict[str, list[SliceReplayResult]],
    requested_regimes: list[str],
) -> ReportProvenance:
    """Bundle the per-run provenance the renderer embeds in the report header."""
    report_id = compute_report_id(
        generated_at,
        candidate.content_hash,
        baseline.content_hash if baseline is not None else None,
    )
    fixture_slice_ids = {
        regime: [s.slice_id for s in candidate_results[regime]]
        for regime in sorted(requested_regimes)
    }
    return ReportProvenance(
        report_id=report_id,
        generated_at=generated_at,
        harness_version=HARNESS_VERSION,
        git_sha=_resolve_git_sha(),
        candidate=candidate,
        baseline=baseline,
        fixture_slice_ids=fixture_slice_ids,
        regimes_replayed=tuple(sorted(requested_regimes)),
    )


def _persist_report(
    *,
    provenance: ReportProvenance,
    aggregated: AggregatedReport,
    report_root: Path,
) -> Path:
    """Write the report and translate the collision error into a friendly message."""
    try:
        return write_report(provenance, aggregated, report_root)
    except ReportAlreadyExistsError as exc:
        existing = (report_root / provenance.report_id).resolve()
        raise _CliError(
            f"report directory {existing} already exists; "
            "refusing to overwrite (re-run with a different timestamp or "
            "remove the existing report)"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    """Run the replay harness end-to-end and return the operator-facing exit code."""
    args = build_parser().parse_args(argv)

    try:
        candidate, baseline, fixtures_by_regime = _resolve_inputs(args)

        candidate_results = _replay_regimes(
            fixtures_by_regime=fixtures_by_regime,
            requested_regimes=args.regimes,
            config=candidate,
        )
        baseline_results: dict[str, list[SliceReplayResult]] | None = None
        if baseline is not None:
            baseline_results = _replay_regimes(
                fixtures_by_regime=fixtures_by_regime,
                requested_regimes=args.regimes,
                config=baseline,
            )

        aggregated = aggregate_replay_results(candidate_results, baseline_results)
        logger.info("aggregation complete: mode=%s", aggregated.mode)

        provenance = _build_provenance(
            generated_at=datetime.now(UTC),
            candidate=candidate,
            baseline=baseline,
            candidate_results=candidate_results,
            requested_regimes=args.regimes,
        )
        report_path = _persist_report(
            provenance=provenance,
            aggregated=aggregated,
            report_root=args.report_root,
        )
    except (_CliError, AggregationInputError) as exc:
        return _emit_error(str(exc))
    except Exception as exc:
        # CLI outermost supervisor per runtime §G1: emit the error to stdout
        # so the operator sees a structured failure, log the full traceback,
        # and return a non-zero exit code. ``BaseException``
        # (``KeyboardInterrupt``) propagates so the operator's Ctrl-C surfaces.
        logger.exception("replay failure")
        return _emit_error(str(exc))

    logger.info("report written: %s", report_path)
    print(f"wrote report: {report_path}")
    return EXIT_OK
