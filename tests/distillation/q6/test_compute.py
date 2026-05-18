"""Pure-compute tests for q6 (ALP-485) — no SQLite, no Session.

ALP-485 propagated the compute/load boundary split (piloted in ALP-467) to
q6. Each compute core is testable from hand-built frozen inputs with no
ORM or in-memory database. The tests in this module construct inputs
directly and assert on the pure return shape — proving the q6 compute
path is genuinely pure and safe to call under
``asyncio.TaskGroup`` + ``asyncio.to_thread``.

Coverage:

- yield_curve_compute — classifier branches (transition / inverted / flat /
  normal), transition flag against a prior label.
- inflation_compute — deflation_risk precedence, hot / cooling / stable
  branches.
- dollar_attribution_compute — rate vs risk dominance, residual
  ``trade_flow_driven`` fallback.
- funding_stress_compute — percentile rank against history, aggregate
  alert.
- macro_surprise_compute — alert percentile gate, z-score magnitude.
- assemble — pure :func:`assemble_q6_blocks_from_inputs` over a frozen
  :class:`Q6Inputs`, both fully-calibrated and bootstrap-stub paths.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.output import AnomalyFlag, OutputAudience
from alphamind.distillation.q6 import (
    DollarAttributionLabel,
    DollarAttributionResult,
    FundingStressResult,
    InflationRegimeLabel,
    InflationRegimeResult,
    MarketLiquidityResult,
    Q6Inputs,
    YieldCurveRegimeLabel,
    YieldCurveRegimeResult,
    assemble_q6_blocks_from_inputs,
    classify_dollar_attribution,
    classify_inflation_regime,
    classify_yield_curve_regime,
    detect_macro_surprise_anomaly,
)
from alphamind.distillation.q6.funding_stress_compute import (
    component_percentile,
    compute_funding_stress_alert,
)

# ---------------------------------------------------------------------------
# yield_curve_compute
# ---------------------------------------------------------------------------


def test_yield_curve_steepening_fires_above_transition_cutoff() -> None:
    """A 5-day 2s10s change above +25 bps tags steepening."""
    result = classify_yield_curve_regime(
        dgs3mo=4.0,
        dgs2=3.5,
        dgs5=3.8,
        dgs10=4.1,
        dgs30=4.4,
        spread_2s10s_5d_ago=0.0,  # current = 0.6, change = +0.6
        prior_label=None,
    )
    assert result.label is YieldCurveRegimeLabel.STEEPENING
    assert result.spread_2s10s == pytest.approx(0.6)
    assert result.regime_transition is False  # no prior label


def test_yield_curve_flattening_below_negative_cutoff() -> None:
    """A 5-day 2s10s change below -25 bps tags flattening."""
    result = classify_yield_curve_regime(
        dgs3mo=4.0,
        dgs2=3.8,
        dgs5=3.85,
        dgs10=4.0,
        dgs30=4.1,
        spread_2s10s_5d_ago=1.0,  # current = 0.2, change = -0.8
        prior_label=None,
    )
    assert result.label is YieldCurveRegimeLabel.FLATTENING


def test_yield_curve_inverted_when_spread_negative_outside_flat() -> None:
    """Negative 2s10s outside the flat band is inverted."""
    result = classify_yield_curve_regime(
        dgs3mo=5.0,
        dgs2=4.5,
        dgs5=4.2,
        dgs10=4.0,
        dgs30=3.9,
        spread_2s10s_5d_ago=-0.5,  # current = -0.5, change = 0
        prior_label=None,
    )
    assert result.label is YieldCurveRegimeLabel.INVERTED


def test_yield_curve_flat_when_spread_inside_flat_band() -> None:
    """|2s10s| <= 10 bps is flat."""
    result = classify_yield_curve_regime(
        dgs3mo=4.0,
        dgs2=4.0,
        dgs5=4.05,
        dgs10=4.05,
        dgs30=4.1,
        spread_2s10s_5d_ago=0.05,  # current = 0.05
        prior_label=None,
    )
    assert result.label is YieldCurveRegimeLabel.FLAT


def test_yield_curve_normal_upward_sloping_default() -> None:
    """Positive 2s10s outside the flat band is normal upward sloping."""
    result = classify_yield_curve_regime(
        dgs3mo=3.5,
        dgs2=3.8,
        dgs5=4.0,
        dgs10=4.3,
        dgs30=4.6,
        spread_2s10s_5d_ago=0.4,  # current = 0.5, change = +0.1
        prior_label=None,
    )
    assert result.label is YieldCurveRegimeLabel.NORMAL_UPWARD_SLOPING


def test_yield_curve_regime_transition_flag_against_prior() -> None:
    """Different prior label flips ``regime_transition`` to True."""
    result = classify_yield_curve_regime(
        dgs3mo=3.5,
        dgs2=3.8,
        dgs5=4.0,
        dgs10=4.3,
        dgs30=4.6,
        spread_2s10s_5d_ago=0.4,
        prior_label=YieldCurveRegimeLabel.INVERTED,
    )
    assert result.label is YieldCurveRegimeLabel.NORMAL_UPWARD_SLOPING
    assert result.regime_transition is True


# ---------------------------------------------------------------------------
# inflation_compute
# ---------------------------------------------------------------------------


def test_inflation_deflation_risk_takes_precedence() -> None:
    """``breakeven_below_threshold_30d`` wins regardless of trend."""
    result = classify_inflation_regime(
        breakeven_trend_bps=0.50,  # would be "hot" but for the override
        recent_cpi_surprise_signs=[1, 1, 1],
        breakeven_below_threshold_30d=True,
        prior_label=None,
    )
    assert result.label is InflationRegimeLabel.DEFLATION_RISK


def test_inflation_hot_when_trend_high_and_surprises_positive() -> None:
    """Trend ≥ +30 bps and all-positive surprises tags hot."""
    result = classify_inflation_regime(
        breakeven_trend_bps=0.40,
        recent_cpi_surprise_signs=[1, 1, 0],
        breakeven_below_threshold_30d=False,
        prior_label=None,
    )
    assert result.label is InflationRegimeLabel.HOT
    assert result.cpi_surprise_signs_positive == 2
    assert result.cpi_surprise_signs_negative == 0


def test_inflation_cooling_when_trend_low_and_surprises_negative() -> None:
    """Trend ≤ -30 bps and all-negative surprises tags cooling."""
    result = classify_inflation_regime(
        breakeven_trend_bps=-0.40,
        recent_cpi_surprise_signs=[-1, -1, 0],
        breakeven_below_threshold_30d=False,
        prior_label=None,
    )
    assert result.label is InflationRegimeLabel.COOLING


def test_inflation_stable_when_mixed_surprises() -> None:
    """Any mixed surprise signs fall through to stable."""
    result = classify_inflation_regime(
        breakeven_trend_bps=0.40,
        recent_cpi_surprise_signs=[1, -1, 0],  # mixed → stable
        breakeven_below_threshold_30d=False,
        prior_label=None,
    )
    assert result.label is InflationRegimeLabel.STABLE


# ---------------------------------------------------------------------------
# dollar_attribution_compute
# ---------------------------------------------------------------------------


def test_dollar_attribution_rate_differential_when_rate_dominant() -> None:
    """|rate corr| dominant by ≥ 20% margin tags rate_differential_driven."""
    # DXY moves perfectly with rate diff, uncorrelated with SPY.
    result = classify_dollar_attribution(
        dxy_returns=[0.01, -0.01, 0.02, -0.02, 0.01],
        rate_diff_returns=[0.01, -0.01, 0.02, -0.02, 0.01],
        spy_returns=[0.005, 0.005, 0.005, 0.005, 0.005],  # zero variance
    )
    assert result.label is DollarAttributionLabel.RATE_DIFFERENTIAL_DRIVEN
    assert result.rate_correlation == pytest.approx(1.0)
    assert result.risk_correlation == pytest.approx(0.0)


def test_dollar_attribution_risk_sentiment_when_spy_dominant() -> None:
    """|SPY corr| dominant by ≥ 20% margin tags risk_sentiment_driven."""
    result = classify_dollar_attribution(
        dxy_returns=[0.01, -0.01, 0.02, -0.02, 0.01],
        rate_diff_returns=[0.005, 0.005, 0.005, 0.005, 0.005],  # zero variance
        spy_returns=[0.01, -0.01, 0.02, -0.02, 0.01],
    )
    assert result.label is DollarAttributionLabel.RISK_SENTIMENT_DRIVEN


def test_dollar_attribution_trade_flow_when_neither_dominant() -> None:
    """Margin below 20% falls through to the residual label."""
    # Both correlations roughly equal at moderate strength.
    result = classify_dollar_attribution(
        dxy_returns=[0.01, -0.01, 0.02],
        rate_diff_returns=[0.01, -0.01, 0.02],
        spy_returns=[0.01, -0.01, 0.02],  # identical correlation
    )
    assert result.label is DollarAttributionLabel.TRADE_FLOW_DRIVEN


def test_dollar_attribution_empty_inputs_yield_trade_flow() -> None:
    """Empty SPY series + zero rate variance: residual fires."""
    result = classify_dollar_attribution(
        dxy_returns=[0.01, -0.01],
        rate_diff_returns=[0.005, 0.005],  # zero variance → 0.0 corr
        spy_returns=[],
    )
    assert result.label is DollarAttributionLabel.TRADE_FLOW_DRIVEN


# ---------------------------------------------------------------------------
# funding_stress_compute
# ---------------------------------------------------------------------------


def test_component_percentile_value_at_top_reports_100() -> None:
    """A value at or above every history entry reports 100.0."""
    assert component_percentile(10.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == 100.0


def test_component_percentile_empty_history_returns_zero() -> None:
    """No prior history means percentile is undefined; report 0.0."""
    assert component_percentile(5.0, []) == 0.0


def test_compute_funding_stress_alert_fires_when_three_above() -> None:
    """≥ 3 components at or above 90th percentile fires the aggregate alert."""
    components = {
        "sofr_ois_spread": 1.0,
        "repo_treasury_spread": 1.0,
        "term_repo_premium": 1.0,
        "mmf_flow": 0.0,
    }
    prior_history: list[dict[str, float]] = [
        {
            "sofr_ois_spread": 0.0,
            "repo_treasury_spread": 0.0,
            "term_repo_premium": 0.0,
            "mmf_flow": 1.0,
        },
        {
            "sofr_ois_spread": 0.5,
            "repo_treasury_spread": 0.5,
            "term_repo_premium": 0.5,
            "mmf_flow": 0.5,
        },
    ]
    percentiles, components_above, alert_active = compute_funding_stress_alert(
        components=components,
        prior_history=prior_history,
        component_alert_count=3,
        component_alert_percentile=90.0,
    )
    # Three components (sofr/repo/term) at 100; mmf at 50%.
    assert percentiles["sofr_ois_spread"] == 100.0
    assert components_above == 3
    assert alert_active is True


def test_compute_funding_stress_alert_silent_below_min_count() -> None:
    """Fewer than ``component_alert_count`` above-threshold → silent."""
    components = {"a": 1.0, "b": 0.0, "c": 0.0, "d": 0.0}
    prior_history: list[dict[str, float]] = [{"a": 0.0, "b": 1.0, "c": 1.0, "d": 1.0}]
    _, components_above, alert_active = compute_funding_stress_alert(
        components=components,
        prior_history=prior_history,
        component_alert_count=2,
        component_alert_percentile=90.0,
    )
    # Only ``a`` is at 100; b/c/d at 50%.
    assert components_above == 1
    assert alert_active is False


# ---------------------------------------------------------------------------
# macro_surprise_compute
# ---------------------------------------------------------------------------


def test_macro_surprise_anomaly_fires_above_alert_percentile() -> None:
    """Surprise in the top 10% of trailing magnitudes fires."""
    flag = detect_macro_surprise_anomaly(
        actual=5.0,
        consensus=0.0,
        trailing_surprises=[0.1, -0.2, 0.05, -0.1, 0.0, -0.05, 0.2, 0.1],
        alert_percentile=90.0,
    )
    assert flag is not None
    assert flag.name == "macro_surprise_anomaly"
    assert flag.severity == "investigate_now"


def test_macro_surprise_anomaly_silent_below_alert_percentile() -> None:
    """A surprise below the rank threshold returns None."""
    flag = detect_macro_surprise_anomaly(
        actual=0.0,
        consensus=0.0,
        trailing_surprises=[10.0, 20.0, 30.0],
        alert_percentile=90.0,
    )
    assert flag is None


def test_macro_surprise_anomaly_empty_history_returns_none() -> None:
    """Empty trailing distribution returns None — no rank to compute."""
    assert (
        detect_macro_surprise_anomaly(
            actual=5.0,
            consensus=0.0,
            trailing_surprises=[],
            alert_percentile=90.0,
        )
        is None
    )


# ---------------------------------------------------------------------------
# assemble — pure assembly path
# ---------------------------------------------------------------------------


def _make_yield_curve_result() -> YieldCurveRegimeResult:
    """Helper: a calibrated yield-curve result for assembly tests."""
    return classify_yield_curve_regime(
        dgs3mo=3.5,
        dgs2=3.8,
        dgs5=4.0,
        dgs10=4.3,
        dgs30=4.6,
        spread_2s10s_5d_ago=0.4,
        prior_label=None,
    )


def _make_inflation_result() -> InflationRegimeResult:
    return classify_inflation_regime(
        breakeven_trend_bps=0.0,
        recent_cpi_surprise_signs=[],
        breakeven_below_threshold_30d=False,
        prior_label=None,
    )


def _make_dollar_result() -> DollarAttributionResult:
    return classify_dollar_attribution(
        dxy_returns=[0.01, -0.01, 0.02],
        rate_diff_returns=[0.01, -0.01, 0.02],
        spy_returns=[0.01, -0.01, 0.02],
    )


def _calibrated_funding_stress() -> FundingStressResult:
    return FundingStressResult(
        composite_value=4.0,
        components={"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0},
        component_percentiles={"a": 50.0, "b": 50.0, "c": 50.0, "d": 50.0},
        components_above_percentile=0,
        alert_active=False,
        state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
    )


def _calibrated_market_liquidity() -> MarketLiquidityResult:
    return MarketLiquidityResult(
        composite_value=3.0,
        components={"a": 1.0, "b": 1.0, "c": 1.0},
        percentile_60d=50.0,
        alert_active=False,
        state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
    )


def test_assemble_q6_blocks_from_inputs_calibrated_path() -> None:
    """Fully-calibrated inputs route through ``assemble_q6_blocks`` and produce 5 blocks."""
    inputs = Q6Inputs(
        as_of=datetime(2026, 5, 15, tzinfo=UTC),
        yield_curve=_make_yield_curve_result(),
        inflation=_make_inflation_result(),
        dollar_attribution=_make_dollar_result(),
        funding_stress=_calibrated_funding_stress(),
        market_liquidity=_calibrated_market_liquidity(),
        macro_surprise_anomalies=(),
    )
    blocks = assemble_q6_blocks_from_inputs(inputs)
    block_ids = [b.block_id for b in blocks]
    assert block_ids == [
        "q6.yield_curve_regime",
        "q6.inflation_regime",
        "q6.dollar_attribution",
        "q6.funding_stress",
        "q6.market_liquidity",
    ]
    # Universal-broadcast audience on every block.
    for block in blocks:
        assert block.audience == frozenset({OutputAudience.UNIVERSAL_BROADCAST})


def test_assemble_q6_blocks_from_inputs_unavailable_path() -> None:
    """Missing yield-curve / inflation / dollar inputs emit unavailable stubs.

    Per ALP-540 these block_ids are tagged ``unavailable`` when the
    upstream FRED series is missing — operator action required, not "give
    it time."
    """
    inputs = Q6Inputs(
        as_of=datetime(2026, 5, 15, tzinfo=UTC),
        yield_curve=None,
        inflation=None,
        dollar_attribution=None,
        funding_stress=_calibrated_funding_stress(),
        market_liquidity=_calibrated_market_liquidity(),
        macro_surprise_anomalies=(),
    )
    blocks = assemble_q6_blocks_from_inputs(inputs)
    block_ids = [b.block_id for b in blocks]
    assert block_ids == [
        "q6.yield_curve_regime",
        "q6.inflation_regime",
        "q6.dollar_attribution",
        "q6.funding_stress",
        "q6.market_liquidity",
    ]
    for block_id in ("q6.yield_curve_regime", "q6.inflation_regime", "q6.dollar_attribution"):
        block = next(b for b in blocks if b.block_id == block_id)
        assert block.calibration_state is CalibrationState.UNAVAILABLE
        assert block.payload == {}


def test_assemble_q6_blocks_from_inputs_emits_one_block_per_surprise() -> None:
    """Each macro-surprise tuple produces a distinct ``q6.macro_surprise_anomaly.<id>`` block."""
    inputs = Q6Inputs(
        as_of=datetime(2026, 5, 15, tzinfo=UTC),
        yield_curve=_make_yield_curve_result(),
        inflation=_make_inflation_result(),
        dollar_attribution=_make_dollar_result(),
        funding_stress=_calibrated_funding_stress(),
        market_liquidity=_calibrated_market_liquidity(),
        macro_surprise_anomalies=(
            (
                "CPIAUCSL",
                AnomalyFlag(
                    name="macro_surprise_anomaly", magnitude=3.0, severity="investigate_now"
                ),
            ),
            (
                "PCEPI",
                AnomalyFlag(
                    name="macro_surprise_anomaly", magnitude=2.5, severity="investigate_now"
                ),
            ),
        ),
    )
    blocks = assemble_q6_blocks_from_inputs(inputs)
    surprise_blocks = [b for b in blocks if b.block_id.startswith("q6.macro_surprise_anomaly.")]
    assert {b.block_id for b in surprise_blocks} == {
        "q6.macro_surprise_anomaly.CPIAUCSL",
        "q6.macro_surprise_anomaly.PCEPI",
    }
