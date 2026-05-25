"""Tests for the calibration-state snapshot writer — story 02-distillation/17.

These tests exercise :func:`write_calibration_state_snapshot` against
hand-built :class:`DistillationOutputs` fixtures. The writer reduces every
:class:`OutputBlock` carried by the orchestrator's outputs into the per-
invocation JSON file documented in
``docs/design/02-distillation-layer/threshold-calibration.md``
§ Calibration-state snapshot file.

Coverage map per the story scope:

- End-to-end: a fixture with mixed states across audiences and block kinds
  produces a JSON file matching the documented schema.
- Determinism: two writer invocations on the same fixture produce
  byte-identical files.
- Empty-input: zero-block fixture produces ``total_blocks = 0`` and empty
  by-* dicts.
- Bootstrap-reason capture and unavailable-reason capture.
- ``schema_version`` field is present and equal to ``"1"``.
- Directory creation: the writer creates missing parents.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.calibration_snapshot import (
    OPERATOR_SUMMARY_SCHEMA_VERSION,
    write_calibration_state_snapshot,
    write_operator_data_health_summary,
)
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import OutputAudience, OutputBlock
from alphamind.distillation.sector_assembly import SectorOutput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _block(
    *,
    block_id: str,
    audience: frozenset[OutputAudience],
    state: CalibrationState,
    bootstrap_reason: str | None = None,
    payload: dict[str, Any] | None = None,
) -> OutputBlock:
    return OutputBlock(
        block_id=block_id,
        audience=audience,
        freshness_ts=_AS_OF,
        calibration_state=state,
        bootstrap_reason=bootstrap_reason,
        payload=payload or {},
        anomaly_flags=(),
        regime_context=None,
    )


def _empty_sector_output(audience: OutputAudience, label: str) -> SectorOutput:
    return SectorOutput(
        audience=audience,
        sector_label=label,
        text="",
        tickers=(),
        block_ids=(),
        freshness_min=_AS_OF,
    )


def _empty_brief() -> CorrelationRegimeBrief:
    return CorrelationRegimeBrief(
        text="",
        reference_index={},
        freshness_min=_AS_OF,
    )


def _build_outputs(
    *,
    invocation_id: str = "20260425T120000Z-test",
    blocks: tuple[OutputBlock, ...] = (),
) -> DistillationOutputs:
    """Build a minimal :class:`DistillationOutputs` carrying ``blocks``.

    The other fields are populated with empty placeholders sufficient for
    the snapshot writer; the writer only inspects the block list.
    """
    sector_outputs = {
        OutputAudience.SECTOR_TECH_SEMIS: _empty_sector_output(
            OutputAudience.SECTOR_TECH_SEMIS, "Tech / Semis"
        ),
        OutputAudience.SECTOR_FINANCIALS: _empty_sector_output(
            OutputAudience.SECTOR_FINANCIALS, "Financials"
        ),
        OutputAudience.SECTOR_ENERGY: _empty_sector_output(OutputAudience.SECTOR_ENERGY, "Energy"),
    }
    return DistillationOutputs(
        sector_outputs=sector_outputs,
        correlation_regime_brief=_empty_brief(),
        universal_regime_label={},
        invocation_id=invocation_id,
        as_of=_AS_OF,
        total_blocks=len(blocks),
        total_anomalies=0,
        non_calibrated_block_count=sum(
            1 for block in blocks if block.calibration_state is not CalibrationState.CALIBRATED
        ),
        all_blocks=blocks,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_writer_emits_schema_version_two(tmp_path: Path) -> None:
    """Tracer bullet — the writer produces a JSON file whose ``schema_version`` equals ``"2"``.

    Bumped from ``"1"`` in ALP-540 because the ``by_state`` keyspace now
    carries ``accumulating`` instead of ``bootstrap`` and the per-block
    reason map was renamed ``accumulating_reasons``.
    """
    outputs = _build_outputs(blocks=())
    path = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="20260425T120000Z-test",
        base_path=tmp_path,
    )

    expected_path = (
        tmp_path / "invocations" / "20260425T120000Z-test" / "data_calibration_state.json"
    )
    assert path == expected_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "2"


def test_writer_creates_invocation_directory(tmp_path: Path) -> None:
    """Directory creation: the writer mkdirs the per-invocation subdirectory if missing."""
    base_path = tmp_path / "fresh"
    assert not base_path.exists()

    outputs = _build_outputs(blocks=())
    path = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="inv-id",
        base_path=base_path,
    )

    assert path.exists()
    assert path.parent.is_dir()


def test_writer_empty_input_produces_zero_counts(tmp_path: Path) -> None:
    """A fixture with no blocks produces ``total_blocks = 0`` and empty by-* dicts."""
    outputs = _build_outputs(blocks=())
    path = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="inv-id",
        base_path=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["summary"]["total_blocks"] == 0
    assert payload["summary"]["by_state"] == {
        "calibrated": 0,
        "accumulating": 0,
        "unavailable": 0,
    }
    assert payload["summary"]["by_audience"] == {}
    assert payload["summary"]["by_block_kind"] == {}
    assert payload["accumulating_reasons"] == {}
    assert payload["unavailable_reasons"] == {}


def test_writer_aggregates_per_state_counts(tmp_path: Path) -> None:
    """``by_state`` counts every block once, partitioned by ``calibration_state``."""
    blocks = (
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.CALIBRATED,
        ),
        _block(
            block_id="q1.price_move_anomaly",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.CALIBRATED,
        ),
        _block(
            block_id="q3.options_flow",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="iv_rank: 4 < 30",
        ),
        _block(
            block_id="q12.recent_corporate_actions",
            audience=frozenset({OutputAudience.SECTOR_ENERGY}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason="event_history: 0 < 3 (cross-sectional pool empty)",
        ),
    )
    outputs = _build_outputs(blocks=blocks)
    path = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="inv-id",
        base_path=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["summary"]["total_blocks"] == 4
    assert payload["summary"]["by_state"] == {
        "calibrated": 2,
        "accumulating": 1,
        "unavailable": 1,
    }


def test_writer_aggregates_per_audience_counts(tmp_path: Path) -> None:
    """``by_audience`` counts each (audience, block) pair; multi-audience blocks count twice."""
    blocks = (
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.CALIBRATED,
        ),
        _block(
            block_id="qual.sentiment_percentile",
            audience=frozenset(
                {OutputAudience.SECTOR_TECH_SEMIS, OutputAudience.UNIVERSAL_BROADCAST}
            ),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="sentiment: 2 < 5",
        ),
    )
    outputs = _build_outputs(blocks=blocks)
    path = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="inv-id",
        base_path=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["summary"]["by_audience"] == {
        "sector_tech_semis": {"calibrated": 1, "accumulating": 1, "unavailable": 0},
        "universal_broadcast": {"calibrated": 0, "accumulating": 1, "unavailable": 0},
    }


def test_writer_aggregates_per_block_kind_counts(tmp_path: Path) -> None:
    """``by_block_kind`` keys are block_ids; counts span the calibration states."""
    blocks = (
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.CALIBRATED,
        ),
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="volume_baseline: 12 < 20",
        ),
        _block(
            block_id="regime.label",
            audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
            state=CalibrationState.CALIBRATED,
        ),
    )
    outputs = _build_outputs(blocks=blocks)
    path = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="inv-id",
        base_path=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["summary"]["by_block_kind"] == {
        "q1.volume_anomaly": {"calibrated": 1, "accumulating": 1, "unavailable": 0},
        "regime.label": {"calibrated": 1, "accumulating": 0, "unavailable": 0},
    }


def test_writer_captures_bootstrap_reasons(tmp_path: Path) -> None:
    """Bootstrap blocks produce a ``bootstrap_reasons`` map keyed by ``block_id``."""
    blocks = (
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="volume_baseline: 12 < 20",
        ),
        _block(
            block_id="qual.sentiment_percentile",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="sentiment: 2 < 5",
        ),
        _block(
            block_id="q3.options_flow",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.CALIBRATED,
        ),
    )
    outputs = _build_outputs(blocks=blocks)
    path = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="inv-id",
        base_path=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["accumulating_reasons"] == {
        "q1.volume_anomaly": "volume_baseline: 12 < 20",
        "qual.sentiment_percentile": "sentiment: 2 < 5",
    }
    # Calibrated blocks must not appear.
    assert "q3.options_flow" not in payload["accumulating_reasons"]
    assert payload["unavailable_reasons"] == {}


def test_writer_captures_unavailable_reasons(tmp_path: Path) -> None:
    """Unavailable blocks produce an ``unavailable_reasons`` map; bootstrap blocks stay separate."""
    blocks = (
        _block(
            block_id="q12.event_novelty",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason="event_history: 0 < 3 (cross-sectional pool empty)",
        ),
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="volume_baseline: 12 < 20",
        ),
    )
    outputs = _build_outputs(blocks=blocks)
    path = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="inv-id",
        base_path=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["unavailable_reasons"] == {
        "q12.event_novelty": "event_history: 0 < 3 (cross-sectional pool empty)",
    }
    assert payload["accumulating_reasons"] == {
        "q1.volume_anomaly": "volume_baseline: 12 < 20",
    }


def test_writer_records_invocation_metadata(tmp_path: Path) -> None:
    """The snapshot records the invocation id and as-of timestamp from ``DistillationOutputs``."""
    outputs = _build_outputs(invocation_id="20260425T120000Z-real", blocks=())
    path = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="20260425T120000Z-real",
        base_path=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["invocation_id"] == "20260425T120000Z-real"
    # ``as_of`` is the ISO-8601 UTC formatted timestamp from outputs.as_of.
    assert payload["as_of"] == "2026-04-25T12:00:00Z"


def test_writer_is_deterministic(tmp_path: Path) -> None:
    """Two writer calls on the same fixture produce byte-identical files."""
    blocks = (
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.CALIBRATED,
        ),
        _block(
            block_id="q3.options_flow",
            audience=frozenset(
                {OutputAudience.SECTOR_FINANCIALS, OutputAudience.UNIVERSAL_BROADCAST}
            ),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="iv_rank: 4 < 30",
        ),
        _block(
            block_id="qual.sentiment_percentile",
            audience=frozenset({OutputAudience.SECTOR_ENERGY}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason="sentiment: 0 < 5 (cross-sectional pool empty)",
        ),
    )
    outputs = _build_outputs(blocks=blocks)

    path_a = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="inv-a",
        base_path=tmp_path / "run_a",
    )
    path_b = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="inv-a",
        base_path=tmp_path / "run_b",
    )

    assert path_a.read_bytes() == path_b.read_bytes()


def test_writer_end_to_end_schema_shape(tmp_path: Path) -> None:
    """End-to-end: a mixed fixture produces a JSON document matching the documented schema."""
    blocks = (
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.CALIBRATED,
        ),
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="volume_baseline: 12 < 20",
        ),
        _block(
            block_id="q3.options_flow",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason="iv_rank: 0 < 30 (cross-sectional pool empty)",
        ),
        _block(
            block_id="regime.label",
            audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
            state=CalibrationState.CALIBRATED,
        ),
    )
    outputs = _build_outputs(blocks=blocks)
    path = write_calibration_state_snapshot(
        outputs=outputs,
        invocation_id="20260425T120000Z-test",
        base_path=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    # Top-level keys match the documented schema.
    assert set(payload) == {
        "schema_version",
        "invocation_id",
        "as_of",
        "summary",
        "accumulating_reasons",
        "unavailable_reasons",
    }
    # Summary structure.
    assert set(payload["summary"]) == {
        "total_blocks",
        "by_state",
        "by_audience",
        "by_block_kind",
    }
    assert payload["summary"]["total_blocks"] == 4
    # Per-state vocabulary matches the CalibrationState enum.
    assert set(payload["summary"]["by_state"]) == {
        "calibrated",
        "accumulating",
        "unavailable",
    }
    # Reason maps populated for non-calibrated blocks only.
    assert payload["accumulating_reasons"] == {
        "q1.volume_anomaly": "volume_baseline: 12 < 20",
    }
    assert payload["unavailable_reasons"] == {
        "q3.options_flow": "iv_rank: 0 < 30 (cross-sectional pool empty)",
    }


# ---------------------------------------------------------------------------
# Operator data-health summary writer (ALP-540 Layer 2)
# ---------------------------------------------------------------------------


def test_operator_summary_empty_blocks(tmp_path: Path) -> None:
    """Empty fixture produces a summary with all zeros and empty per-state lists."""
    outputs = _build_outputs(blocks=())
    path = write_operator_data_health_summary(
        outputs=outputs,
        invocation_id="inv-id",
        archive_root=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == OPERATOR_SUMMARY_SCHEMA_VERSION
    assert payload["summary"] == {"calibrated": 0, "accumulating": 0, "unavailable": 0}
    assert payload["unavailable"] == []
    assert payload["accumulating"] == []


def test_operator_summary_mixed_state_blocks(tmp_path: Path) -> None:
    """Per-state lists carry one entry per non-calibrated block, block-id-sorted."""
    blocks = (
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.CALIBRATED,
        ),
        _block(
            block_id="q6.dollar_attribution",
            audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason="dollar_attribution: DTWEXBGS history unavailable",
        ),
        _block(
            block_id="q6.funding_stress",
            audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="funding_stress_min_observations: 11 < 60",
        ),
        # Second unavailable block — order-stability assertion below.
        _block(
            block_id="q7.intermarket_regime.gld_real_yields",
            audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason="gld_real_yields_observations: 0 < 60 (0 observations)",
        ),
    )
    outputs = _build_outputs(blocks=blocks)
    path = write_operator_data_health_summary(
        outputs=outputs,
        invocation_id="inv-id",
        archive_root=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["summary"] == {"calibrated": 1, "accumulating": 1, "unavailable": 2}
    assert payload["unavailable"] == [
        {
            "module": "q6.dollar_attribution",
            "reason": "dollar_attribution: DTWEXBGS history unavailable",
        },
        {
            "module": "q7.intermarket_regime.gld_real_yields",
            "reason": "gld_real_yields_observations: 0 < 60 (0 observations)",
        },
    ]
    assert payload["accumulating"] == [
        {
            "module": "q6.funding_stress",
            "reason": "funding_stress_min_observations: 11 < 60",
        },
    ]


def test_operator_summary_dedupes_identical_module_reason_entries(tmp_path: Path) -> None:
    """Per-sector blocks sharing a module + reason collapse to one operator-facing entry.

    ALP-573: q1.gap emits one OutputBlock per sector (tech_semis, financials,
    energy). When all three carry the same bootstrap_reason — e.g. the
    bootstrap-window `gap_fill_min_events: 0 < 30 (0 observations)` — the
    operator sees the same line three times. The operator-facing summary
    dedupes on `(module, reason)`; the internal `data_calibration_state`
    snapshot keeps per-block detail.
    """
    reason = "gap_fill_min_events: 0 < 30 (0 observations)"
    blocks = (
        _block(
            block_id="q1.gap",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=reason,
        ),
        _block(
            block_id="q1.gap",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=reason,
        ),
        _block(
            block_id="q1.gap",
            audience=frozenset({OutputAudience.SECTOR_ENERGY}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=reason,
        ),
    )
    outputs = _build_outputs(blocks=blocks)
    path = write_operator_data_health_summary(
        outputs=outputs,
        invocation_id="inv-id",
        archive_root=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["unavailable"] == [{"module": "q1.gap", "reason": reason}]


def test_operator_summary_keeps_distinct_reasons_for_same_module(tmp_path: Path) -> None:
    """Same module with distinct reasons stays as separate entries (no over-dedup).

    Dedup keys on the full `(module, reason)` tuple — a per-ticker baseline
    reason like `baseline_days: ... for AAPL` differs from
    `baseline_days: ... for MSFT` and both should survive.
    """
    blocks = (
        _block(
            block_id="q1.technicals",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="baseline_days: 12 < 20 for AAPL",
        ),
        _block(
            block_id="q1.technicals",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="baseline_days: 5 < 20 for JPM",
        ),
    )
    outputs = _build_outputs(blocks=blocks)
    path = write_operator_data_health_summary(
        outputs=outputs,
        invocation_id="inv-id",
        archive_root=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["accumulating"] == [
        {"module": "q1.technicals", "reason": "baseline_days: 12 < 20 for AAPL"},
        {"module": "q1.technicals", "reason": "baseline_days: 5 < 20 for JPM"},
    ]


def test_operator_summary_counts_match_per_state_array_lengths(tmp_path: Path) -> None:
    """ALP-631: ``summary`` counts equal the lengths of the per-state arrays.

    Pre-fix the counts came from ``_aggregate_by_state`` (one tick per
    ``OutputBlock``, i.e. per sector audience) while the arrays were deduped
    on ``(module, reason)``, so any sector-replicated module/reason pair
    drove the count above the array length. The operator-facing summary
    must report the same shape as the lists it ships.
    """
    reason = "gap_fill_min_events: 0 < 30 (0 observations)"
    accumulating_reason = "volume_baseline: 12 < 20"
    blocks = (
        # Three sector-replicas of the same unavailable module/reason — count
        # of 3 vs array length of 1 was exactly the pre-fix divergence.
        _block(
            block_id="q1.gap",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=reason,
        ),
        _block(
            block_id="q1.gap",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=reason,
        ),
        _block(
            block_id="q1.gap",
            audience=frozenset({OutputAudience.SECTOR_ENERGY}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=reason,
        ),
        # Two sector-replicas of the same accumulating module/reason.
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason=accumulating_reason,
        ),
        _block(
            block_id="q1.volume_anomaly",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason=accumulating_reason,
        ),
        # Two sector-replicas of a calibrated module — calibrated count
        # must dedupe the same way to stay consistent.
        _block(
            block_id="q1.technicals",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            state=CalibrationState.CALIBRATED,
        ),
        _block(
            block_id="q1.technicals",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            state=CalibrationState.CALIBRATED,
        ),
    )
    outputs = _build_outputs(blocks=blocks)
    path = write_operator_data_health_summary(
        outputs=outputs, invocation_id="inv-id", archive_root=tmp_path
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["summary"]["unavailable"] == len(payload["unavailable"])
    assert payload["summary"]["accumulating"] == len(payload["accumulating"])
    assert payload["summary"]["unavailable"] == 1
    assert payload["summary"]["accumulating"] == 1
    # Calibrated has no per-state array; dedupe distinct module ids.
    assert payload["summary"]["calibrated"] == 1


def test_operator_summary_writes_to_archive_root_invocation_path(tmp_path: Path) -> None:
    """The operator summary lands at <archive_root>/invocations/<id>/data_calibration_state.json."""
    outputs = _build_outputs(blocks=())
    path = write_operator_data_health_summary(
        outputs=outputs,
        invocation_id="inv-XYZ",
        archive_root=tmp_path,
    )
    assert path == tmp_path / "invocations" / "inv-XYZ" / "data_calibration_state.json"
    assert path.exists()


def test_operator_summary_is_deterministic(tmp_path: Path) -> None:
    """Two writer calls on the same fixture produce byte-identical files."""
    blocks = (
        _block(
            block_id="q6.dollar_attribution",
            audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason="DTWEXBGS unavailable",
        ),
        _block(
            block_id="q6.funding_stress",
            audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
            state=CalibrationState.ACCUMULATING,
            bootstrap_reason="11 < 60",
        ),
    )
    outputs = _build_outputs(blocks=blocks)

    path_a = write_operator_data_health_summary(
        outputs=outputs, invocation_id="inv-id", archive_root=tmp_path / "a"
    )
    path_b = write_operator_data_health_summary(
        outputs=outputs, invocation_id="inv-id", archive_root=tmp_path / "b"
    )
    assert path_a.read_bytes() == path_b.read_bytes()
