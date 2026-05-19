"""Tests for the correlation/regime brief assembler — story 02-distillation/11b."""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.correlation_brief import (
    CorrelationRegimeBrief,
    assemble_correlation_brief,
)
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.regime import REGIME_BLOCK_ID


def _regime_block(
    *,
    freshness_ts: datetime = datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
    payload: dict[str, object] | None = None,
) -> OutputBlock:
    """Build the universal-broadcast regime.label block."""
    default_payload: dict[str, object] = {
        "regime_label": "low_vol_compression",
        "transition_state": "stable",
        "prior_label": None,
        "invocations_held": 1,
        "indicator_agreement_count": 4,
        "regime_skip_emergency": False,
        "vix_level": 13.5,
        "term_structure_basis": 1.2,
        "vvix_percentile": 22.0,
        "realized_vol_5d": 0.08,
        "realized_vol_20d": 0.10,
    }
    return OutputBlock(
        block_id=REGIME_BLOCK_ID,
        audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload=payload if payload is not None else default_payload,
        anomaly_flags=(),
        regime_context=None,
    )


def test_returns_correlation_regime_brief_dataclass() -> None:
    """Tracer bullet — assembler returns the documented dataclass shape."""
    regime_block = _regime_block()

    brief = assemble_correlation_brief(blocks=[regime_block], invocation_id="inv-001")

    assert isinstance(brief, CorrelationRegimeBrief)
    assert isinstance(brief.text, str)
    assert isinstance(brief.reference_index, dict)
    assert isinstance(brief.freshness_min, datetime)


def test_regime_block_becomes_cr_1() -> None:
    """Per the story scope: regime.label is always embedded as CR-1."""
    regime_block = _regime_block()

    brief = assemble_correlation_brief(blocks=[regime_block], invocation_id="inv-002")

    assert "CR-1" in brief.reference_index
    assert brief.reference_index["CR-1"] == "regime.label"
    assert "[CR-1]" in brief.text
    assert "low_vol_compression" in brief.text
    assert "=== REGIME ===" in brief.text


def test_regime_section_first_invocation_shows_no_prior_label() -> None:
    """First-deploy regime renders 'n/a (first invocation)' — explicit per the story."""
    regime_block = _regime_block(
        payload={
            "regime_label": "low_vol_compression",
            "transition_state": "stable",
            "prior_label": None,
            "invocations_held": 1,
            "indicator_agreement_count": 4,
            "regime_skip_emergency": False,
            "vix_level": 13.5,
            "term_structure_basis": 1.2,
            "vvix_percentile": 22.0,
            "realized_vol_5d": 0.08,
            "realized_vol_20d": 0.10,
        }
    )

    brief = assemble_correlation_brief(blocks=[regime_block], invocation_id="inv-003")

    assert "n/a (first invocation)" in brief.text


def _lead_lag_block(*, pair_key: str, lead: str, lag: str, days: float) -> OutputBlock:
    """Build a story-08d lead_lag block carrying one pair."""
    return OutputBlock(
        block_id=f"q7.lead_lag.{pair_key}",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "pair_lag": {
                pair_key: {
                    "lead_ticker": lead,
                    "lag_ticker": lag,
                    "lead_lag_days_estimate": days,
                    "n_pair_events": 12,
                    "max_days": 5,
                }
            },
        },
        anomaly_flags=(),
        regime_context=None,
    )


def test_sequential_cr_n_indexing_contiguous_no_gaps() -> None:
    """CR-1, CR-2, ..., CR-N — contiguous, no gaps, no reuse within the brief."""
    regime_block = _regime_block()
    pair_a = _lead_lag_block(pair_key="alpha", lead="HYG", lag="SPY", days=1.5)
    pair_b = _lead_lag_block(pair_key="bravo", lead="QQQ", lag="IWM", days=2.0)

    brief = assemble_correlation_brief(
        blocks=[regime_block, pair_a, pair_b], invocation_id="inv-seq"
    )

    references = sorted(brief.reference_index.keys(), key=lambda k: int(k.split("-")[1]))
    expected = [f"CR-{n}" for n in range(1, len(references) + 1)]
    assert references == expected
    # No reuse — every value is unique-by-key (dict guarantees that), and
    # the values are also unique block_id::natural_key strings:
    assert len(set(brief.reference_index.values())) == len(brief.reference_index)


