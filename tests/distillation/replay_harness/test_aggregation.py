"""Tests for the aggregation primitives — story 02-distillation-layer/replay-harness/06.

Constructs synthetic ``SliceReplayResult`` instances directly (no engine dependency)
and verifies that ``aggregate_replay_results`` produces the four content aggregations
the report renderer (story 07) consumes: per-regime flag-rate table, regime-label
distribution, Class B baseline shape summary, calibration-state breakdown.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from alphamind.distillation.replay_harness.aggregation import (
    AggregatedReport,
    AggregationInputError,
    aggregate_replay_results,
)
from alphamind.distillation.replay_harness.engine import (
    AnomalyFlagRecord,
    BaselineValueRecord,
    InvocationOutputs,
    SliceReplayResult,
)

# ---------------------------------------------------------------------------
# Synthetic builders — keep tests focused on aggregation behavior
# ---------------------------------------------------------------------------


def _build_invocation(
    *,
    slice_id: str,
    index: int,
    regime_label: str = "normal",
    transition_state: str = "stable",
    anomaly_flags: tuple[AnomalyFlagRecord, ...] = (),
    baselines: tuple[BaselineValueRecord, ...] = (),
) -> InvocationOutputs:
    base = datetime(2026, 4, 25, 0, 0, 0, tzinfo=UTC)
    as_of = base + timedelta(hours=index)
    return InvocationOutputs(
        as_of=as_of,
        invocation_id=f"replay_{slice_id}_{index}",
        regime_label=regime_label,
        regime_transition_state=transition_state,
        anomaly_flags=anomaly_flags,
        class_b_baseline_values=baselines,
    )


def _build_slice_result(
    *,
    slice_id: str,
    exemplary_regime: str,
    invocations: tuple[InvocationOutputs, ...],
    config_content_hash: str = "sha256:cand",
) -> SliceReplayResult:
    return SliceReplayResult(
        slice_id=slice_id,
        regime_label=exemplary_regime,
        invocations=invocations,
        config_content_hash=config_content_hash,
    )


# ---------------------------------------------------------------------------
# Tracer bullet — single-mode aggregation returns the documented shape
# ---------------------------------------------------------------------------


def test_aggregate_returns_aggregated_report_in_single_mode() -> None:
    """A trivial single-config aggregation yields ``AggregatedReport(mode='single')``."""
    invocation = _build_invocation(slice_id="s1", index=0)
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=(invocation,)
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    assert isinstance(report, AggregatedReport)
    assert report.mode == "single"
    assert report.flag_rates.mode == "single"
    assert report.baseline_shape.mode == "single"


# ---------------------------------------------------------------------------
# Per-regime flag-rate table
# ---------------------------------------------------------------------------


def _flag(flag_type: str, *, calibration_state: str = "calibrated") -> AnomalyFlagRecord:
    return AnomalyFlagRecord(
        flag_type=flag_type,
        ticker_or_pair_key=None,
        magnitude=1.0,
        calibration_state=calibration_state,
    )


def test_flag_rate_cell_rate_equals_observed_fraction() -> None:
    """1 regime, 2 slices, 5 invocations each, 3 flag types — rates match counts."""
    # Slice s1: volume_anomaly fires in 4/5; gap_anomaly in 1/5; iv_skew in 0/5.
    s1_invocations = (
        _build_invocation(slice_id="s1", index=0, anomaly_flags=(_flag("volume_anomaly"),)),
        _build_invocation(slice_id="s1", index=1, anomaly_flags=(_flag("volume_anomaly"),)),
        _build_invocation(
            slice_id="s1",
            index=2,
            anomaly_flags=(_flag("volume_anomaly"), _flag("gap_anomaly")),
        ),
        _build_invocation(slice_id="s1", index=3, anomaly_flags=(_flag("volume_anomaly"),)),
        _build_invocation(slice_id="s1", index=4, anomaly_flags=()),
    )
    # Slice s2: volume_anomaly fires in 2/5; gap_anomaly in 0/5; iv_skew in 3/5.
    s2_invocations = (
        _build_invocation(slice_id="s2", index=0, anomaly_flags=(_flag("iv_skew"),)),
        _build_invocation(slice_id="s2", index=1, anomaly_flags=(_flag("volume_anomaly"),)),
        _build_invocation(slice_id="s2", index=2, anomaly_flags=(_flag("iv_skew"),)),
        _build_invocation(slice_id="s2", index=3, anomaly_flags=()),
        _build_invocation(
            slice_id="s2",
            index=4,
            anomaly_flags=(_flag("volume_anomaly"), _flag("iv_skew")),
        ),
    )
    s1 = _build_slice_result(slice_id="s1", exemplary_regime="normal", invocations=s1_invocations)
    s2 = _build_slice_result(slice_id="s2", exemplary_regime="normal", invocations=s2_invocations)

    report = aggregate_replay_results({"normal": [s1, s2]})

    cells_by_flag = {cell.flag_type: cell for cell in report.flag_rates.single_cells or ()}
    assert cells_by_flag["volume_anomaly"].flag_invocation_count == 6
    assert cells_by_flag["volume_anomaly"].total_invocation_count == 10
    assert cells_by_flag["volume_anomaly"].rate == pytest.approx(0.6)
    assert cells_by_flag["gap_anomaly"].flag_invocation_count == 1
    assert cells_by_flag["gap_anomaly"].rate == pytest.approx(0.1)
    assert cells_by_flag["iv_skew"].flag_invocation_count == 3
    assert cells_by_flag["iv_skew"].rate == pytest.approx(0.3)
    assert report.flag_rates.flag_types == ("gap_anomaly", "iv_skew", "volume_anomaly")
    assert report.flag_rates.regimes == ("normal",)


def test_flag_rate_deduplicates_within_invocation() -> None:
    """An invocation that fires three volume_anomalies counts as 1, not 3."""
    triple = (
        AnomalyFlagRecord("volume_anomaly", "AAPL", 2.5, "calibrated"),
        AnomalyFlagRecord("volume_anomaly", "MSFT", 3.0, "calibrated"),
        AnomalyFlagRecord("volume_anomaly", "NVDA", 4.0, "calibrated"),
    )
    invocations = (
        _build_invocation(slice_id="s1", index=0, anomaly_flags=triple),
        _build_invocation(slice_id="s1", index=1, anomaly_flags=()),
    )
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=invocations
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    cells_by_flag = {cell.flag_type: cell for cell in report.flag_rates.single_cells or ()}
    assert cells_by_flag["volume_anomaly"].flag_invocation_count == 1
    assert cells_by_flag["volume_anomaly"].total_invocation_count == 2
    assert cells_by_flag["volume_anomaly"].rate == pytest.approx(0.5)


def test_flag_rate_table_covers_every_observed_regime() -> None:
    """4 regimes, 1 slice each — the regimes tuple lists all four sorted alphabetically."""
    inputs: dict[str, list[SliceReplayResult]] = {}
    for regime in ("low_vol", "normal", "elevated", "crisis"):
        invocation = _build_invocation(
            slice_id=f"slice_{regime}",
            index=0,
            anomaly_flags=(_flag("volume_anomaly"),),
        )
        inputs[regime] = [
            _build_slice_result(
                slice_id=f"slice_{regime}", exemplary_regime=regime, invocations=(invocation,)
            )
        ]

    report = aggregate_replay_results(inputs)

    assert report.flag_rates.regimes == ("crisis", "elevated", "low_vol", "normal")
    cells_by_regime = {cell.regime_label: cell for cell in report.flag_rates.single_cells or ()}
    assert set(cells_by_regime) == {"crisis", "elevated", "low_vol", "normal"}
    for regime in cells_by_regime:
        assert cells_by_regime[regime].rate == pytest.approx(1.0)


def test_diff_mode_emits_delta_rate_with_correct_sign() -> None:
    """Candidate fires more often than baseline — delta_rate is positive."""
    candidate_invocations = (
        _build_invocation(slice_id="s1", index=0, anomaly_flags=(_flag("volume_anomaly"),)),
        _build_invocation(slice_id="s1", index=1, anomaly_flags=(_flag("volume_anomaly"),)),
        _build_invocation(slice_id="s1", index=2, anomaly_flags=(_flag("volume_anomaly"),)),
        _build_invocation(slice_id="s1", index=3, anomaly_flags=(_flag("volume_anomaly"),)),
    )
    baseline_invocations = (
        _build_invocation(slice_id="s1", index=0, anomaly_flags=(_flag("volume_anomaly"),)),
        _build_invocation(slice_id="s1", index=1, anomaly_flags=()),
        _build_invocation(slice_id="s1", index=2, anomaly_flags=()),
        _build_invocation(slice_id="s1", index=3, anomaly_flags=()),
    )
    candidate_slice = _build_slice_result(
        slice_id="s1",
        exemplary_regime="normal",
        invocations=candidate_invocations,
        config_content_hash="sha256:cand",
    )
    baseline_slice = _build_slice_result(
        slice_id="s1",
        exemplary_regime="normal",
        invocations=baseline_invocations,
        config_content_hash="sha256:base",
    )

    report = aggregate_replay_results({"normal": [candidate_slice]}, {"normal": [baseline_slice]})

    assert report.mode == "diff"
    assert report.flag_rates.mode == "diff"
    assert report.flag_rates.single_cells is None
    assert report.flag_rates.diff_cells is not None
    delta_by_flag = {cell.candidate.flag_type: cell for cell in report.flag_rates.diff_cells}
    delta = delta_by_flag["volume_anomaly"]
    assert delta.candidate.rate == pytest.approx(1.0)
    assert delta.baseline.rate == pytest.approx(0.25)
    assert delta.delta_rate == pytest.approx(0.75)


def test_diff_mode_flag_only_under_candidate_appears_with_zero_baseline_rate() -> None:
    """A flag firing only under candidate appears in diff cells with baseline rate 0."""
    candidate_invocations = (
        _build_invocation(slice_id="s1", index=0, anomaly_flags=(_flag("new_signal"),)),
        _build_invocation(slice_id="s1", index=1, anomaly_flags=(_flag("new_signal"),)),
    )
    baseline_invocations = (
        _build_invocation(slice_id="s1", index=0, anomaly_flags=()),
        _build_invocation(slice_id="s1", index=1, anomaly_flags=()),
    )
    candidate_slice = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=candidate_invocations
    )
    baseline_slice = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=baseline_invocations
    )

    report = aggregate_replay_results({"normal": [candidate_slice]}, {"normal": [baseline_slice]})

    delta_by_flag = {cell.candidate.flag_type: cell for cell in report.flag_rates.diff_cells or ()}
    assert "new_signal" in delta_by_flag
    assert delta_by_flag["new_signal"].candidate.rate == pytest.approx(1.0)
    assert delta_by_flag["new_signal"].baseline.rate == pytest.approx(0.0)
    assert delta_by_flag["new_signal"].delta_rate == pytest.approx(1.0)


def test_diff_mode_flag_only_under_baseline_appears_with_zero_candidate_rate() -> None:
    """A flag firing only under baseline appears in diff cells with candidate rate 0."""
    candidate_invocations = (_build_invocation(slice_id="s1", index=0, anomaly_flags=()),)
    baseline_invocations = (
        _build_invocation(slice_id="s1", index=0, anomaly_flags=(_flag("retired_signal"),)),
    )
    candidate_slice = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=candidate_invocations
    )
    baseline_slice = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=baseline_invocations
    )

    report = aggregate_replay_results({"normal": [candidate_slice]}, {"normal": [baseline_slice]})

    delta_by_flag = {cell.candidate.flag_type: cell for cell in report.flag_rates.diff_cells or ()}
    assert "retired_signal" in delta_by_flag
    assert delta_by_flag["retired_signal"].candidate.rate == pytest.approx(0.0)
    assert delta_by_flag["retired_signal"].baseline.rate == pytest.approx(1.0)
    assert delta_by_flag["retired_signal"].delta_rate == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Diff-mode input validation
# ---------------------------------------------------------------------------


def test_diff_mode_mismatched_regime_keys_raises() -> None:
    """Candidate covers ``normal``; baseline covers ``crisis`` — error names mismatch."""
    candidate_slice = _build_slice_result(
        slice_id="s1",
        exemplary_regime="normal",
        invocations=(_build_invocation(slice_id="s1", index=0),),
    )
    baseline_slice = _build_slice_result(
        slice_id="s1",
        exemplary_regime="crisis",
        invocations=(_build_invocation(slice_id="s1", index=0),),
    )

    with pytest.raises(AggregationInputError, match="regime"):
        aggregate_replay_results({"normal": [candidate_slice]}, {"crisis": [baseline_slice]})


def test_diff_mode_mismatched_slice_ids_within_regime_raises() -> None:
    """Candidate has slice ``s1``; baseline has slice ``s2`` in the same regime."""
    candidate_slice = _build_slice_result(
        slice_id="s1",
        exemplary_regime="normal",
        invocations=(_build_invocation(slice_id="s1", index=0),),
    )
    baseline_slice = _build_slice_result(
        slice_id="s2",
        exemplary_regime="normal",
        invocations=(_build_invocation(slice_id="s2", index=0),),
    )

    with pytest.raises(AggregationInputError, match="slice"):
        aggregate_replay_results({"normal": [candidate_slice]}, {"normal": [baseline_slice]})


def test_empty_candidate_dict_raises() -> None:
    """An empty candidate dict has nothing to aggregate — error names the failure."""
    with pytest.raises(AggregationInputError, match="candidate"):
        aggregate_replay_results({})


# ---------------------------------------------------------------------------
# Regime-label distribution
# ---------------------------------------------------------------------------


def test_regime_distribution_uses_per_invocation_classification() -> None:
    """5 invocations all classified ``vol_expansion`` — counter says ``{vol_expansion: 5}``.

    Crucially, the slice's exemplary regime is ``normal`` (the curation tag) — the
    distribution counts per-invocation classification, not the manifest tag.
    """
    invocations = tuple(
        _build_invocation(slice_id="s1", index=i, regime_label="vol_expansion") for i in range(5)
    )
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=invocations
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    assert len(report.regime_distribution.entries) == 1
    entry = report.regime_distribution.entries[0]
    assert entry.slice_id == "s1"
    assert entry.slice_exemplary_regime == "normal"
    assert entry.candidate_label_counts == {"vol_expansion": 5}
    assert entry.baseline_label_counts is None


def test_regime_distribution_counts_multiple_classified_labels() -> None:
    """A slice straddling a regime transition produces a multi-key counter."""
    invocations = (
        _build_invocation(slice_id="s1", index=0, regime_label="normal"),
        _build_invocation(slice_id="s1", index=1, regime_label="normal"),
        _build_invocation(slice_id="s1", index=2, regime_label="vol_expansion"),
        _build_invocation(slice_id="s1", index=3, regime_label="vol_expansion"),
        _build_invocation(slice_id="s1", index=4, regime_label="vol_expansion"),
    )
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=invocations
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    entry = report.regime_distribution.entries[0]
    assert entry.candidate_label_counts == {"normal": 2, "vol_expansion": 3}


def test_regime_distribution_orders_entries_by_regime_then_slice_id() -> None:
    """Entries sorted by ``(slice_exemplary_regime, slice_id)`` alphabetically."""
    inputs: dict[str, list[SliceReplayResult]] = {}
    for regime in ("normal", "crisis"):
        slices = []
        for slice_id in ("z_slice", "a_slice"):
            slices.append(
                _build_slice_result(
                    slice_id=slice_id,
                    exemplary_regime=regime,
                    invocations=(_build_invocation(slice_id=slice_id, index=0),),
                )
            )
        inputs[regime] = slices

    report = aggregate_replay_results(inputs)

    keys = [(e.slice_exemplary_regime, e.slice_id) for e in report.regime_distribution.entries]
    assert keys == [
        ("crisis", "a_slice"),
        ("crisis", "z_slice"),
        ("normal", "a_slice"),
        ("normal", "z_slice"),
    ]


def test_regime_distribution_in_diff_mode_includes_baseline_counts() -> None:
    """Diff mode populates ``baseline_label_counts`` per slice."""
    candidate_invocations = (
        _build_invocation(slice_id="s1", index=0, regime_label="vol_expansion"),
        _build_invocation(slice_id="s1", index=1, regime_label="vol_expansion"),
    )
    baseline_invocations = (
        _build_invocation(slice_id="s1", index=0, regime_label="normal"),
        _build_invocation(slice_id="s1", index=1, regime_label="vol_expansion"),
    )
    candidate_slice = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=candidate_invocations
    )
    baseline_slice = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=baseline_invocations
    )

    report = aggregate_replay_results({"normal": [candidate_slice]}, {"normal": [baseline_slice]})

    entry = report.regime_distribution.entries[0]
    assert entry.candidate_label_counts == {"vol_expansion": 2}
    assert entry.baseline_label_counts == {"normal": 1, "vol_expansion": 1}


# ---------------------------------------------------------------------------
# Class B baseline shape summary
# ---------------------------------------------------------------------------


def _baseline_value(
    *,
    baseline_kind: str,
    entity_key: str,
    value: float | None,
    calibration_state: str = "calibrated",
) -> BaselineValueRecord:
    return BaselineValueRecord(
        baseline_kind=baseline_kind,
        entity_key=entity_key,
        value=value,
        calibration_state=calibration_state,
    )


def test_baseline_shape_median_and_iqr_match_documented_interpolation() -> None:
    """A baseline kind with values [1..10] yields median=5.5, IQR=4.5.

    The aggregator documents ``numpy.percentile`` linear interpolation (the
    package default), which on [1..10] yields Q1=3.25 and Q3=7.75 so IQR=4.5.
    """
    invocations = (
        _build_invocation(
            slice_id="s1",
            index=0,
            baselines=tuple(
                _baseline_value(baseline_kind="volume", entity_key=f"T{i}", value=float(i))
                for i in range(1, 11)
            ),
        ),
    )
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=invocations
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    cells = {
        (cell.baseline_kind, cell.regime_label): cell
        for cell in report.baseline_shape.single_cells or ()
    }
    cell = cells[("volume", "normal")]
    assert cell.value_count == 10
    assert cell.value_median == pytest.approx(5.5)
    assert cell.value_iqr == pytest.approx(4.5)


def test_baseline_kind_with_zero_values_is_dropped() -> None:
    """A baseline kind that produces zero non-null values does not appear."""
    invocations = (
        _build_invocation(
            slice_id="s1",
            index=0,
            baselines=(
                _baseline_value(baseline_kind="volume", entity_key="T1", value=1.0),
                _baseline_value(
                    baseline_kind="event_history_earnings", entity_key="T1", value=None
                ),
                _baseline_value(
                    baseline_kind="event_history_earnings", entity_key="T2", value=None
                ),
            ),
        ),
    )
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=invocations
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    kinds = {cell.baseline_kind for cell in report.baseline_shape.single_cells or ()}
    assert "event_history_earnings" not in kinds
    assert "volume" in kinds


def test_baseline_shape_drops_none_values_from_count() -> None:
    """``value=None`` rows do not contribute to ``value_count`` or to median/IQR."""
    invocations = (
        _build_invocation(
            slice_id="s1",
            index=0,
            baselines=(
                _baseline_value(baseline_kind="volume", entity_key="T1", value=1.0),
                _baseline_value(baseline_kind="volume", entity_key="T2", value=None),
                _baseline_value(baseline_kind="volume", entity_key="T3", value=3.0),
            ),
        ),
    )
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=invocations
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    cells = {cell.baseline_kind: cell for cell in report.baseline_shape.single_cells or ()}
    assert cells["volume"].value_count == 2


def test_baseline_shape_diff_mode_emits_candidate_and_baseline() -> None:
    """Diff mode populates ``diff_cells`` with paired summaries."""
    candidate_invocations = (
        _build_invocation(
            slice_id="s1",
            index=0,
            baselines=(
                _baseline_value(baseline_kind="volume", entity_key="T1", value=10.0),
                _baseline_value(baseline_kind="volume", entity_key="T2", value=20.0),
            ),
        ),
    )
    baseline_invocations = (
        _build_invocation(
            slice_id="s1",
            index=0,
            baselines=(
                _baseline_value(baseline_kind="volume", entity_key="T1", value=5.0),
                _baseline_value(baseline_kind="volume", entity_key="T2", value=15.0),
            ),
        ),
    )
    candidate_slice = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=candidate_invocations
    )
    baseline_slice = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=baseline_invocations
    )

    report = aggregate_replay_results({"normal": [candidate_slice]}, {"normal": [baseline_slice]})

    assert report.baseline_shape.mode == "diff"
    assert report.baseline_shape.single_cells is None
    assert report.baseline_shape.diff_cells is not None
    cells = {
        (d.candidate.baseline_kind, d.candidate.regime_label): d
        for d in report.baseline_shape.diff_cells
    }
    delta = cells[("volume", "normal")]
    assert delta.candidate.value_median == pytest.approx(15.0)
    assert delta.baseline.value_median == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Calibration-state breakdown
# ---------------------------------------------------------------------------


def test_calibration_breakdown_counts_observed_states() -> None:
    """A fixture with 80 calibrated, 15 bootstrap, 5 unavailable outputs in one regime.

    Each invocation contributes exactly one anomaly flag with the given calibration
    state, so the per-regime totals match the fixture counts directly.
    """
    invocations: list[InvocationOutputs] = []
    for i in range(80):
        invocations.append(
            _build_invocation(
                slice_id="s1",
                index=i,
                anomaly_flags=(_flag("volume_anomaly", calibration_state="calibrated"),),
            )
        )
    for i in range(80, 95):
        invocations.append(
            _build_invocation(
                slice_id="s1",
                index=i,
                anomaly_flags=(_flag("volume_anomaly", calibration_state="bootstrap"),),
            )
        )
    for i in range(95, 100):
        invocations.append(
            _build_invocation(
                slice_id="s1",
                index=i,
                anomaly_flags=(_flag("volume_anomaly", calibration_state="unavailable"),),
            )
        )
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=tuple(invocations)
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    by_state = {
        (e.regime_label, e.calibration_state): e for e in report.calibration_breakdown.entries
    }
    assert by_state[("normal", "calibrated")].candidate_count == 80
    assert by_state[("normal", "bootstrap")].candidate_count == 15
    assert by_state[("normal", "unavailable")].candidate_count == 5
    assert by_state[("normal", "calibrated")].baseline_count is None


def test_calibration_breakdown_counts_baseline_values_too() -> None:
    """Calibration breakdown counts BOTH anomaly flags AND Class B baseline values.

    Per design: "Count of outputs tagged calibrated/bootstrap/unavailable" — the
    word "outputs" includes both flag emissions and baseline emissions.
    """
    invocations = (
        _build_invocation(
            slice_id="s1",
            index=0,
            anomaly_flags=(_flag("volume_anomaly", calibration_state="calibrated"),),
            baselines=(
                _baseline_value(
                    baseline_kind="volume",
                    entity_key="T1",
                    value=1.0,
                    calibration_state="bootstrap",
                ),
                _baseline_value(
                    baseline_kind="volume",
                    entity_key="T2",
                    value=2.0,
                    calibration_state="bootstrap",
                ),
            ),
        ),
    )
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=invocations
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    by_state = {
        (e.regime_label, e.calibration_state): e for e in report.calibration_breakdown.entries
    }
    assert by_state[("normal", "calibrated")].candidate_count == 1
    assert by_state[("normal", "bootstrap")].candidate_count == 2


def test_calibration_breakdown_orders_entries_alphabetically() -> None:
    """Entries sorted by ``(regime_label, calibration_state)``."""
    invocations: list[InvocationOutputs] = []
    for state in ("unavailable", "bootstrap", "calibrated"):
        invocations.append(
            _build_invocation(
                slice_id="s1",
                index=len(invocations),
                anomaly_flags=(_flag("volume_anomaly", calibration_state=state),),
            )
        )
    inputs = {
        "normal": [
            _build_slice_result(
                slice_id="s1", exemplary_regime="normal", invocations=tuple(invocations)
            )
        ],
        "crisis": [
            _build_slice_result(
                slice_id="s1",
                exemplary_regime="crisis",
                invocations=tuple(
                    _build_invocation(
                        slice_id="s1",
                        index=i,
                        anomaly_flags=(_flag("volume_anomaly", calibration_state="calibrated"),),
                    )
                    for i in range(2)
                ),
            )
        ],
    }

    report = aggregate_replay_results(inputs)

    keys = [(e.regime_label, e.calibration_state) for e in report.calibration_breakdown.entries]
    assert keys == [
        ("crisis", "calibrated"),
        ("normal", "bootstrap"),
        ("normal", "calibrated"),
        ("normal", "unavailable"),
    ]


def test_calibration_breakdown_in_diff_mode_includes_baseline_count() -> None:
    """Diff mode populates ``baseline_count`` per cell."""
    candidate_invocations = (
        _build_invocation(
            slice_id="s1",
            index=0,
            anomaly_flags=(_flag("volume_anomaly", calibration_state="calibrated"),),
        ),
    )
    baseline_invocations = (
        _build_invocation(
            slice_id="s1",
            index=0,
            anomaly_flags=(_flag("volume_anomaly", calibration_state="bootstrap"),),
        ),
    )
    candidate_slice = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=candidate_invocations
    )
    baseline_slice = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=baseline_invocations
    )

    report = aggregate_replay_results({"normal": [candidate_slice]}, {"normal": [baseline_slice]})

    by_state = {
        (e.regime_label, e.calibration_state): e for e in report.calibration_breakdown.entries
    }
    assert by_state[("normal", "calibrated")].candidate_count == 1
    assert by_state[("normal", "calibrated")].baseline_count == 0
    assert by_state[("normal", "bootstrap")].candidate_count == 0
    assert by_state[("normal", "bootstrap")].baseline_count == 1


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_aggregate_is_deterministic_across_calls() -> None:
    """Two runs on the same inputs produce equal ``AggregatedReport`` (asdict)."""
    invocations = (
        _build_invocation(
            slice_id="s1",
            index=0,
            anomaly_flags=(_flag("volume_anomaly"), _flag("gap_anomaly")),
            baselines=(
                _baseline_value(baseline_kind="volume", entity_key="T1", value=1.0),
                _baseline_value(baseline_kind="volume", entity_key="T2", value=2.0),
            ),
        ),
        _build_invocation(
            slice_id="s1",
            index=1,
            anomaly_flags=(_flag("iv_skew"),),
            baselines=(_baseline_value(baseline_kind="pair_lag", entity_key="HYG_SPY", value=1.5),),
        ),
    )
    inputs = {
        "normal": [
            _build_slice_result(slice_id="s1", exemplary_regime="normal", invocations=invocations)
        ],
        "crisis": [
            _build_slice_result(
                slice_id="s2",
                exemplary_regime="crisis",
                invocations=(
                    _build_invocation(
                        slice_id="s2",
                        index=0,
                        anomaly_flags=(_flag("volume_anomaly"),),
                    ),
                ),
            )
        ],
    }

    first = aggregate_replay_results(inputs)
    second = aggregate_replay_results(inputs)

    assert dataclasses.asdict(first) == dataclasses.asdict(second)


def test_aggregate_input_dict_iteration_order_does_not_affect_output() -> None:
    """Reordering the input dict's keys does not change the report."""
    base_invocation = _build_invocation(
        slice_id="s1", index=0, anomaly_flags=(_flag("volume_anomaly"),)
    )
    inputs_a = {
        "normal": [
            _build_slice_result(
                slice_id="s1", exemplary_regime="normal", invocations=(base_invocation,)
            )
        ],
        "crisis": [
            _build_slice_result(
                slice_id="s2",
                exemplary_regime="crisis",
                invocations=(_build_invocation(slice_id="s2", index=0),),
            )
        ],
    }
    inputs_b = dict(reversed(inputs_a.items()))

    assert dataclasses.asdict(aggregate_replay_results(inputs_a)) == dataclasses.asdict(
        aggregate_replay_results(inputs_b)
    )


