"""Tests for the Markdown report renderer — story 02-distillation-layer/replay-harness/07.

Synthesizes ``(ReportProvenance, AggregatedReport)`` pairs directly (no engine
or aggregator dependency for shape) and verifies the renderer emits the
documented section structure with deterministic byte output, and that the
filesystem writer creates the canonical ``{report_id}/report.md`` artifact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from alphamind.distillation.replay_harness.aggregation import (
    AggregatedReport,
    BaselineShapeDelta,
    BaselineShapeSummary,
    CalibrationStateBreakdown,
    CalibrationStateBreakdownEntry,
    FlagRateCell,
    FlagRateDelta,
    PerBaselineShapeSummary,
    PerRegimeFlagRates,
    RegimeLabelDistribution,
    RegimeLabelDistributionEntry,
)
from alphamind.distillation.replay_harness.candidate_config import (
    LoadedCandidateConfig,
    load_candidate_config,
)
from alphamind.distillation.replay_harness.report import (
    ReportAlreadyExistsError,
    ReportProvenance,
    ReportRenderError,
    render_report,
    write_report,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_CONFIG_PATH = REPO_ROOT / "config" / "distillation.yaml"


# ---------------------------------------------------------------------------
# Synthetic builders
# ---------------------------------------------------------------------------


def _candidate_config() -> LoadedCandidateConfig:
    """Real ``LoadedCandidateConfig`` from the canonical YAML — exercises the full path."""
    return load_candidate_config(CANONICAL_CONFIG_PATH)


def _baseline_config(tmp_path: Path) -> LoadedCandidateConfig:
    """Synthesize a baseline config by appending one trailing whitespace byte.

    This produces a different content hash and size while staying YAML-valid.
    """
    target = tmp_path / "baseline_distillation.yaml"
    target.write_bytes(CANONICAL_CONFIG_PATH.read_bytes() + b" ")
    return load_candidate_config(target)


def _generated_at() -> datetime:
    return datetime(2026, 4, 28, 12, 30, 45, tzinfo=UTC)


def _single_provenance() -> ReportProvenance:
    return ReportProvenance(
        report_id="20260428T123045Z_abcdef0123456789",
        generated_at=_generated_at(),
        harness_version="0.1.0",
        git_sha="93a587e72217d84cab19c69fd5aee4d41c9e3083",
        candidate=_candidate_config(),
        baseline=None,
        fixture_slice_ids={"normal": ["slice_normal_1", "slice_normal_2"]},
        regimes_replayed=("normal",),
    )


def _diff_provenance(tmp_path: Path) -> ReportProvenance:
    return ReportProvenance(
        report_id="20260428T123045Z_abcdef0123456789_fedcba9876543210",
        generated_at=_generated_at(),
        harness_version="0.1.0",
        git_sha="93a587e72217d84cab19c69fd5aee4d41c9e3083",
        candidate=_candidate_config(),
        baseline=_baseline_config(tmp_path),
        fixture_slice_ids={
            "crisis": ["slice_crisis_1"],
            "normal": ["slice_normal_1"],
        },
        regimes_replayed=("crisis", "normal"),
    )


def _single_aggregated() -> AggregatedReport:
    flag_rates = PerRegimeFlagRates(
        mode="single",
        single_cells=(
            FlagRateCell(
                flag_type="volume_anomaly",
                regime_label="normal",
                flag_invocation_count=3,
                total_invocation_count=10,
                rate=0.3,
            ),
            FlagRateCell(
                flag_type="gap_anomaly",
                regime_label="normal",
                flag_invocation_count=1,
                total_invocation_count=10,
                rate=0.1,
            ),
        ),
        diff_cells=None,
        flag_types=("gap_anomaly", "volume_anomaly"),
        regimes=("normal",),
    )
    regime_distribution = RegimeLabelDistribution(
        entries=(
            RegimeLabelDistributionEntry(
                slice_id="slice_normal_1",
                slice_exemplary_regime="normal",
                candidate_label_counts={"normal": 4, "elevated": 1},
                baseline_label_counts=None,
            ),
            RegimeLabelDistributionEntry(
                slice_id="slice_normal_2",
                slice_exemplary_regime="normal",
                candidate_label_counts={"normal": 5},
                baseline_label_counts=None,
            ),
        )
    )
    baseline_shape = PerBaselineShapeSummary(
        mode="single",
        single_cells=(
            BaselineShapeSummary(
                baseline_kind="ticker_volume",
                regime_label="normal",
                value_count=20,
                value_median=12345.6789,
                value_iqr=4567.8901,
            ),
        ),
        diff_cells=None,
    )
    calibration_breakdown = CalibrationStateBreakdown(
        entries=(
            CalibrationStateBreakdownEntry(
                regime_label="normal",
                calibration_state="calibrated",
                candidate_count=15,
                baseline_count=None,
            ),
            CalibrationStateBreakdownEntry(
                regime_label="normal",
                calibration_state="accumulating",
                candidate_count=3,
                baseline_count=None,
            ),
        )
    )
    return AggregatedReport(
        mode="single",
        flag_rates=flag_rates,
        regime_distribution=regime_distribution,
        baseline_shape=baseline_shape,
        calibration_breakdown=calibration_breakdown,
    )


def _diff_aggregated() -> AggregatedReport:
    candidate_volume = FlagRateCell(
        flag_type="volume_anomaly",
        regime_label="normal",
        flag_invocation_count=4,
        total_invocation_count=10,
        rate=0.4,
    )
    baseline_volume = FlagRateCell(
        flag_type="volume_anomaly",
        regime_label="normal",
        flag_invocation_count=2,
        total_invocation_count=10,
        rate=0.2,
    )
    candidate_volume_crisis = FlagRateCell(
        flag_type="volume_anomaly",
        regime_label="crisis",
        flag_invocation_count=8,
        total_invocation_count=10,
        rate=0.8,
    )
    baseline_volume_crisis = FlagRateCell(
        flag_type="volume_anomaly",
        regime_label="crisis",
        flag_invocation_count=9,
        total_invocation_count=10,
        rate=0.9,
    )
    flag_rates = PerRegimeFlagRates(
        mode="diff",
        single_cells=None,
        diff_cells=(
            FlagRateDelta(
                candidate=candidate_volume_crisis,
                baseline=baseline_volume_crisis,
                delta_rate=-0.1,
            ),
            FlagRateDelta(
                candidate=candidate_volume,
                baseline=baseline_volume,
                delta_rate=0.2,
            ),
        ),
        flag_types=("volume_anomaly",),
        regimes=("crisis", "normal"),
    )
    regime_distribution = RegimeLabelDistribution(
        entries=(
            RegimeLabelDistributionEntry(
                slice_id="slice_crisis_1",
                slice_exemplary_regime="crisis",
                candidate_label_counts={"crisis": 3, "elevated": 2},
                baseline_label_counts={"crisis": 4, "elevated": 1},
            ),
            RegimeLabelDistributionEntry(
                slice_id="slice_normal_1",
                slice_exemplary_regime="normal",
                candidate_label_counts={"normal": 5},
                baseline_label_counts={"normal": 5},
            ),
        )
    )
    candidate_shape_normal = BaselineShapeSummary(
        baseline_kind="ticker_volume",
        regime_label="normal",
        value_count=10,
        value_median=100.0,
        value_iqr=20.0,
    )
    baseline_shape_normal = BaselineShapeSummary(
        baseline_kind="ticker_volume",
        regime_label="normal",
        value_count=10,
        value_median=110.0,
        value_iqr=15.0,
    )
    baseline_shape = PerBaselineShapeSummary(
        mode="diff",
        single_cells=None,
        diff_cells=(
            BaselineShapeDelta(
                candidate=candidate_shape_normal,
                baseline=baseline_shape_normal,
            ),
        ),
    )
    calibration_breakdown = CalibrationStateBreakdown(
        entries=(
            CalibrationStateBreakdownEntry(
                regime_label="crisis",
                calibration_state="calibrated",
                candidate_count=8,
                baseline_count=7,
            ),
            CalibrationStateBreakdownEntry(
                regime_label="normal",
                calibration_state="calibrated",
                candidate_count=15,
                baseline_count=14,
            ),
            CalibrationStateBreakdownEntry(
                regime_label="normal",
                calibration_state="accumulating",
                candidate_count=3,
                baseline_count=4,
            ),
        )
    )
    return AggregatedReport(
        mode="diff",
        flag_rates=flag_rates,
        regime_distribution=regime_distribution,
        baseline_shape=baseline_shape,
        calibration_breakdown=calibration_breakdown,
    )


# ---------------------------------------------------------------------------
# Tracer bullet — single-mode happy path
# ---------------------------------------------------------------------------


def test_render_report_single_mode_emits_all_documented_sections() -> None:
    """Single-mode renders header + four content sections + footer in order."""
    rendered = render_report(_single_provenance(), _single_aggregated())

    assert "# Replay harness report" in rendered
    assert "**report_id:** 20260428T123045Z_abcdef0123456789" in rendered
    assert "## Per-regime flag-rate table" in rendered
    assert "## Regime-label distribution" in rendered
    assert "## Class B baseline shape summary" in rendered
    assert "## Calibration-state breakdown" in rendered
    assert "/feedback-validate" in rendered


def test_render_report_diff_mode_includes_baseline_header_and_delta_columns(
    tmp_path: Path,
) -> None:
    """Diff-mode renders baseline_config_path in header and delta columns in tables."""
    rendered = render_report(_diff_provenance(tmp_path), _diff_aggregated())

    assert "**baseline_config_path:**" in rendered
    assert "**baseline_config_sha256:**" in rendered
    assert "**baseline_config_size_bytes:**" in rendered
    # Diff cells encode candidate / baseline / delta — at least one delta sign appears.
    assert "+20.00pp" in rendered or "-10.00pp" in rendered


def test_render_report_rejects_diff_aggregation_paired_with_single_provenance() -> None:
    """Diff-mode aggregation with no baseline provenance is a contract violation."""
    with pytest.raises(ReportRenderError, match="mode"):
        render_report(_single_provenance(), _diff_aggregated())


def test_render_report_rejects_single_aggregation_paired_with_diff_provenance(
    tmp_path: Path,
) -> None:
    """Single-mode aggregation with a baseline provenance is a contract violation."""
    with pytest.raises(ReportRenderError, match="mode"):
        render_report(_diff_provenance(tmp_path), _single_aggregated())


def test_render_report_is_byte_deterministic() -> None:
    """Two renders of identical inputs produce byte-identical output."""
    first = render_report(_single_provenance(), _single_aggregated())
    second = render_report(_single_provenance(), _single_aggregated())
    assert first.encode("utf-8") == second.encode("utf-8")


def test_render_report_empty_flag_table_renders_no_anomalies_message() -> None:
    """An aggregation with zero flag types still renders the section with a message."""
    aggregated = _single_aggregated()
    empty_flag_rates = PerRegimeFlagRates(
        mode="single",
        single_cells=(),
        diff_cells=None,
        flag_types=(),
        regimes=("normal",),
    )
    aggregated_no_flags = AggregatedReport(
        mode="single",
        flag_rates=empty_flag_rates,
        regime_distribution=aggregated.regime_distribution,
        baseline_shape=aggregated.baseline_shape,
        calibration_breakdown=aggregated.calibration_breakdown,
    )

    rendered = render_report(_single_provenance(), aggregated_no_flags)

    assert "## Per-regime flag-rate table" in rendered
    assert "*No anomaly flags fired across replayed invocations.*" in rendered


def test_render_report_single_regime_emits_one_column_flag_table() -> None:
    """``regimes_replayed`` covering one regime renders a one-column table."""
    rendered = render_report(_single_provenance(), _single_aggregated())

    # The single-mode header row has flag_type plus exactly one regime column.
    assert "| flag_type | normal |" in rendered
    # The bare regime name appears alone in the header — no duplicate sub-columns.
    assert "| flag_type | normal | normal |" not in rendered


def test_render_report_uses_fixed_precision_rate_and_delta_formatting(
    tmp_path: Path,
) -> None:
    """Rates render as ``{:.2%}``, deltas as ``{:+.2f}pp``."""
    flag_rates = PerRegimeFlagRates(
        mode="diff",
        single_cells=None,
        diff_cells=(
            FlagRateDelta(
                candidate=FlagRateCell(
                    flag_type="volume_anomaly",
                    regime_label="normal",
                    flag_invocation_count=1,
                    total_invocation_count=80,
                    rate=0.0125,
                ),
                baseline=FlagRateCell(
                    flag_type="volume_anomaly",
                    regime_label="normal",
                    flag_invocation_count=13,
                    total_invocation_count=80,
                    rate=0.0159,
                ),
                delta_rate=-0.0034,
            ),
        ),
        flag_types=("volume_anomaly",),
        regimes=("normal",),
    )
    aggregated_diff = AggregatedReport(
        mode="diff",
        flag_rates=flag_rates,
        regime_distribution=RegimeLabelDistribution(entries=()),
        baseline_shape=PerBaselineShapeSummary(mode="diff", single_cells=None, diff_cells=()),
        calibration_breakdown=CalibrationStateBreakdown(entries=()),
    )

    rendered = render_report(_diff_provenance(tmp_path), aggregated_diff)

    assert "1.25%" in rendered
    assert "-0.34pp" in rendered


def test_render_report_uses_fixed_precision_for_baseline_shape_median_and_iqr() -> None:
    """Baseline-shape ``median`` and ``IQR`` render with ``{:.4g}`` precision."""
    flag_rates = PerRegimeFlagRates(
        mode="single",
        single_cells=(),
        diff_cells=None,
        flag_types=(),
        regimes=("normal",),
    )
    baseline_shape = PerBaselineShapeSummary(
        mode="single",
        single_cells=(
            BaselineShapeSummary(
                baseline_kind="ticker_volume",
                regime_label="normal",
                value_count=10,
                value_median=12345.6789,
                value_iqr=4567.8901,
            ),
        ),
        diff_cells=None,
    )
    aggregated = AggregatedReport(
        mode="single",
        flag_rates=flag_rates,
        regime_distribution=RegimeLabelDistribution(entries=()),
        baseline_shape=baseline_shape,
        calibration_breakdown=CalibrationStateBreakdown(entries=()),
    )

    rendered = render_report(_single_provenance(), aggregated)

    # 12345.6789 with :.4g rounds to 1.235e+04.
    assert "1.235e+04" in rendered
    assert "4568" in rendered  # 4567.8901 with :.4g rounds to 4568.


def test_render_report_baseline_shape_zero_count_cell_renders_empty_marker() -> None:
    """A baseline-shape cell with ``value_count == 0`` renders ``—`` for median + IQR."""
    flag_rates = PerRegimeFlagRates(
        mode="single",
        single_cells=(),
        diff_cells=None,
        flag_types=(),
        regimes=("normal",),
    )
    baseline_shape = PerBaselineShapeSummary(
        mode="single",
        single_cells=(
            BaselineShapeSummary(
                baseline_kind="ticker_volume",
                regime_label="normal",
                value_count=0,
                value_median=None,
                value_iqr=None,
            ),
        ),
        diff_cells=None,
    )
    aggregated = AggregatedReport(
        mode="single",
        flag_rates=flag_rates,
        regime_distribution=RegimeLabelDistribution(entries=()),
        baseline_shape=baseline_shape,
        calibration_breakdown=CalibrationStateBreakdown(entries=()),
    )

    rendered = render_report(_single_provenance(), aggregated)

    assert "### ticker_volume" in rendered
    # The cell row carries the regime label plus em-dash markers for median/IQR.
    assert "| normal | 0 | — | — |" in rendered


def test_render_report_regime_distribution_lists_per_slice_counts_in_single_mode() -> None:
    """Regime-label distribution lists each slice with per-label counts in single mode."""
    rendered = render_report(_single_provenance(), _single_aggregated())

    assert "### normal" in rendered
    assert "slice_normal_1" in rendered
    assert "candidate=" in rendered
    # In single mode, the line should not also carry baseline= counts.
    for line in rendered.splitlines():
        if "slice_normal_1" in line:
            assert "baseline=" not in line


def test_render_report_regime_distribution_includes_baseline_counts_in_diff_mode(
    tmp_path: Path,
) -> None:
    """Diff-mode lines include both candidate= and baseline= per slice."""
    rendered = render_report(_diff_provenance(tmp_path), _diff_aggregated())

    # The regime-distribution section's per-slice line carries candidate= and baseline=.
    distribution_lines = [
        line for line in rendered.splitlines() if "slice_crisis_1" in line and "candidate=" in line
    ]
    assert distribution_lines
    for line in distribution_lines:
        assert "baseline=" in line


def test_render_report_calibration_breakdown_renders_documented_columns() -> None:
    """Calibration-state breakdown renders rows=regimes and a row per (regime, state)."""
    rendered = render_report(_single_provenance(), _single_aggregated())

    # Section heading present.
    assert "## Calibration-state breakdown" in rendered
    # Header row in the table mentions calibration_state and count columns.
    assert "calibration_state" in rendered
    assert "count" in rendered
    # The synthesized calibrated entry of count=15 surfaces in the rendered table.
    assert "| normal | calibrated | 15 |" in rendered


def test_render_report_calibration_breakdown_diff_mode_renders_candidate_and_baseline_counts(
    tmp_path: Path,
) -> None:
    """Diff-mode calibration breakdown renders separate candidate and baseline counts."""
    rendered = render_report(_diff_provenance(tmp_path), _diff_aggregated())

    # Calibrated row for normal has candidate=15 / baseline=14 in adjacent cells.
    assert "| normal | calibrated | 15 | 14 |" in rendered


def test_render_report_baseline_shape_diff_mode_renders_candidate_baseline_subcolumns(
    tmp_path: Path,
) -> None:
    """Diff-mode baseline-shape table renders candidate and baseline metric sub-columns."""
    rendered = render_report(_diff_provenance(tmp_path), _diff_aggregated())

    assert "### ticker_volume" in rendered
    # The candidate median (100.0) and baseline median (110.0) both surface.
    assert "100" in rendered and "110" in rendered


# ---------------------------------------------------------------------------
# write_report — file-system contract
# ---------------------------------------------------------------------------


def test_write_report_creates_report_md_under_report_id_directory(tmp_path: Path) -> None:
    """The writer creates ``root/report_id/report.md`` and returns its absolute path."""
    provenance = _single_provenance()
    aggregated = _single_aggregated()

    written = write_report(provenance, aggregated, tmp_path)

    expected = tmp_path / provenance.report_id / "report.md"
    assert written == expected.resolve()
    assert expected.read_text(encoding="utf-8").startswith("# Replay harness report")


def test_write_report_uses_lf_line_terminators(tmp_path: Path) -> None:
    """The written file contains no ``\\r`` bytes."""
    provenance = _single_provenance()
    written = write_report(provenance, _single_aggregated(), tmp_path)

    assert b"\r" not in written.read_bytes()


def test_write_report_refuses_to_clobber_existing_report_directory(
    tmp_path: Path,
) -> None:
    """A second write to the same report_id raises ``ReportAlreadyExistsError``."""
    provenance = _single_provenance()
    write_report(provenance, _single_aggregated(), tmp_path)

    with pytest.raises(ReportAlreadyExistsError):
        write_report(provenance, _single_aggregated(), tmp_path)