def test_lead_lag_block_with_pair_natural_key_uses_double_colon_form() -> None:
    """Multi-finding payloads use ``block_id::natural_key`` per the story scope."""
    regime_block = _regime_block()
    pair_block = _lead_lag_block(pair_key="alpha", lead="HYG", lag="SPY", days=1.5)

    brief = assemble_correlation_brief(blocks=[regime_block, pair_block], invocation_id="inv-ll")

    pair_reference = next(
        ref for ref in brief.reference_index.values() if ref.startswith("q7.lead_lag.alpha")
    )
    assert pair_reference == "q7.lead_lag.alpha::alpha"


def test_empty_sections_are_omitted() -> None:
    """Regime-only invocation: only REGIME section appears; other sections suppressed."""
    regime_block = _regime_block()

    brief = assemble_correlation_brief(blocks=[regime_block], invocation_id="inv-empty")

    assert "=== REGIME ===" in brief.text
    assert "=== INTRA-SECTOR CORRELATION ===" not in brief.text
    assert "=== CROSS-SECTOR ROTATION ===" not in brief.text
    assert "=== INTERMARKET REGIME SIGNALS ===" not in brief.text
    assert "=== LEAD-LAG ===" not in brief.text
    assert "=== CORRELATION REGIME CHANGE ===" not in brief.text
    assert "=== NARRATIVE LAG ===" not in brief.text


def test_freshness_min_reflects_oldest_contributing_block() -> None:
    """``freshness_min`` is the oldest contributing block's ``freshness_ts``."""
    regime_block = _regime_block(freshness_ts=datetime(2026, 4, 27, 14, 30, tzinfo=UTC))
    older_block = OutputBlock(
        block_id="q7.cross_sector_rotation",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=datetime(2026, 4, 27, 9, 0, tzinfo=UTC),  # earlier
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "velocity_label": "slow",
            "narrative_label": "growth_driven",
            "short_window": {"window_days": 5, "relative_performance": {}},
            "long_window": {"window_days": 30, "relative_performance": {}},
        },
        anomaly_flags=(),
        regime_context=None,
    )

    brief = assemble_correlation_brief(
        blocks=[regime_block, older_block], invocation_id="inv-fresh"
    )

    assert brief.freshness_min == datetime(2026, 4, 27, 9, 0, tzinfo=UTC)


def test_universal_context_section_excludes_regime_label_block() -> None:
    """The trailing universal-context section never duplicates regime.label.

    regime.label is already embedded as CR-1; the universal-context
    section must not render it a second time.
    """
    regime_block = _regime_block()
    macro_block = OutputBlock(
        block_id="q6.yield_curve_regime",
        audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "label": "normal",
            "spread_2s10s": 0.55,
            "spread_3m10y": 0.40,
            "spread_5s30s": 0.20,
            "regime_transition": False,
        },
        anomaly_flags=(),
        regime_context=None,
    )

    brief = assemble_correlation_brief(
        blocks=[regime_block, macro_block], invocation_id="inv-universal"
    )

    assert "=== UNIVERSAL CONTEXT ===" in brief.text
    assert "q6.yield_curve_regime" in brief.text
    # The regime.label block_id appears only once — in the regime section
    # header rendering as part of CR-1, NOT as a "### regime.label" heading
    # in the universal-context section.
    assert brief.text.count("### regime.label") == 0


