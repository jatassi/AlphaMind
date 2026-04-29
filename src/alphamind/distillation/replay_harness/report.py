"""Markdown report renderer — story 02-distillation-layer/replay-harness/07.

Consumes the :class:`AggregatedReport` (story 06) and the run's
:class:`ReportProvenance` metadata, and emits a deterministic single-file
Markdown report at ``data/replay_reports/{report_id}/report.md``.
The same function handles single-config and diff-mode layouts; diff mode
adds candidate / baseline / delta sub-columns to every numeric table and a
``baseline_config_*`` row to the header.

Output is byte-deterministic so that re-running the harness on identical
inputs produces an identical file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from alphamind.distillation.replay_harness.aggregation import (
    AggregatedReport,
    BaselineShapeSummary,
    FlagRateCell,
    PerBaselineShapeSummary,
    PerRegimeFlagRates,
    RegimeLabelDistributionEntry,
)
from alphamind.distillation.replay_harness.candidate_config import LoadedCandidateConfig

_MODE_SINGLE = "single"
_MODE_DIFF = "diff"

_EMPTY_CELL = "—"
_EMPTY_FLAG_TABLE_MESSAGE = "*No anomaly flags fired across replayed invocations.*"


@dataclass(frozen=True)
class ReportProvenance:
    """Run metadata the renderer embeds in the report header.

    Kept separate from :class:`AggregatedReport` so the renderer can be
    tested without going through the engine or aggregator.
    """

    report_id: str
    generated_at: datetime
    harness_version: str
    git_sha: str
    candidate: LoadedCandidateConfig
    baseline: LoadedCandidateConfig | None
    fixture_slice_ids: dict[str, list[str]]
    regimes_replayed: tuple[str, ...]


class ReportRenderError(ValueError):
    """Raised when ``render_report`` receives mismatched provenance + aggregation modes."""


class ReportAlreadyExistsError(FileExistsError):
    """Raised when ``write_report`` would clobber an existing report directory."""


def _provenance_mode(provenance: ReportProvenance) -> str:
    return _MODE_DIFF if provenance.baseline is not None else _MODE_SINGLE


def render_report(provenance: ReportProvenance, aggregated: AggregatedReport) -> str:
    """Return the rendered Markdown report content as a single UTF-8 string."""
    if _provenance_mode(provenance) != aggregated.mode:
        raise ReportRenderError(
            f"provenance mode '{_provenance_mode(provenance)}' does not match "
            f"aggregation mode '{aggregated.mode}'"
        )

    sections = [
        _render_header(provenance),
        _render_flag_rates(aggregated),
        _render_regime_distribution(aggregated),
        _render_baseline_shape(aggregated),
        _render_calibration_breakdown(aggregated),
        _render_footer(),
    ]
    return "\n".join(sections)


def _render_header(provenance: ReportProvenance) -> str:
    lines: list[str] = ["# Replay harness report", ""]
    lines.append(f"**report_id:** {provenance.report_id}")
    lines.append(f"**generated_at:** {_format_datetime(provenance.generated_at)}")
    lines.append(f"**harness_version:** {provenance.harness_version}")
    lines.append(f"**git_sha:** {provenance.git_sha}")
    lines.append(f"**candidate_config_path:** {provenance.candidate.path}")
    lines.append(f"**candidate_config_sha256:** {provenance.candidate.content_hash}")
    lines.append(f"**candidate_config_size_bytes:** {provenance.candidate.content_bytes_size}")
    if provenance.baseline is not None:
        lines.append(f"**baseline_config_path:** {provenance.baseline.path}")
        lines.append(f"**baseline_config_sha256:** {provenance.baseline.content_hash}")
        lines.append(f"**baseline_config_size_bytes:** {provenance.baseline.content_bytes_size}")
    lines.append(f"**regimes_replayed:** {', '.join(provenance.regimes_replayed)}")
    lines.append("**fixture_slices_consumed:**")
    for regime in provenance.regimes_replayed:
        lines.append(f"  - {regime}:")
        for slice_id in provenance.fixture_slice_ids.get(regime, []):
            lines.append(f"    - {slice_id}")
    lines.append("")
    return "\n".join(lines)


def _render_flag_rates(aggregated: AggregatedReport) -> str:
    table = aggregated.flag_rates
    lines: list[str] = ["## Per-regime flag-rate table", ""]
    if not table.flag_types:
        lines.append(_EMPTY_FLAG_TABLE_MESSAGE)
        lines.append("")
        return "\n".join(lines)

    if table.mode == _MODE_SINGLE:
        lines.extend(_render_flag_rate_table_single(table))
    else:
        lines.extend(_render_flag_rate_table_diff(table))
    lines.append("")
    return "\n".join(lines)


def _render_flag_rate_table_single(table: PerRegimeFlagRates) -> list[str]:
    cells_by_key = {(c.flag_type, c.regime_label): c for c in table.single_cells or ()}
    header = ["flag_type", *table.regimes]
    separator = ["---"] * len(header)
    lines = [_md_row(header), _md_row(separator)]
    for flag_type in table.flag_types:
        row = [flag_type]
        for regime in table.regimes:
            cell = cells_by_key.get((flag_type, regime))
            row.append(_format_flag_rate_cell(cell))
        lines.append(_md_row(row))
    return lines


def _format_flag_rate_cell(cell: FlagRateCell | None) -> str:
    if cell is None or cell.flag_invocation_count == 0:
        return _EMPTY_CELL
    return f"{cell.rate:.2%} (n={cell.total_invocation_count})"


def _render_flag_rate_table_diff(table: PerRegimeFlagRates) -> list[str]:
    deltas_by_key = {
        (d.candidate.flag_type, d.candidate.regime_label): d for d in table.diff_cells or ()
    }
    header = ["flag_type"]
    for regime in table.regimes:
        header.extend([f"{regime} candidate", f"{regime} baseline", f"{regime} Δ"])
    separator = ["---"] * len(header)
    lines = [_md_row(header), _md_row(separator)]
    for flag_type in table.flag_types:
        row = [flag_type]
        for regime in table.regimes:
            delta = deltas_by_key.get((flag_type, regime))
            if delta is None:
                row.extend([_EMPTY_CELL, _EMPTY_CELL, _EMPTY_CELL])
                continue
            row.extend(
                [
                    _format_rate(delta.candidate.rate),
                    _format_rate(delta.baseline.rate),
                    _format_delta_pp(delta.delta_rate),
                ]
            )
        lines.append(_md_row(row))
    return lines


def _format_rate(rate: float) -> str:
    return f"{rate:.2%}"


def _format_delta_pp(delta: float) -> str:
    """Signed percentage-point delta — e.g. ``+0.0034`` → ``+0.34pp``."""
    return f"{delta * 100.0:+.2f}pp"


def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _render_regime_distribution(aggregated: AggregatedReport) -> str:
    lines: list[str] = ["## Regime-label distribution", ""]
    entries_by_regime: dict[str, list[str]] = {}
    for entry in aggregated.regime_distribution.entries:
        entries_by_regime.setdefault(entry.slice_exemplary_regime, []).append(
            f"- {entry.slice_id}: " + _format_label_counts(entry)
        )
    for regime, regime_lines in entries_by_regime.items():
        lines.append(f"### {regime}")
        lines.extend(regime_lines)
        lines.append("")
    return "\n".join(lines)


def _format_label_counts(entry: RegimeLabelDistributionEntry) -> str:
    all_labels = sorted(set(entry.candidate_label_counts) | set(entry.baseline_label_counts or {}))
    pairs: list[str] = []
    for label in all_labels:
        cand = entry.candidate_label_counts.get(label, 0)
        if entry.baseline_label_counts is None:
            pairs.append(f"{label}: candidate={cand}")
        else:
            base = entry.baseline_label_counts.get(label, 0)
            pairs.append(f"{label}: candidate={cand}, baseline={base}")
    return "; ".join(pairs)


def _render_baseline_shape(aggregated: AggregatedReport) -> str:
    summary = aggregated.baseline_shape
    lines: list[str] = ["## Class B baseline shape summary", ""]
    if summary.mode == _MODE_SINGLE:
        lines.extend(_render_baseline_shape_single(summary))
    else:
        lines.extend(_render_baseline_shape_diff(summary))
    return "\n".join(lines)


def _render_baseline_shape_single(summary: PerBaselineShapeSummary) -> list[str]:
    by_kind: dict[str, list[BaselineShapeSummary]] = {}
    for cell in summary.single_cells or ():
        by_kind.setdefault(cell.baseline_kind, []).append(cell)
    lines: list[str] = []
    for kind in by_kind:
        lines.append(f"### {kind}")
        lines.append(_md_row(["regime", "count", "median", "IQR"]))
        lines.append(_md_row(["---"] * 4))
        for cell in by_kind[kind]:
            lines.append(
                _md_row(
                    [
                        cell.regime_label,
                        str(cell.value_count),
                        _format_optional_metric(cell.value_median),
                        _format_optional_metric(cell.value_iqr),
                    ]
                )
            )
        lines.append("")
    return lines


def _render_baseline_shape_diff(summary: PerBaselineShapeSummary) -> list[str]:
    by_kind: dict[str, list[tuple[BaselineShapeSummary, BaselineShapeSummary]]] = {}
    for delta in summary.diff_cells or ():
        by_kind.setdefault(delta.candidate.baseline_kind, []).append(
            (delta.candidate, delta.baseline)
        )
    lines: list[str] = []
    for kind in by_kind:
        lines.append(f"### {kind}")
        header = [
            "regime",
            "candidate count",
            "baseline count",
            "candidate median",
            "baseline median",
            "candidate IQR",
            "baseline IQR",
        ]
        lines.append(_md_row(header))
        lines.append(_md_row(["---"] * len(header)))
        for cand, base in by_kind[kind]:
            lines.append(
                _md_row(
                    [
                        cand.regime_label,
                        str(cand.value_count),
                        str(base.value_count),
                        _format_optional_metric(cand.value_median),
                        _format_optional_metric(base.value_median),
                        _format_optional_metric(cand.value_iqr),
                        _format_optional_metric(base.value_iqr),
                    ]
                )
            )
        lines.append("")
    return lines


def _format_optional_metric(value: float | None) -> str:
    if value is None:
        return _EMPTY_CELL
    return f"{value:.4g}"


def _render_calibration_breakdown(aggregated: AggregatedReport) -> str:
    breakdown = aggregated.calibration_breakdown
    is_diff = aggregated.mode == _MODE_DIFF
    header = ["regime", "calibration_state", "candidate count", "baseline count"]
    if not is_diff:
        header = ["regime", "calibration_state", "count"]
    lines: list[str] = [
        "## Calibration-state breakdown",
        "",
        _md_row(header),
        _md_row(["---"] * len(header)),
    ]
    for entry in breakdown.entries:
        row = [entry.regime_label, entry.calibration_state, str(entry.candidate_count)]
        if is_diff:
            row.append(str(entry.baseline_count if entry.baseline_count is not None else 0))
        lines.append(_md_row(row))
    lines.append("")
    return "\n".join(lines)


def _render_footer() -> str:
    return (
        "---\n"
        "*Cite this report's `report_id` in `/feedback-validate` REGISTER step's "
        "`expected_magnitude` or `success_criterion` field for "
        "`config/distillation.yaml` edits.*\n"
    )


def _format_datetime(value: datetime) -> str:
    """ISO 8601 UTC with ``Z`` suffix; ``datetime.isoformat`` emits ``+00:00``."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_report(provenance: ReportProvenance, aggregated: AggregatedReport, root: Path) -> Path:
    """Render and persist the report under ``root / report_id / report.md``.

    Refuses to clobber an existing report directory: raises
    :class:`ReportAlreadyExistsError` instead.
    """
    rendered = render_report(provenance, aggregated)
    target_dir = root / provenance.report_id
    try:
        target_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ReportAlreadyExistsError(f"report directory already exists: {target_dir}") from exc
    target_path = target_dir / "report.md"
    target_path.write_bytes(rendered.encode("utf-8"))
    return target_path.resolve()


__all__ = [
    "ReportAlreadyExistsError",
    "ReportProvenance",
    "ReportRenderError",
    "render_report",
    "write_report",
]
