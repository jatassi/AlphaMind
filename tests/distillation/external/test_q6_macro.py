"""Tests for the Q6 macro indicators and funding-stress composite — story 02-distillation-layer/08c.

Cover yield curve regime classification, inflation regime classification,
dollar move attribution, the four-component funding-stress composite, the
market-wide liquidity composite, and the macro surprise anomaly. All outputs
carry ``audience = UNIVERSAL_BROADCAST`` per the story scope.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
)
from alphamind.distillation.q6_macro import (
    DollarAttributionLabel,
    DollarAttributionResult,
    FundingStressResult,
    InflationRegimeLabel,
    InflationRegimeResult,
    MarketLiquidityResult,
    YieldCurveRegimeLabel,
    YieldCurveRegimeResult,
    assemble_q6_blocks,
    classify_dollar_attribution,
    classify_inflation_regime,
    classify_yield_curve_regime,
    detect_macro_surprise_anomaly,
    refresh_funding_stress_composite,
    refresh_market_liquidity_composite,
)
from alphamind.persistence.models import (
    Base,
    DistillationCompositeState,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Shared in-memory engine / session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with the full distillation schema."""
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


AS_OF = "2026-04-25T14:30:00Z"
FRESHNESS_TS = datetime(2026, 4, 25, 14, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Yield curve regime classification — five labels and the transition flag
# ---------------------------------------------------------------------------


class TestYieldCurveRegime:
    """``classify_yield_curve_regime`` produces one of the five documented labels.

    The five labels are ``normal_upward_sloping``, ``flat``, ``inverted``,
    ``steepening``, and ``flattening`` per the story scope. Steepening and
    flattening are transition labels, fired when the trailing 5-day change in
    the 2s10s spread exceeds 25 bps.
    """

    def test_normal_upward_sloping_when_long_above_short_with_stable_history(self) -> None:
        # 10y > 2y by 70 bps; 5-day change small (< 25 bps); not a transition.
        result = classify_yield_curve_regime(
            dgs3mo=3.50,
            dgs2=3.80,
            dgs5=4.20,
            dgs10=4.50,
            dgs30=4.70,
            spread_2s10s_5d_ago=0.65,  # current 0.70; change = 5 bps
            prior_label=None,
        )
        assert result.label is YieldCurveRegimeLabel.NORMAL_UPWARD_SLOPING
        assert result.regime_transition is False

    def test_flat_when_2s10s_within_flat_band(self) -> None:
        # 10y = 2y so spread is zero; not a transition.
        result = classify_yield_curve_regime(
            dgs3mo=4.00,
            dgs2=4.00,
            dgs5=4.05,
            dgs10=4.00,
            dgs30=4.10,
            spread_2s10s_5d_ago=0.05,  # change = -5 bps
            prior_label=None,
        )
        assert result.label is YieldCurveRegimeLabel.FLAT

    def test_inverted_when_2s10s_negative_outside_flat_band(self) -> None:
        # 2y > 10y by 80 bps; 5-day change small.
        result = classify_yield_curve_regime(
            dgs3mo=5.00,
            dgs2=4.80,
            dgs5=4.30,
            dgs10=4.00,
            dgs30=3.90,
            spread_2s10s_5d_ago=-0.85,  # change = +5 bps (still inverted)
            prior_label=None,
        )
        assert result.label is YieldCurveRegimeLabel.INVERTED

    def test_steepening_when_5d_2s10s_change_above_25bps_positive(self) -> None:
        # Curve steepening: 2s10s went from -0.20 to +0.15 → +35 bps.
        result = classify_yield_curve_regime(
            dgs3mo=4.00,
            dgs2=3.80,
            dgs5=3.85,
            dgs10=3.95,
            dgs30=4.10,
            spread_2s10s_5d_ago=-0.20,  # change = +35 bps → steepening
            prior_label=None,
        )
        assert result.label is YieldCurveRegimeLabel.STEEPENING

    def test_flattening_when_5d_2s10s_change_below_neg_25bps(self) -> None:
        # 2s10s went from +0.50 to +0.10 → -40 bps.
        result = classify_yield_curve_regime(
            dgs3mo=3.50,
            dgs2=3.80,
            dgs5=3.85,
            dgs10=3.90,
            dgs30=4.05,
            spread_2s10s_5d_ago=0.50,  # change = -40 bps → flattening
            prior_label=None,
        )
        assert result.label is YieldCurveRegimeLabel.FLATTENING

    def test_transition_flag_fires_only_on_label_change(self) -> None:
        # Same inputs as the inverted test but prior label was normal:
        # the resolved label changed → regime_transition is True.
        result = classify_yield_curve_regime(
            dgs3mo=5.00,
            dgs2=4.80,
            dgs5=4.30,
            dgs10=4.00,
            dgs30=3.90,
            spread_2s10s_5d_ago=-0.85,
            prior_label=YieldCurveRegimeLabel.NORMAL_UPWARD_SLOPING,
        )
        assert result.label is YieldCurveRegimeLabel.INVERTED
        assert result.regime_transition is True

    def test_transition_flag_off_when_label_matches_prior(self) -> None:
        # Same inputs but prior label was already inverted: no transition.
        result = classify_yield_curve_regime(
            dgs3mo=5.00,
            dgs2=4.80,
            dgs5=4.30,
            dgs10=4.00,
            dgs30=3.90,
            spread_2s10s_5d_ago=-0.85,
            prior_label=YieldCurveRegimeLabel.INVERTED,
        )
        assert result.label is YieldCurveRegimeLabel.INVERTED
        assert result.regime_transition is False


# ---------------------------------------------------------------------------
# Inflation regime classification
# ---------------------------------------------------------------------------


class TestInflationRegime:
    """``classify_inflation_regime`` produces one of the four documented labels.

    The four labels are ``hot``, ``cooling``, ``stable``, and
    ``deflation_risk``. Per the story scope:

    - ``hot``: trailing 3-month breakeven trend ≥ +30 bps AND CPI surprises
      positive in the last 3 releases.
    - ``cooling``: breakeven trend ≤ -30 bps AND surprises negative.
    - ``deflation_risk``: breakeven < 1.5% sustained over 30 days.
    - ``stable``: none of the above.
    """

    def test_hot_when_breakeven_trending_up_and_surprises_positive(self) -> None:
        # +35 bps trend, last 3 surprises all +1 (positive).
        result = classify_inflation_regime(
            breakeven_trend_bps=0.35,
            recent_cpi_surprise_signs=[1, 1, 1],
            breakeven_below_threshold_30d=False,
            prior_label=None,
        )
        assert result.label is InflationRegimeLabel.HOT
        assert result.regime_transition is False

    def test_cooling_when_breakeven_trending_down_and_surprises_negative(self) -> None:
        # -35 bps trend, last 3 surprises all -1 (negative).
        result = classify_inflation_regime(
            breakeven_trend_bps=-0.35,
            recent_cpi_surprise_signs=[-1, -1, -1],
            breakeven_below_threshold_30d=False,
            prior_label=None,
        )
        assert result.label is InflationRegimeLabel.COOLING

    def test_stable_when_neither_hot_nor_cooling_pattern(self) -> None:
        # +10 bps trend, mixed surprises → stable.
        result = classify_inflation_regime(
            breakeven_trend_bps=0.10,
            recent_cpi_surprise_signs=[1, -1, 0],
            breakeven_below_threshold_30d=False,
            prior_label=None,
        )
        assert result.label is InflationRegimeLabel.STABLE

    def test_deflation_risk_when_breakeven_below_threshold_30d(self) -> None:
        # Sustained low breakeven dominates regardless of the recent trend.
        result = classify_inflation_regime(
            breakeven_trend_bps=0.40,  # would otherwise look hot
            recent_cpi_surprise_signs=[1, 1, 1],
            breakeven_below_threshold_30d=True,
            prior_label=None,
        )
        assert result.label is InflationRegimeLabel.DEFLATION_RISK

    def test_inflation_transition_flag_fires_on_label_change(self) -> None:
        # Prior was stable, current is hot.
        result = classify_inflation_regime(
            breakeven_trend_bps=0.35,
            recent_cpi_surprise_signs=[1, 1, 1],
            breakeven_below_threshold_30d=False,
            prior_label=InflationRegimeLabel.STABLE,
        )
        assert result.label is InflationRegimeLabel.HOT
        assert result.regime_transition is True


# ---------------------------------------------------------------------------
# Dollar move attribution
# ---------------------------------------------------------------------------


class TestDollarAttribution:
    """``classify_dollar_attribution`` produces one of the three labels.

    Per the story scope, the label is decided by the largest |correlation|
    coefficient over the trailing 20-day window:

    - ``rate_differential_driven`` when |rate-diff correlation| dominates.
    - ``risk_sentiment_driven`` when |SPY correlation| dominates.
    - ``trade_flow_driven`` when neither is dominant (residual).

    The simplification — single-factor max-|coefficient| — is documented in
    the source.
    """

    def test_rate_differential_driven_when_rate_correlation_dominates(self) -> None:
        # Perfect positive correlation with rate differential, no spy correlation.
        dxy = [0.1, 0.2, 0.3, 0.4, 0.5]
        rates = [0.1, 0.2, 0.3, 0.4, 0.5]
        spy = [0.0, 0.0, 0.0, 0.0, 0.1]  # near-zero variance with dxy
        result = classify_dollar_attribution(
            dxy_returns=dxy,
            rate_diff_returns=rates,
            spy_returns=spy,
        )
        assert result.label is DollarAttributionLabel.RATE_DIFFERENTIAL_DRIVEN

    def test_risk_sentiment_driven_when_spy_correlation_dominates(self) -> None:
        # USD up = SPY down (typical risk-off pattern).
        dxy = [0.5, 0.4, 0.3, 0.2, 0.1]
        rates = [0.1, 0.0, 0.0, 0.0, 0.1]  # weak/inverse with dxy
        spy = [-0.5, -0.4, -0.3, -0.2, -0.1]  # negative correlation with dxy
        result = classify_dollar_attribution(
            dxy_returns=dxy,
            rate_diff_returns=rates,
            spy_returns=spy,
        )
        assert result.label is DollarAttributionLabel.RISK_SENTIMENT_DRIVEN

    def test_trade_flow_driven_when_neither_correlation_dominates(self) -> None:
        # Both correlations near zero — residual label.
        dxy = [0.1, 0.2, 0.3, 0.4, 0.5]
        rates = [0.5, 0.1, 0.4, 0.2, 0.3]  # essentially random
        spy = [0.3, 0.5, 0.2, 0.4, 0.1]  # essentially random
        result = classify_dollar_attribution(
            dxy_returns=dxy,
            rate_diff_returns=rates,
            spy_returns=spy,
        )
        assert result.label is DollarAttributionLabel.TRADE_FLOW_DRIVEN


# ---------------------------------------------------------------------------
# Funding-stress composite — alert at 2 of 4 components above the percentile
# ---------------------------------------------------------------------------


# Test-side constants (not Class A bypasses — these mirror the YAML defaults
# the orchestrator would pass in production). The no-magic-numbers audit
# only scans src/alphamind/distillation/, so naming them here is fine.
FUNDING_STRESS_BASELINE_DAYS = 60
FUNDING_STRESS_MIN_OBSERVATIONS = 60
FUNDING_STRESS_COMPONENT_ALERT_COUNT = 2
FUNDING_STRESS_COMPONENT_PERCENTILE = 90.0


def _seed_funding_stress_history(
    session: Session,
    *,
    n_rows: int,
    component_values: list[float],
) -> None:
    """Seed ``n_rows`` of prior funding_stress composite history.

    Each row carries the same ``component_values`` so the component
    distributions are well-defined for the percentile calculation under test.
    """
    for index in range(n_rows):
        components = {
            "sofr_ois_spread": component_values[0] + index * 0.0001,
            "repo_treasury_spread": component_values[1] + index * 0.0001,
            "term_repo_premium": component_values[2] + index * 0.0001,
            "mmf_flow": component_values[3] + index * 0.0001,
        }
        session.add(
            DistillationCompositeState(
                composite_kind="funding_stress",
                as_of=f"2026-02-{(index % 28) + 1:02d}T0{index % 10}:00:00Z",
                composite_value=sum(components.values()),
                component_breakdown_json=json.dumps(components),
                percentile_60d=0.0,
                alert_active=0,
                calibration_state="calibrated",
                ingested_at="2026-02-01T00:00:00Z",
            )
        )


class TestFundingStressComposite:
    """Funding-stress composite alert at 2-of-4 components above the 90th percentile.

    Per the story scope:

    - Per-component: compute the trailing 60-day percentile from prior rows
      in ``distillation_composite_state.component_breakdown_json``.
    - Composite alert: ``alert_active = True`` when ≥
      ``funding_stress_component_alert_count`` (default 2) of the 4
      components are above ``funding_stress_component_percentile``
      (default 90th).
    """

    def test_alert_fires_at_exactly_two_components_above_threshold(self, session: Session) -> None:
        # Seed the trailing history so each component's distribution is
        # well-defined. New SOFR-OIS and Repo-Treasury values are above the
        # 90th-percentile cut; the other two are below. Count = 2 → alert.
        _seed_funding_stress_history(
            session,
            n_rows=FUNDING_STRESS_BASELINE_DAYS,
            component_values=[0.10, 0.10, 0.10, 0.10],  # baseline ~0.10 per
        )
        session.commit()

        result = refresh_funding_stress_composite(
            session,
            components={
                "sofr_ois_spread": 0.50,  # well above baseline → top percentile
                "repo_treasury_spread": 0.50,  # well above baseline → top percentile
                "term_repo_premium": 0.05,  # below baseline → low percentile
                "mmf_flow": 0.05,  # below baseline → low percentile
            },
            as_of=AS_OF,
            min_observations=FUNDING_STRESS_MIN_OBSERVATIONS,
            component_alert_count=FUNDING_STRESS_COMPONENT_ALERT_COUNT,
            component_alert_percentile=FUNDING_STRESS_COMPONENT_PERCENTILE,
        )

        assert result.alert_active is True
        assert result.components_above_percentile == 2

    def test_alert_suppressed_at_one_component_above_threshold(self, session: Session) -> None:
        # Only one component above its trailing 90th percentile → alert off.
        _seed_funding_stress_history(
            session,
            n_rows=FUNDING_STRESS_BASELINE_DAYS,
            component_values=[0.10, 0.10, 0.10, 0.10],
        )
        session.commit()

        result = refresh_funding_stress_composite(
            session,
            components={
                "sofr_ois_spread": 0.50,  # only this one is elevated
                "repo_treasury_spread": 0.05,
                "term_repo_premium": 0.05,
                "mmf_flow": 0.05,
            },
            as_of=AS_OF,
            min_observations=FUNDING_STRESS_MIN_OBSERVATIONS,
            component_alert_count=FUNDING_STRESS_COMPONENT_ALERT_COUNT,
            component_alert_percentile=FUNDING_STRESS_COMPONENT_PERCENTILE,
        )

        assert result.alert_active is False
        assert result.components_above_percentile == 1

    def test_persisted_row_carries_alert_state(self, session: Session) -> None:
        # The persisted row's alert_active column reflects the per-component
        # verdict, not the composite-percentile from refresh_composite_state.
        _seed_funding_stress_history(
            session,
            n_rows=FUNDING_STRESS_BASELINE_DAYS,
            component_values=[0.10, 0.10, 0.10, 0.10],
        )
        session.commit()

        refresh_funding_stress_composite(
            session,
            components={
                "sofr_ois_spread": 0.50,
                "repo_treasury_spread": 0.50,
                "term_repo_premium": 0.05,
                "mmf_flow": 0.05,
            },
            as_of=AS_OF,
            min_observations=FUNDING_STRESS_MIN_OBSERVATIONS,
            component_alert_count=FUNDING_STRESS_COMPONENT_ALERT_COUNT,
            component_alert_percentile=FUNDING_STRESS_COMPONENT_PERCENTILE,
        )

        row = session.execute(
            DistillationCompositeState.__table__.select().where(
                DistillationCompositeState.composite_kind == "funding_stress",
                DistillationCompositeState.as_of == AS_OF,
            )
        ).first()
        assert row is not None
        assert row.alert_active == 1


# ---------------------------------------------------------------------------
# Market-wide liquidity composite — alert at the 10th percentile boundary
# ---------------------------------------------------------------------------


MARKET_LIQUIDITY_BASELINE_DAYS = 60
MARKET_LIQUIDITY_MIN_OBSERVATIONS = 60
MARKET_LIQUIDITY_ALERT_PERCENTILE = 10.0


def _seed_market_liquidity_history(
    session: Session,
    *,
    n_rows: int,
    base_value: float,
    increment: float,
) -> None:
    """Seed ``n_rows`` of prior market_liquidity composite rows.

    Composite values march upward by ``increment`` so the trailing
    distribution is well-defined for the percentile calculation under test.
    """
    for index in range(n_rows):
        session.add(
            DistillationCompositeState(
                composite_kind="market_liquidity",
                as_of=f"2026-02-{(index % 28) + 1:02d}T0{index % 10}:00:00Z",
                composite_value=base_value + index * increment,
                component_breakdown_json=json.dumps({"seed": index}),
                percentile_60d=0.0,
                alert_active=0,
                calibration_state="calibrated",
                ingested_at="2026-02-01T00:00:00Z",
            )
        )


class TestMarketLiquidityComposite:
    """Market-liquidity alert fires at exactly the 10th percentile boundary.

    Per the story scope: ``alert_active = True`` when the composite is in the
    bottom ``market_liquidity_alert_percentile`` (default 10) of the trailing
    60-day distribution.
    """

    def test_alert_fires_when_in_bottom_10th_percentile(self, session: Session) -> None:
        # Seed history climbing from 10.0 to 15.9; new value 9.0 is below all
        # of them → bottom percentile → alert.
        _seed_market_liquidity_history(
            session,
            n_rows=MARKET_LIQUIDITY_BASELINE_DAYS,
            base_value=10.0,
            increment=0.1,
        )
        session.commit()

        result = refresh_market_liquidity_composite(
            session,
            components={
                "spread_score": 5.0,
                "depth_score": 2.0,
                "volume_score": 2.0,
            },  # composite sum 9.0 → below all seeded values
            as_of=AS_OF,
            min_observations=MARKET_LIQUIDITY_MIN_OBSERVATIONS,
            alert_percentile=MARKET_LIQUIDITY_ALERT_PERCENTILE,
        )

        assert result.alert_active is True
        assert result.percentile_60d is not None
        assert result.percentile_60d <= MARKET_LIQUIDITY_ALERT_PERCENTILE

    def test_alert_suppressed_when_above_10th_percentile(self, session: Session) -> None:
        # New composite at 13.0 is around the median of [10.0..15.9] → no alert.
        _seed_market_liquidity_history(
            session,
            n_rows=MARKET_LIQUIDITY_BASELINE_DAYS,
            base_value=10.0,
            increment=0.1,
        )
        session.commit()

        result = refresh_market_liquidity_composite(
            session,
            components={
                "spread_score": 5.0,
                "depth_score": 4.0,
                "volume_score": 4.0,
            },  # sum 13.0 → near the median
            as_of=AS_OF,
            min_observations=MARKET_LIQUIDITY_MIN_OBSERVATIONS,
            alert_percentile=MARKET_LIQUIDITY_ALERT_PERCENTILE,
        )

        assert result.alert_active is False
        assert result.percentile_60d is not None
        assert result.percentile_60d > MARKET_LIQUIDITY_ALERT_PERCENTILE


# ---------------------------------------------------------------------------
# Macro surprise anomaly
# ---------------------------------------------------------------------------


MACRO_SURPRISE_PERCENTILE = 90.0


class TestMacroSurpriseAnomaly:
    """``detect_macro_surprise_anomaly`` fires at the 90th-percentile boundary.

    Per ``external.md`` § 3 Anomaly detection and the threshold-calibration
    spec: the surprise (``actual - consensus``) is ranked by magnitude
    against the trailing distribution of past surprises. The flag fires when
    the current ``|surprise|`` lies in the top
    ``macro_surprise_percentile`` (default 90, i.e. top 10%) of the trailing
    distribution. Magnitude is the z-score from story 06's
    ``macro_surprise_zscore``.
    """

    def test_anomaly_fires_when_surprise_in_top_10_percent(self) -> None:
        # Trailing surprises cluster near zero with stdev ~0.04.
        # Current surprise of 1.0 is far above the 90th |surprise| percentile.
        # Magnitudes vary across the window — a uniform absolute distribution
        # would collapse to a zero-variance rank (None) per ALP-545.
        trailing = [(0.04 + 0.0005 * i) * ((-1) ** i) for i in range(100)]
        result = detect_macro_surprise_anomaly(
            actual=2.5,
            consensus=1.5,  # surprise = +1.0
            trailing_surprises=trailing,
            alert_percentile=MACRO_SURPRISE_PERCENTILE,
        )
        assert result is not None
        assert result.name == "macro_surprise_anomaly"
        assert result.severity == "investigate_now"
        # Magnitude is the z-score, not the raw surprise.
        assert result.magnitude > 0  # large positive z-score for +1.0 surprise

    def test_anomaly_suppressed_below_90th_percentile(self) -> None:
        # Trailing surprises range -1.0..+1.0; current 0.05 is below all
        # cutoffs.
        trailing = [(i - 50) * 0.02 for i in range(100)]  # -1.0..+0.98
        result = detect_macro_surprise_anomaly(
            actual=1.5,
            consensus=1.45,  # surprise = +0.05 — small
            trailing_surprises=trailing,
            alert_percentile=MACRO_SURPRISE_PERCENTILE,
        )
        assert result is None

    def test_anomaly_fires_at_exactly_90th_percentile_boundary(self) -> None:
        # 100 trailing |surprises| of integer values 1..100.
        # The 90th-percentile cut is exactly 90. A current |surprise| of
        # 90 lands on the boundary and the flag fires (>= boundary).
        trailing = [float(i + 1) for i in range(100)]
        result = detect_macro_surprise_anomaly(
            actual=190.0,
            consensus=100.0,  # surprise = 90.0 exactly
            trailing_surprises=trailing,
            alert_percentile=MACRO_SURPRISE_PERCENTILE,
        )
        assert result is not None


# ---------------------------------------------------------------------------
# Output assembly — UNIVERSAL_BROADCAST audience for every Q6 block
# ---------------------------------------------------------------------------


def _make_yield_curve_result(
    label: YieldCurveRegimeLabel = YieldCurveRegimeLabel.NORMAL_UPWARD_SLOPING,
) -> YieldCurveRegimeResult:
    return YieldCurveRegimeResult(
        label=label,
        spread_2s10s=0.70,
        spread_3m10y=1.00,
        spread_5s30s=0.50,
        regime_transition=False,
    )


def _make_inflation_result(
    label: InflationRegimeLabel = InflationRegimeLabel.STABLE,
) -> InflationRegimeResult:
    return InflationRegimeResult(
        label=label,
        breakeven_trend_bps=0.10,
        cpi_surprise_signs_positive=1,
        cpi_surprise_signs_negative=1,
        regime_transition=False,
    )


def _make_dollar_result(
    label: DollarAttributionLabel = DollarAttributionLabel.TRADE_FLOW_DRIVEN,
) -> DollarAttributionResult:
    return DollarAttributionResult(
        label=label,
        rate_correlation=0.10,
        risk_correlation=0.05,
    )


def _make_funding_stress_result(alert: bool = False) -> FundingStressResult:
    return FundingStressResult(
        composite_value=0.40,
        components={
            "sofr_ois_spread": 0.10,
            "repo_treasury_spread": 0.10,
            "term_repo_premium": 0.10,
            "mmf_flow": 0.10,
        },
        component_percentiles={
            "sofr_ois_spread": 50.0,
            "repo_treasury_spread": 50.0,
            "term_repo_premium": 50.0,
            "mmf_flow": 50.0,
        },
        components_above_percentile=2 if alert else 0,
        alert_active=alert,
        state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
    )


def _make_market_liquidity_result(alert: bool = False) -> MarketLiquidityResult:
    return MarketLiquidityResult(
        composite_value=12.0,
        components={"spread_score": 4.0, "depth_score": 4.0, "volume_score": 4.0},
        percentile_60d=50.0 if not alert else 5.0,
        alert_active=alert,
        state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
    )


class TestAssembleQ6Blocks:
    """Output assembly emits one OutputBlock per computation, all UNIVERSAL_BROADCAST.

    The story scope enumerates six block_id values:
    ``q6.yield_curve_regime``, ``q6.inflation_regime``,
    ``q6.dollar_attribution``, ``q6.funding_stress``, ``q6.market_liquidity``,
    and ``q6.macro_surprise_anomaly`` (per detection).
    """

    def test_every_block_is_universal_broadcast(self) -> None:
        blocks = assemble_q6_blocks(
            yield_curve=_make_yield_curve_result(),
            inflation=_make_inflation_result(),
            dollar_attribution=_make_dollar_result(),
            funding_stress=_make_funding_stress_result(),
            market_liquidity=_make_market_liquidity_result(),
            macro_surprise_anomalies=(),
            freshness_ts=FRESHNESS_TS,
        )
        for block in blocks:
            assert block.audience == frozenset({OutputAudience.UNIVERSAL_BROADCAST})

    def test_block_ids_match_story_enumeration(self) -> None:
        blocks = assemble_q6_blocks(
            yield_curve=_make_yield_curve_result(),
            inflation=_make_inflation_result(),
            dollar_attribution=_make_dollar_result(),
            funding_stress=_make_funding_stress_result(),
            market_liquidity=_make_market_liquidity_result(),
            macro_surprise_anomalies=(),
            freshness_ts=FRESHNESS_TS,
        )
        ids = {block.block_id for block in blocks}
        assert ids == {
            "q6.yield_curve_regime",
            "q6.inflation_regime",
            "q6.dollar_attribution",
            "q6.funding_stress",
            "q6.market_liquidity",
        }

    def test_macro_surprise_anomalies_emit_their_own_block_per_detection(self) -> None:
        anomalies = (
            (
                "CPIAUCSL",
                AnomalyFlag(
                    name="macro_surprise_anomaly",
                    magnitude=2.5,
                    severity="investigate_now",
                ),
            ),
            (
                "PCEPI",
                AnomalyFlag(
                    name="macro_surprise_anomaly",
                    magnitude=3.0,
                    severity="investigate_now",
                ),
            ),
        )
        blocks = assemble_q6_blocks(
            yield_curve=_make_yield_curve_result(),
            inflation=_make_inflation_result(),
            dollar_attribution=_make_dollar_result(),
            funding_stress=_make_funding_stress_result(),
            market_liquidity=_make_market_liquidity_result(),
            macro_surprise_anomalies=anomalies,
            freshness_ts=FRESHNESS_TS,
        )
        anomaly_blocks = [b for b in blocks if b.block_id.startswith("q6.macro_surprise")]
        assert len(anomaly_blocks) == 2
        universal = frozenset({OutputAudience.UNIVERSAL_BROADCAST})
        assert all(b.audience == universal for b in anomaly_blocks)
        assert {b.payload["indicator"] for b in anomaly_blocks} == {"CPIAUCSL", "PCEPI"}

    def test_funding_stress_block_carries_alert_flag(self) -> None:
        # When the funding-stress alert is active, the block carries an
        # AnomalyFlag named ``funding_stress_alert`` per the story scope.
        blocks = assemble_q6_blocks(
            yield_curve=_make_yield_curve_result(),
            inflation=_make_inflation_result(),
            dollar_attribution=_make_dollar_result(),
            funding_stress=_make_funding_stress_result(alert=True),
            market_liquidity=_make_market_liquidity_result(),
            macro_surprise_anomalies=(),
            freshness_ts=FRESHNESS_TS,
        )
        funding_block = next(b for b in blocks if b.block_id == "q6.funding_stress")
        flag_names = {flag.name for flag in funding_block.anomaly_flags}
        assert "funding_stress_alert" in flag_names

    def test_yield_curve_block_carries_regime_label_in_payload(self) -> None:
        blocks = assemble_q6_blocks(
            yield_curve=_make_yield_curve_result(label=YieldCurveRegimeLabel.INVERTED),
            inflation=_make_inflation_result(),
            dollar_attribution=_make_dollar_result(),
            funding_stress=_make_funding_stress_result(),
            market_liquidity=_make_market_liquidity_result(),
            macro_surprise_anomalies=(),
            freshness_ts=FRESHNESS_TS,
        )
        yc_block = next(b for b in blocks if b.block_id == "q6.yield_curve_regime")
        assert yc_block.payload["label"] == "inverted"
