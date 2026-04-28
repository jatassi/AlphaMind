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
    # Sanity: heading is present and starts the output.
    assert rendered_first.startswith("### q1.volume_anomaly\n")


def test_format_block_omits_bootstrap_reason_when_calibrated() -> None:
    """No bootstrap_reason line when state is CALIBRATED."""
    block = _make_block(calibration_state=CalibrationState.CALIBRATED)
    rendered = format_block(block)
    assert "bootstrap_reason" not in rendered
    assert "Calibration: calibrated" in rendered


def test_format_block_includes_bootstrap_reason_when_bootstrap() -> None:
    """The bootstrap_reason line appears when state is BOOTSTRAP."""
    block = _make_block(
        calibration_state=CalibrationState.BOOTSTRAP,
        bootstrap_reason="volume_baseline: 5 < 20",
    )
    rendered = format_block(block)
    assert "Calibration: bootstrap — bootstrap_reason: volume_baseline: 5 < 20" in rendered


def test_format_block_includes_bootstrap_reason_when_unavailable() -> None:
    """The bootstrap_reason line appears when state is UNAVAILABLE."""
    block = _make_block(
        calibration_state=CalibrationState.UNAVAILABLE,
        bootstrap_reason="volume_baseline: 0 < 20 (cross-sectional pool empty)",
    )
    rendered = format_block(block)
    assert "Calibration: unavailable — bootstrap_reason:" in rendered
    assert "(cross-sectional pool empty)" in rendered


def test_format_block_omits_regime_line_when_regime_context_is_none() -> None:
    """The Regime line appears only on blocks where regime is load-bearing."""
    block = _make_block(regime_context=None)
    rendered = format_block(block)
    assert "Regime:" not in rendered


def test_format_block_includes_regime_line_when_regime_context_is_set() -> None:
    """When regime_context is supplied the Regime line is rendered."""
    block = _make_block(regime_context="low_vol_compression (early-strong)")
    rendered = format_block(block)
    assert "Regime: low_vol_compression (early-strong)" in rendered


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
    """Lock the envelope layout: heading, freshness, calibration, regime, payload, flags.

    Verifying the exact byte-for-byte render gives a single readable
    expression of the entire envelope shape. The fixture uses bootstrap state
    plus a regime context so every conditional line is exercised. Sorted-key
    iteration is exercised via the mixed-order payload.
    """
    block = OutputBlock(
        block_id="q1.volume_anomaly",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        freshness_ts=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        calibration_state=CalibrationState.BOOTSTRAP,
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
        "### q1.volume_anomaly\n"
        "Freshness: 2026-04-27T14:30:00+00:00\n"
        "Calibration: bootstrap — bootstrap_reason: volume_baseline: 5 < 20\n"
        "Regime: low_vol_compression\n"
        "\n"
        "context:\n"
        "  atr: 4.875\n"
        "  sector: tech_semis\n"
        "ticker: NVDA\n"
        "z_score: 2.5\n"
        "\n"
        "Anomaly flags (1):\n"
        "  - volume_spike | magnitude 2.50 | severity investigate_now\n"
    )
    assert format_block(block) == expected
