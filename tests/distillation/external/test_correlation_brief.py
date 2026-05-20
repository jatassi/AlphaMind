"""Tests for the correlation/regime brief assembler — story 02-distillation/11b."""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.correlation_brief import (
    CorrelationRegimeBrief,
    assemble_correlation_brief,
)
from alphamind.distillation.output import (
    GENERAL_FLOAT_FORMAT,
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7.correlation_regime_change_compute import (
    DISPERSION_SHIFT_BLOCK_ID,
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


def test_no_data_invocation_renders_every_section_with_empty_state_marker() -> None:
    """Per ALP-577: every documented section appears with explicit empty-state text.

    The contract: a brief section never silently drops on empty data — every
    section has a fixed position and renders either findings or an explicit
    empty-state marker. Pre-fix, the NARRATIVE LAG section vanished entirely
    when the news pipeline produced no qualifying headlines (the symptom in
    invocation ``inv-20260519T030654Z-60f10023``); downstream agents could
    not distinguish "section not computed" from "section computed but
    empty" from "section accidentally elided".
    """
    regime_block = _regime_block()

    brief = assemble_correlation_brief(blocks=[regime_block], invocation_id="inv-empty")

    text = brief.text
    expected_headers = (
        "=== REGIME ===",
        "=== INTRA-SECTOR CORRELATION ===",
        "=== CROSS-SECTOR ROTATION ===",
        "=== INTERMARKET REGIME SIGNALS ===",
        "=== LEAD-LAG ===",
        "=== LOCUS FLAGS ===",
        "=== CORRELATION REGIME CHANGE ===",
        "=== NARRATIVE LAG ===",
        "=== UNIVERSAL CONTEXT ===",
        "=== ANOMALY FLAGS (0) ===",
    )
    for header in expected_headers:
        assert header in text, f"{header} missing from no-data brief"

    # Document order — each header strictly follows the previous one.
    positions = [text.find(header) for header in expected_headers]
    assert positions == sorted(positions), positions

    # CR-N sections with no entries render the explicit empty-state marker.
    empty_cr_sections = (
        "=== INTRA-SECTOR CORRELATION ===",
        "=== CROSS-SECTOR ROTATION ===",
        "=== INTERMARKET REGIME SIGNALS ===",
        "=== LEAD-LAG ===",
        "=== LOCUS FLAGS ===",
        "=== CORRELATION REGIME CHANGE ===",
        "=== NARRATIVE LAG ===",
    )
    for header in empty_cr_sections:
        idx = text.find(header)
        following = text[idx + len(header) :].lstrip("\n")
        assert following.startswith("(no findings)"), (
            f"{header} should be followed by '(no findings)' marker, got: {following[:80]!r}"
        )

    # Universal context section carries its own empty-state line (the regime
    # block is excluded from the universal-context body — it is already
    # embedded as CR-1).
    universal_idx = text.find("=== UNIVERSAL CONTEXT ===")
    next_section_idx = text.find("=== ANOMALY FLAGS", universal_idx)
    universal_body = text[universal_idx + len("=== UNIVERSAL CONTEXT ===") : next_section_idx]
    assert "(no universal-broadcast blocks)" in universal_body, universal_body


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


def test_regime_section_renders_unavailable_when_vvix_percentile_is_null() -> None:
    """Per ALP-571: null vvix_percentile renders as 'unavailable', never '50'.

    Pre-fix the regime block stamped 50.0 unconditionally; the brief rendered
    'VVIX percentile 50' and downstream agents treated it as a live
    median-vol reading. The fix propagates ``None`` through the payload and
    the brief surfaces the explicit missing-data signal plus the calibration
    reason.
    """
    regime_block = OutputBlock(
        block_id=REGIME_BLOCK_ID,
        audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
        freshness_ts=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        calibration_state=CalibrationState.UNAVAILABLE,
        bootstrap_reason="regime: VVIX series unavailable",
        payload={
            "regime_label": "vol_expansion",
            "transition_state": "stable",
            "prior_label": None,
            "invocations_held": 1,
            "indicator_agreement_count": 3,
            "regime_skip_emergency": False,
            "vix_level": 17.26,
            "term_structure_basis": 0.5,
            "vvix_percentile": None,
            "realized_vol_5d": 0.12,
            "realized_vol_20d": 0.10,
        },
        anomaly_flags=(),
        regime_context=None,
    )

    brief = assemble_correlation_brief(blocks=[regime_block], invocation_id="inv-null-vvix")

    assert "VVIX percentile unavailable" in brief.text
    assert "VVIX percentile 50" not in brief.text
    # Signal-quality annotation surfaces the calibration tag so the
    # synthesizer sees why the field is missing rather than guessing.
    assert "Signal quality: unavailable" in brief.text
    assert "VVIX series unavailable" in brief.text


def test_regime_section_renders_unavailable_when_term_structure_basis_is_null() -> None:
    """Per ALP-572: null term_structure_basis renders as 'unavailable', never '0'.

    Pre-fix the regime block stamped ``vx1_minus_vix=0.0`` unconditionally; the
    brief rendered 'term-structure basis 0' and downstream agents treated it
    as a live "flat term structure" reading. The fix propagates ``None``
    through the payload and the brief surfaces the explicit missing-data
    signal plus the calibration reason.
    """
    regime_block = OutputBlock(
        block_id=REGIME_BLOCK_ID,
        audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
        freshness_ts=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        calibration_state=CalibrationState.UNAVAILABLE,
        bootstrap_reason="regime: VX1 series unavailable",
        payload={
            "regime_label": "vol_expansion",
            "transition_state": "stable",
            "prior_label": None,
            "invocations_held": 1,
            "indicator_agreement_count": 3,
            "regime_skip_emergency": False,
            "vix_level": 17.26,
            "term_structure_basis": None,
            "vvix_percentile": 55.0,
            "realized_vol_5d": 0.12,
            "realized_vol_20d": 0.10,
        },
        anomaly_flags=(),
        regime_context=None,
    )

    brief = assemble_correlation_brief(blocks=[regime_block], invocation_id="inv-null-basis")

    assert "term-structure basis unavailable" in brief.text
    assert "term-structure basis 0" not in brief.text
    assert "Signal quality: unavailable" in brief.text
    assert "VX1 series unavailable" in brief.text


def test_regime_section_preserves_signed_basis_for_backwardation() -> None:
    """A negative basis (backwardation) renders as the signed real, not 0.

    Pre-fix the ``0.0`` default lost the sign; the fix ensures a real
    ``-0.42`` continues to format as ``-0.42`` under the ``.4g`` precision
    rule rather than truncating to integer ``0``.
    """
    regime_block = OutputBlock(
        block_id=REGIME_BLOCK_ID,
        audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
        freshness_ts=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "regime_label": "crisis_spike",
            "transition_state": "early-strong",
            "prior_label": "vol_expansion",
            "invocations_held": 1,
            "indicator_agreement_count": 4,
            "regime_skip_emergency": False,
            "vix_level": 38.0,
            "term_structure_basis": -0.42,
            "vvix_percentile": 90.0,
            "realized_vol_5d": 0.32,
            "realized_vol_20d": 0.22,
        },
        anomaly_flags=(),
        regime_context=None,
    )

    brief = assemble_correlation_brief(blocks=[regime_block], invocation_id="inv-signed-basis")

    assert "term-structure basis -0.42" in brief.text


def _locus_block(
    *,
    locus_ticker: str,
    pair_count: int = 4,
    max_sigma: float = 5.83,
    partners: tuple[str, ...] = ("A", "B", "C", "D"),
    partners_by_sector: dict[str, list[str]] | None = None,
    cross_sector_spread: str | None = None,
) -> OutputBlock:
    """Build a ``q7.correlation_locus.<ticker>`` block for the brief tests."""
    payload: dict[str, object] = {
        "locus_ticker": locus_ticker,
        "pair_count": pair_count,
        "max_deviation_sigma": max_sigma,
        "partner_tickers": list(partners),
        "supporting_pairs": tuple(f"q7.correlation_breakdown.{locus_ticker}_{p}" for p in partners),
        "partners_by_sector": partners_by_sector,
        "cross_sector_spread": cross_sector_spread,
    }
    return OutputBlock(
        block_id=f"q7.correlation_locus.{locus_ticker}",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload=payload,
        anomaly_flags=(
            AnomalyFlag(
                name=f"correlation_locus_flag:{locus_ticker}",
                magnitude=max_sigma,
                severity="investigate_now",
            ),
        ),
        regime_context=None,
    )


def test_locus_block_renders_in_locus_flags_section_above_breakdown_section() -> None:
    """ALP-543 — locus blocks land under ``=== LOCUS FLAGS ===`` ahead of per-pair section."""
    regime_block = _regime_block()
    locus = _locus_block(
        locus_ticker="META",
        pair_count=4,
        max_sigma=5.83,
        partners=("AAPL", "GOOG", "AMZN", "MSFT"),
        partners_by_sector={"tech": ["AAPL", "AMZN", "GOOG", "MSFT"]},
        cross_sector_spread="tech-only",
    )
    pair = OutputBlock(
        block_id="q7.correlation_breakdown.X_Y",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "pair": ["X", "Y"],
            "short_correlation": -0.5,
            "long_correlation": 0.8,
            "deviation_sigma": 4.2,
            "q_value": 0.02,
            "short_window_days": 20,
            "long_window_days": 60,
            "n_overlapping_observations": 60,
        },
        anomaly_flags=(),
        regime_context=None,
    )

    brief = assemble_correlation_brief(
        blocks=[regime_block, locus, pair], invocation_id="inv-locus"
    )

    text = brief.text
    locus_idx = text.find("=== LOCUS FLAGS ===")
    breakdown_idx = text.find("=== CORRELATION REGIME CHANGE ===")
    assert locus_idx > 0, text
    assert breakdown_idx > locus_idx, "locus section must render before per-pair section"
    assert "META" in text
    assert "Pair count: 4" in text
    assert "AAPL" in text
    assert "Cross-sector spread: tech-only" in text