def test_anomaly_summary_appears_at_end_scoped_to_cr_audience() -> None:
    """Anomaly summary section appears at the end and is scoped to CR audience."""
    regime_block = _regime_block()
    anomaly_block = OutputBlock(
        block_id="q7.correlation_breakdown.AAPL_MSFT",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "pair": ["AAPL", "MSFT"],
            "short_correlation": 0.20,
            "long_correlation": 0.85,
            "deviation_sigma": 3.5,
            "q_value": 0.01,
            "short_window_days": 5,
            "long_window_days": 60,
        },
        anomaly_flags=(
            AnomalyFlag(
                name="correlation_breakdown_flag:AAPL:MSFT",
                magnitude=3.5,
                severity="investigate_now",
            ),
        ),
        regime_context=None,
    )

    brief = assemble_correlation_brief(
        blocks=[regime_block, anomaly_block], invocation_id="inv-anomaly"
    )

    anomaly_header_idx = brief.text.find("=== ANOMALY FLAGS")
    regime_header_idx = brief.text.find("=== REGIME ===")
    assert anomaly_header_idx > 0
    assert anomaly_header_idx > regime_header_idx
    assert "correlation_breakdown_flag:AAPL:MSFT" in brief.text


def test_reference_index_maps_every_cr_back_to_a_source_block() -> None:
    """Every CR-N entry resolves to a real source block_id."""
    regime_block = _regime_block()
    rotation_block = OutputBlock(
        block_id="q7.cross_sector_rotation",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "velocity_label": "slow",
            "narrative_label": "growth_driven",
            "short_window": {"window_days": 5, "relative_performance": {}},
            "long_window": {"window_days": 30, "relative_performance": {}},
        },
        anomaly_flags=(),
        regime_context=None,
    )

    brief = assemble_correlation_brief(
        blocks=[regime_block, rotation_block], invocation_id="inv-rev"
    )

    source_ids = {block.block_id for block in [regime_block, rotation_block]}
    for reference_value in brief.reference_index.values():
        # Strip optional ``::natural_key`` suffix.
        block_id = reference_value.split("::", 1)[0]
        assert block_id in source_ids


def test_each_category_renders_in_its_documented_section() -> None:
    """Q7 blocks land in the correct section per the document framing in the spec."""
    regime_block = _regime_block()
    intra = OutputBlock(
        block_id="q7.intra_sector_correlation.tech_semis",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "sector": "tech_semis",
            "short_window": {"window_days": 5, "n_observations": 5, "correlation_matrix": {}},
            "long_window": {"window_days": 60, "n_observations": 60, "correlation_matrix": {}},
        },
        anomaly_flags=(),
        regime_context=None,
    )
    intermarket = OutputBlock(
        block_id="q7.intermarket_regime.spy_tlt",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "correlation": -0.45,
            "regime_label": "growth_environment",
            "window_days": 60,
        },
        anomaly_flags=(),
        regime_context=None,
    )
    narrative = OutputBlock(
        block_id="q7.narrative_lag",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "media_silence_hours": 24,
            "qualifying_news_present": False,
            "n_breakdowns": 2,
        },
        anomaly_flags=(),
        regime_context=None,
    )

    brief = assemble_correlation_brief(
        blocks=[regime_block, intra, intermarket, narrative], invocation_id="inv-cat"
    )

    text = brief.text
    intra_idx = text.find("=== INTRA-SECTOR CORRELATION ===")
    intermarket_idx = text.find("=== INTERMARKET REGIME SIGNALS ===")
    narrative_idx = text.find("=== NARRATIVE LAG ===")

    assert intra_idx > 0
    assert intermarket_idx > intra_idx  # documented order
    assert narrative_idx > intermarket_idx
    assert "tech_semis" in text
    assert "spy_tlt" in text