def test_flag_types_and_baseline_kinds_sorted_alphabetically() -> None:
    """``flag_types`` and baseline-shape cells are returned in alphabetical order."""
    invocations = (
        _build_invocation(
            slice_id="s1",
            index=0,
            anomaly_flags=(
                _flag("zoo_signal"),
                _flag("alpha_signal"),
                _flag("middle_signal"),
            ),
            baselines=(
                _baseline_value(baseline_kind="zeta_kind", entity_key="T1", value=1.0),
                _baseline_value(baseline_kind="alpha_kind", entity_key="T2", value=2.0),
            ),
        ),
    )
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=invocations
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    assert report.flag_rates.flag_types == ("alpha_signal", "middle_signal", "zoo_signal")
    kinds = [c.baseline_kind for c in report.baseline_shape.single_cells or ()]
    assert kinds == sorted(kinds)
    assert kinds == ["alpha_kind", "zeta_kind"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_regime_with_zero_slices_is_dropped_from_aggregations() -> None:
    """A regime entry with an empty slices list does not appear in any cell."""
    invocations = (_build_invocation(slice_id="s1", index=0, anomaly_flags=(_flag("v"),)),)
    inputs = {
        "normal": [
            _build_slice_result(slice_id="s1", exemplary_regime="normal", invocations=invocations)
        ],
        "crisis": [],
    }

    report = aggregate_replay_results(inputs)

    assert "crisis" not in report.flag_rates.regimes
    assert all(c.regime_label != "crisis" for c in report.flag_rates.single_cells or ())
    assert all(e.slice_exemplary_regime != "crisis" for e in report.regime_distribution.entries)
    assert all(c.regime_label != "crisis" for c in report.baseline_shape.single_cells or ())
    assert all(e.regime_label != "crisis" for e in report.calibration_breakdown.entries)


def test_flag_with_zero_observations_does_not_appear() -> None:
    """A flag that never fires across any slice does not appear in the table."""
    invocations = (_build_invocation(slice_id="s1", index=0, anomaly_flags=()),)
    slice_result = _build_slice_result(
        slice_id="s1", exemplary_regime="normal", invocations=invocations
    )

    report = aggregate_replay_results({"normal": [slice_result]})

    assert report.flag_rates.flag_types == ()
    assert report.flag_rates.single_cells == ()