def test_locus_reference_index_maps_to_locus_block_id() -> None:
    """Each locus CR-N entry resolves to its ``q7.correlation_locus.<ticker>`` block."""
    regime_block = _regime_block()
    locus = _locus_block(locus_ticker="META", partners=("A", "B", "C"))

    brief = assemble_correlation_brief(blocks=[regime_block, locus], invocation_id="inv-locus-ref")

    locus_refs = [
        cr for cr, value in brief.reference_index.items() if value == "q7.correlation_locus.META"
    ]
    assert locus_refs, brief.reference_index


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
        block_id=DISPERSION_SHIFT_BLOCK_ID,
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

    # Structural guard against the misroute-symptom class: the
    # CORRELATION REGIME CHANGE section must not contain the literal
    # ``None`` (the rendered form of an absent payload key).
    cr_section_start = brief.text.find("=== CORRELATION REGIME CHANGE ===")
    cr_section_end = brief.text.find("===", cr_section_start + 1)
    if cr_section_end == -1:
        cr_section_end = len(brief.text)
    cr_section = brief.text[cr_section_start:cr_section_end]
    assert "None" not in cr_section, cr_section
    # Positive: the dispersion_shift block contributes its own CR-N entry
    # with the actual payload values rendered. Float strings go through
    # GENERAL_FLOAT_FORMAT so the assertion tracks the format constant
    # rather than a hardcoded literal.
    today_str = format(0.025, GENERAL_FLOAT_FORMAT)
    mean_str = format(0.010, GENERAL_FLOAT_FORMAT)
    zscore_str = format(5.0, GENERAL_FLOAT_FORMAT)
    assert "dispersion shift" in brief.text
    assert f"Today dispersion: {today_str}" in brief.text
    assert f"Trailing mean dispersion: {mean_str}" in brief.text
    assert f"Z-score: {zscore_str}" in brief.text
    assert DISPERSION_SHIFT_BLOCK_ID in brief.reference_index.values()


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