def test_regime_skip_emergency_renders_in_regime_section() -> None:
    """Emergency-skip flag surfaces in the regime detail when active."""
    regime_block = _regime_block(
        payload={
            "regime_label": "crisis_spike",
            "transition_state": "early-strong",
            "prior_label": "low_vol_compression",
            "invocations_held": 1,
            "indicator_agreement_count": 4,
            "regime_skip_emergency": True,
            "vix_level": 42.0,
            "term_structure_basis": -3.5,
            "vvix_percentile": 95.0,
            "realized_vol_5d": 0.45,
            "realized_vol_20d": 0.20,
        }
    )

    brief = assemble_correlation_brief(blocks=[regime_block], invocation_id="inv-skip")

    assert "Emergency-skip" in brief.text or "emergency-skip" in brief.text


def _dispersion_shift_block(
    *,
    today_dispersion: float = 0.0125,
    mean_trailing_dispersion: float = 0.0050,
    stdev_trailing_dispersion: float = 0.0015,
    zscore: float = 5.0,
    dispersion_window_days: int = 20,
) -> OutputBlock:
    """Build a story-08c dispersion_shift block with the documented payload schema."""
    return OutputBlock(
        block_id="q7.correlation_breakdown.dispersion_shift",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "today_dispersion": today_dispersion,
            "mean_trailing_dispersion": mean_trailing_dispersion,
            "stdev_trailing_dispersion": stdev_trailing_dispersion,
            "zscore": zscore,
            "dispersion_window_days": dispersion_window_days,
        },
        anomaly_flags=(),
        regime_context=None,
    )


def test_dispersion_shift_block_does_not_render_as_correlation_pair() -> None:
    """``q7.correlation_breakdown.dispersion_shift`` must not be rendered by the pair formatter.

    The pair formatter reads ``short_correlation`` / ``long_correlation`` /
    ``deviation_sigma`` — keys the dispersion_shift payload does not carry.
    Misrouting produces ``[CR-N] dispersion_shift: short None vs. long None
    (deviation None sigma)`` with all-None detail lines (the symptom in
    ALP-546). Verify the brief renders the dispersion_shift payload's actual
    fields instead.
    """
    regime_block = _regime_block()
    dispersion_block = _dispersion_shift_block(
        today_dispersion=0.025,
        mean_trailing_dispersion=0.010,
        stdev_trailing_dispersion=0.003,
        zscore=5.0,
        dispersion_window_days=20,
    )

    brief = assemble_correlation_brief(
        blocks=[regime_block, dispersion_block], invocation_id="inv-dispersion"
    )

    assert "short None" not in brief.text
    assert "long None" not in brief.text
    assert "deviation None sigma" not in brief.text
    assert "Short correlation: None" not in brief.text
    assert "Long correlation: None" not in brief.text
    assert "Deviation sigma: None" not in brief.text
    # The dispersion_shift block contributes its own CR-N entry with payload
    # values rendered explicitly. Reference index resolves the CR-N back to
    # the source block_id.
    assert "dispersion shift" in brief.text
    assert "Today dispersion: 0.025" in brief.text
    assert "Trailing mean dispersion: 0.01" in brief.text
    assert "Z-score: 5" in brief.text
    assert "q7.correlation_breakdown.dispersion_shift" in brief.reference_index.values()


def test_byte_identical_across_repeated_calls() -> None:
    """Same inputs produce byte-identical output — invocation archive diffs cleanly."""
    regime_block = _regime_block()
    rotation_block = OutputBlock(
        block_id="q7.cross_sector_rotation",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "velocity_label": "sharp",
            "narrative_label": "rate_driven",
            "short_window": {"window_days": 5, "relative_performance": {"XLF": 0.012}},
            "long_window": {"window_days": 30, "relative_performance": {"XLF": 0.045}},
        },
        anomaly_flags=(),
        regime_context=None,
    )

    first = assemble_correlation_brief(blocks=[regime_block, rotation_block], invocation_id="inv-9")
    second = assemble_correlation_brief(
        blocks=[regime_block, rotation_block], invocation_id="inv-9"
    )

    assert first.text == second.text
    assert first.reference_index == second.reference_index
