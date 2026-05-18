"""Tests for the per-block output envelope and structured-text formatter — story 02-distillation/05.

Cover the audience enum, the OutputBlock and AnomalyFlag dataclasses, the
``format_block`` deterministic renderer, and the ``format_blocks_for_audience``
filter+sort helper.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
    format_block,
    format_blocks_for_audience,
)


def test_output_audience_has_five_partition_targets() -> None:
    """The five members map 1:1 to the partition targets in external.md § Output format."""
    members = {member.name for member in OutputAudience}
    assert members == {
        "SECTOR_TECH_SEMIS",
        "SECTOR_FINANCIALS",
        "SECTOR_ENERGY",
        "CORRELATION_REGIME_BRIEF",
        "UNIVERSAL_BROADCAST",
    }


def test_anomaly_flag_is_frozen_with_documented_fields() -> None:
    """AnomalyFlag is a frozen dataclass with name/magnitude/severity."""
    flag = AnomalyFlag(name="volume_spike", magnitude=2.5, severity="investigate_now")
    assert flag.name == "volume_spike"
    assert flag.magnitude == 2.5
    assert flag.severity == "investigate_now"
    with pytest.raises(dataclasses.FrozenInstanceError):
        flag.name = "other"  # type: ignore[misc]


def _make_block(
    *,
    audience: frozenset[OutputAudience] = frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
    block_id: str = "q1.volume_anomaly",
    calibration_state: CalibrationState = CalibrationState.CALIBRATED,
    bootstrap_reason: str | None = None,
    payload: dict[str, object] | None = None,
    anomaly_flags: tuple[AnomalyFlag, ...] = (),
    regime_context: str | None = None,
) -> OutputBlock:
    """Construct an OutputBlock with sensible defaults for tests."""
    return OutputBlock(
        block_id=block_id,
        audience=audience,
        freshness_ts=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        calibration_state=calibration_state,
        bootstrap_reason=bootstrap_reason,
        payload=payload if payload is not None else {"ticker": "NVDA", "z_score": 2.5},
        anomaly_flags=anomaly_flags,
        regime_context=regime_context,
    )


def test_output_block_rejects_empty_audience() -> None:
    """Every block must declare at least one consumer per the envelope contract."""
    with pytest.raises(ValueError, match="audience"):
        _make_block(audience=frozenset())


def test_format_block_is_byte_identical_on_repeated_calls() -> None:
    """The renderer is deterministic — required for clean invocation-archive diffs.

    Mixed key order and a nested dict in the payload exercise the
    sorted-iteration contract; a float exercises the fixed-precision rule.
    """
    block = _make_block(
        payload={
            "z_score": 2.5,
            "ticker": "NVDA",
            "context": {"sector": "tech_semis", "atr": 4.875},
        },
        anomaly_flags=(
            AnomalyFlag(name="volume_spike", magnitude=3.14159, severity="investigate_now"),
        ),
    )
    rendered_first = format_block(block)
    rendered_second = format_block(block)
    assert rendered_first == rendered_second
    # Sanity: compact single-line heading is present and starts the output.
    assert rendered_first.startswith("### q1.volume_anomaly | ")


def test_format_block_omits_reason_when_calibrated() -> None:
    """No reason segment when state is CALIBRATED."""
    block = _make_block(calibration_state=CalibrationState.CALIBRATED)
    rendered = format_block(block)
    assert "reason" not in rendered
    assert "| calibrated" in rendered


def test_format_block_includes_reason_when_accumulating() -> None:
    """The reason segment appears when state is ACCUMULATING."""
    block = _make_block(
        calibration_state=CalibrationState.ACCUMULATING,
        bootstrap_reason="volume_baseline: 5 < 20",
    )
    rendered = format_block(block)
    assert "| accumulating — reason: volume_baseline: 5 < 20" in rendered


def test_format_block_includes_reason_when_unavailable() -> None:
    """The reason segment appears when state is UNAVAILABLE."""
    block = _make_block(
        calibration_state=CalibrationState.UNAVAILABLE,
        bootstrap_reason="volume_baseline: 0 < 20 (cross-sectional pool empty)",
    )
    rendered = format_block(block)
    assert "| unavailable — reason:" in rendered
    assert "(cross-sectional pool empty)" in rendered


def test_format_block_omits_regime_segment_when_regime_context_is_none() -> None:
    """The regime segment appears only on blocks where regime is load-bearing."""
    block = _make_block(regime_context=None)
    rendered = format_block(block)
    assert "regime:" not in rendered


def test_format_block_includes_regime_segment_when_regime_context_is_set() -> None:
    """When regime_context is supplied the regime segment is rendered."""
    block = _make_block(regime_context="low_vol_compression (early-strong)")
    rendered = format_block(block)
    assert "| regime: low_vol_compression (early-strong)" in rendered


def test_format_block_omits_anomaly_trailer_when_no_flags() -> None:
    """Empty per-block anomaly trailer is omitted; universal summary lists flags upstream."""
    block = _make_block(anomaly_flags=())
    rendered = format_block(block)
    assert "Anomaly flags" not in rendered


def test_format_per_ticker_renders_compact_one_line_per_ticker() -> None:
    """The per_ticker key collapses to one row per ticker for ALP-272 compactness."""
    block = _make_block(
        payload={
            "per_ticker": {
                "NVDA": {"rsi": 60.0, "macd_state": "bullish"},
                "AAPL": {"rsi": 55.0, "macd_state": "bearish"},
            },
        },
    )
    rendered = format_block(block)
    assert "per_ticker:" in rendered
    assert "AAPL macd_state=bearish rsi=55" in rendered
    assert "NVDA macd_state=bullish rsi=60" in rendered
    assert "  AAPL:" not in rendered
    assert "  NVDA:" not in rendered


def test_format_blocks_for_audience_filters_and_orders_by_block_id() -> None:
    """Filter to membership in audience, then sort by block_id ascending."""
    # Construct a multi-audience fixture. The blocks intentionally arrive in a
    # non-sorted order so the sort step is exercised. The fin_only block is
    # filtered out for the tech audience; for tech the expected blocks render
    # in q1 < q7 < qual order regardless of input order.
    tech_qual_block = _make_block(
        block_id="qual.headline_cluster",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
    )
    tech_q1_block = _make_block(
        block_id="q1.volume_anomaly",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS, OutputAudience.UNIVERSAL_BROADCAST}),
    )
    tech_q7_block = _make_block(
        block_id="q7.cross_correlation",
        audience=frozenset(
            {OutputAudience.SECTOR_TECH_SEMIS, OutputAudience.CORRELATION_REGIME_BRIEF}
        ),
    )
    fin_only_block = _make_block(
        block_id="q1.volume_anomaly_fin",
        audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
    )
    blocks = [tech_qual_block, fin_only_block, tech_q7_block, tech_q1_block]

    rendered = format_blocks_for_audience(blocks, OutputAudience.SECTOR_TECH_SEMIS)
    expected = (
        format_block(tech_q1_block) + format_block(tech_q7_block) + format_block(tech_qual_block)
    )
    assert rendered == expected
    assert "q1.volume_anomaly_fin" not in rendered


def test_format_blocks_for_audience_returns_empty_string_when_no_matches() -> None:
    """Audience with no matching blocks renders to empty string deterministically."""
    block = _make_block(audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}))
    rendered = format_blocks_for_audience([block], OutputAudience.SECTOR_ENERGY)
    assert rendered == ""


def test_format_block_full_shape_locks_envelope_layout() -> None:
    """Lock the compact envelope layout (ALP-272): single-line header, dense payload.

    Verifying the exact byte-for-byte render gives a single readable
    expression of the entire envelope shape. The fixture uses bootstrap state
    plus a regime context so every conditional segment is exercised. Sorted-key
    iteration is exercised via the mixed-order payload.
    """
    block = OutputBlock(
        block_id="q1.volume_anomaly",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        freshness_ts=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        calibration_state=CalibrationState.ACCUMULATING,
        bootstrap_reason="volume_baseline: 5 < 20",
        payload={
            "z_score": 2.5,
            "ticker": "NVDA",
            "context": {"sector": "tech_semis", "atr": 4.875},
        },
        anomaly_flags=(
            AnomalyFlag(name="volume_spike", magnitude=2.5, severity="investigate_now"),
        ),
        regime_context="low_vol_compression",
    )
    expected = (
        "### q1.volume_anomaly | freshness 2026-04-27T14:30:00+00:00"
        " | accumulating — reason: volume_baseline: 5 < 20"
        " | regime: low_vol_compression\n"
        "context:\n"
        "  atr: 4.875\n"
        "  sector: tech_semis\n"
        "ticker: NVDA\n"
        "z_score: 2.5\n"
        "Anomaly flags (1):\n"
        "  - volume_spike | magnitude 2.50 | severity investigate_now\n"
    )
    assert format_block(block) == expected


def test_format_block_per_ticker_full_shape() -> None:
    """Lock the compact per_ticker layout: one row per ticker, sorted fields, no nesting."""
    block = OutputBlock(
        block_id="q1.divergence_flags",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        freshness_ts=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "per_ticker": {
                "NVDA": {"rsi_1d": 60.0, "divergence_pairs": []},
                "AAPL": {"rsi_1d": 55.0, "divergence_pairs": []},
            },
        },
        anomaly_flags=(),
        regime_context=None,
    )
    expected = (
        "### q1.divergence_flags | freshness 2026-04-27T14:30:00+00:00 | calibrated\n"
        "per_ticker:\n"
        "AAPL divergence_pairs=[] rsi_1d=55\n"
        "NVDA divergence_pairs=[] rsi_1d=60\n"
    )
    assert format_block(block) == expected
