"""Q6 macro indicators and funding-stress composite — story 02-distillation-layer/08c.

Implements the deterministic macro computations from
``docs/design/02-distillation-layer/external.md`` § 2 From macro and rates
(quant 6):

- :func:`classify_yield_curve_regime` — quant 6a yield-curve regime
  classifier (five labels plus transition flag).
- :func:`classify_inflation_regime` — quant 6c inflation regime classifier
  (four labels plus transition flag).
- :func:`classify_dollar_attribution` — quant 6f dollar-move attribution
  (three labels by largest absolute correlation over a trailing window).
- :func:`refresh_funding_stress_composite` — quant 6e four-component
  funding-stress composite, persisted via story 07's
  ``refresh_composite_state``.
- :func:`refresh_market_liquidity_composite` — market-wide liquidity
  composite (the partner of funding stress), persisted via story 07.
- :func:`detect_macro_surprise_anomaly` — macro release surprise anomaly
  per ``external.md`` § 3 Anomaly detection.
- :func:`assemble_q6_blocks` — pack the above into ``OutputBlock`` instances
  with ``audience = UNIVERSAL_BROADCAST``.

The yield-curve and inflation rule cutoffs (25 bps for 2s10s steepening
detection, 30 bps for breakeven-trend, 1.5% for deflation-risk, 0.5% for
significant DXY moves) are definitional rather than tunable: per the story
they are not exposed as Class A. They are documented inline as named
constants so a reader can verify them against
``external.md`` § 2 without leaving the source.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.baselines import refresh_composite_state
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.normalization import macro_surprise_zscore
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.persistence.models import DistillationCompositeState

# ---------------------------------------------------------------------------
# Yield curve regime
# ---------------------------------------------------------------------------


class YieldCurveRegimeLabel(StrEnum):
    """The five yield-curve regime labels per ``external.md`` § quant 6a."""

    NORMAL_UPWARD_SLOPING = "normal_upward_sloping"
    FLAT = "flat"
    INVERTED = "inverted"
    STEEPENING = "steepening"
    FLATTENING = "flattening"


# Definitional cutoffs for the yield-curve classifier. Per the story Notes:
# these are documented inline as ``# regime classification cutoff per
# external.md § 2`` — not exposed as Class A because tightening risks
# producing labels the analysts and PM aren't trained to read.

# regime classification cutoff per external.md § 2 quant 6a — 5-day 2s10s
# change at or above this magnitude triggers a steepening / flattening
# transition label.
_YIELD_CURVE_TRANSITION_BPS_CUTOFF: float = 0.25
# regime classification cutoff per external.md § 2 quant 6a — |2s10s spread|
# at or below this is treated as flat.
_YIELD_CURVE_FLAT_BPS_CUTOFF: float = 0.10


@dataclass(frozen=True, slots=True)
class YieldCurveRegimeResult:
    """Output of :func:`classify_yield_curve_regime`."""

    label: YieldCurveRegimeLabel
    spread_2s10s: float
    spread_3m10y: float
    spread_5s30s: float
    regime_transition: bool


def classify_yield_curve_regime(
    *,
    dgs3mo: float,
    dgs2: float,
    dgs5: float,
    dgs10: float,
    dgs30: float,
    spread_2s10s_5d_ago: float,
    prior_label: YieldCurveRegimeLabel | None,
) -> YieldCurveRegimeResult:
    """Classify the yield curve into one of five regime labels.

    Spreads are computed as long minus short (in percentage points, since the
    FRED ``DGS*`` series are quoted that way):

    - ``2s10s = DGS10 - DGS2``
    - ``3m10y = DGS10 - DGS3MO``
    - ``5s30s = DGS30 - DGS5``

    Classification:

    - ``steepening`` / ``flattening`` (transition labels) fire when the
      trailing 5-day change in the 2s10s spread exceeds
      ``_YIELD_CURVE_TRANSITION_BPS_CUTOFF`` percentage points (25 bps).
    - ``inverted`` when 2s10s < 0 outside the flat band.
    - ``flat`` when |2s10s| <= ``_YIELD_CURVE_FLAT_BPS_CUTOFF``.
    - ``normal_upward_sloping`` otherwise.

    ``regime_transition`` fires when the resolved label differs from
    ``prior_label`` (None on the first invocation never triggers a transition).
    """
    spread_2s10s = dgs10 - dgs2
    spread_3m10y = dgs10 - dgs3mo
    spread_5s30s = dgs30 - dgs5

    five_day_change = spread_2s10s - spread_2s10s_5d_ago
    if abs(five_day_change) >= _YIELD_CURVE_TRANSITION_BPS_CUTOFF:
        label = (
            YieldCurveRegimeLabel.STEEPENING
            if five_day_change > 0
            else YieldCurveRegimeLabel.FLATTENING
        )
    elif abs(spread_2s10s) <= _YIELD_CURVE_FLAT_BPS_CUTOFF:
        label = YieldCurveRegimeLabel.FLAT
    elif spread_2s10s < 0:
        label = YieldCurveRegimeLabel.INVERTED
    else:
        label = YieldCurveRegimeLabel.NORMAL_UPWARD_SLOPING

    transition = prior_label is not None and prior_label is not label
    return YieldCurveRegimeResult(
        label=label,
        spread_2s10s=spread_2s10s,
        spread_3m10y=spread_3m10y,
        spread_5s30s=spread_5s30s,
        regime_transition=transition,
    )


# ---------------------------------------------------------------------------
# Inflation regime
# ---------------------------------------------------------------------------


class InflationRegimeLabel(StrEnum):
    """The four inflation regime labels per ``external.md`` § quant 6c."""

    HOT = "hot"
    COOLING = "cooling"
    STABLE = "stable"
    DEFLATION_RISK = "deflation_risk"


# Definitional cutoffs for the inflation classifier. Per the story Notes:
# documented inline as ``# regime classification cutoff per external.md
# § 2 quant 6c`` — not exposed as Class A.

# regime classification cutoff per external.md § 2 quant 6c — trailing
# 3-month breakeven trend magnitude (in percentage points) for the
# hot/cooling labels. ``trend_bps`` is in pp so 30 bps = 0.30.
_INFLATION_BREAKEVEN_TREND_PP_CUTOFF: float = 0.30
# regime classification cutoff per external.md § 2 quant 6c — 10y breakeven
# below this level, sustained over 30 days, triggers the deflation_risk label.
_DEFLATION_RISK_BREAKEVEN_PP_CUTOFF: float = 1.5


@dataclass(frozen=True, slots=True)
class InflationRegimeResult:
    """Output of :func:`classify_inflation_regime`."""

    label: InflationRegimeLabel
    breakeven_trend_bps: float
    cpi_surprise_signs_positive: int
    cpi_surprise_signs_negative: int
    regime_transition: bool


def classify_inflation_regime(
    *,
    breakeven_trend_bps: float,
    recent_cpi_surprise_signs: list[int],
    breakeven_below_threshold_30d: bool,
    prior_label: InflationRegimeLabel | None,
) -> InflationRegimeResult:
    """Classify the inflation regime into one of four labels.

    Inputs:

    - ``breakeven_trend_bps`` — trailing 3-month change in the 10y breakeven
      (``T10YIE``), in percentage points (positive means breakevens rising).
    - ``recent_cpi_surprise_signs`` — list of integer signs (``+1``, ``-1``,
      ``0``) for the last 3 CPI releases. Sourced from the macro-release
      surprise component (``actual - consensus``); the caller computes the
      sign and passes it through.
    - ``breakeven_below_threshold_30d`` — whether the 10y breakeven has stayed
      below ``_DEFLATION_RISK_BREAKEVEN_PP_CUTOFF`` (1.5%) over the trailing
      30 days. Computed by the caller against ``macro_observations``.

    Classification (rule table per ``external.md`` § quant 6c):

    - ``hot`` when breakeven trend ≥ +``_INFLATION_BREAKEVEN_TREND_PP_CUTOFF``
      AND surprises are positive in the last 3 releases (no negatives).
    - ``cooling`` when breakeven trend ≤ -``_INFLATION_BREAKEVEN_TREND_PP_CUTOFF``
      AND surprises are negative in the last 3 releases (no positives).
    - ``deflation_risk`` when ``breakeven_below_threshold_30d`` is True.
    - ``stable`` otherwise.

    The ``deflation_risk`` rule is checked first per
    ``external.md`` § quant 6c — sustained low breakevens are the dominant
    signal regardless of the recent trend.
    """
    positives = sum(1 for sign in recent_cpi_surprise_signs if sign > 0)
    negatives = sum(1 for sign in recent_cpi_surprise_signs if sign < 0)

    if breakeven_below_threshold_30d:
        label = InflationRegimeLabel.DEFLATION_RISK
    elif (
        breakeven_trend_bps >= _INFLATION_BREAKEVEN_TREND_PP_CUTOFF
        and negatives == 0
        and positives > 0
    ):
        label = InflationRegimeLabel.HOT
    elif (
        breakeven_trend_bps <= -_INFLATION_BREAKEVEN_TREND_PP_CUTOFF
        and positives == 0
        and negatives > 0
    ):
        label = InflationRegimeLabel.COOLING
    else:
        label = InflationRegimeLabel.STABLE

    transition = prior_label is not None and prior_label is not label
    return InflationRegimeResult(
        label=label,
        breakeven_trend_bps=breakeven_trend_bps,
        cpi_surprise_signs_positive=positives,
        cpi_surprise_signs_negative=negatives,
        regime_transition=transition,
    )


# ---------------------------------------------------------------------------
# Dollar move attribution
# ---------------------------------------------------------------------------


class DollarAttributionLabel(StrEnum):
    """The three dollar-move attribution labels per ``external.md`` § quant 6f."""

    RATE_DIFFERENTIAL_DRIVEN = "rate_differential_driven"
    RISK_SENTIMENT_DRIVEN = "risk_sentiment_driven"
    TRADE_FLOW_DRIVEN = "trade_flow_driven"


# Definitional cutoff for dollar-move attribution. The single-factor
# discriminant compares |rate correlation| vs |SPY correlation| over the
# trailing 20-day window; whichever is dominant by at least this absolute
# margin wins. Residual (neither dominant) → trade_flow_driven.

# regime classification cutoff per external.md § 2 quant 6f — minimum
# |correlation-coefficient| margin for a single factor to claim the label.
# Below this, neither factor explains the move and the residual
# ``trade_flow_driven`` label fires.
_DOLLAR_ATTRIBUTION_DOMINANCE_MARGIN: float = 0.20


@dataclass(frozen=True, slots=True)
class DollarAttributionResult:
    """Output of :func:`classify_dollar_attribution`."""

    label: DollarAttributionLabel
    rate_correlation: float
    risk_correlation: float


def _pearson_correlation(a: list[float], b: list[float]) -> float:
    """Pearson correlation of two equal-length series.

    Returns ``0.0`` for empty input or zero-variance series. The dollar
    attribution heuristic treats ``0.0`` as "no signal" → caller falls
    through to the residual label.
    """
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    dot = 0.0
    var_a = 0.0
    var_b = 0.0
    for x, y in zip(a, b, strict=True):
        da = x - mean_a
        db = y - mean_b
        dot += da * db
        var_a += da * da
        var_b += db * db
    if var_a == 0.0 or var_b == 0.0:
        return 0.0
    return float(dot / (var_a * var_b) ** 0.5)


def classify_dollar_attribution(
    *,
    dxy_returns: list[float],
    rate_diff_returns: list[float],
    spy_returns: list[float],
) -> DollarAttributionResult:
    """Classify a DXY move's likely driver into one of three labels.

    The heuristic is a deliberate simplification of full multi-factor
    attribution: compare |corr(DXY, rate_diff)| against |corr(DXY, SPY)| over
    the trailing 20-day window the caller passes.

    - When the larger |correlation| exceeds the smaller by at least
      ``_DOLLAR_ATTRIBUTION_DOMINANCE_MARGIN``, the dominant factor wins:
      ``rate_differential_driven`` if the rate factor; ``risk_sentiment_driven``
      if the SPY factor.
    - Otherwise the residual ``trade_flow_driven`` label fires.

    The simplification is documented per the story Notes: "the simplest
    version that produces a non-arbitrary label."
    """
    rate_corr = _pearson_correlation(dxy_returns, rate_diff_returns)
    risk_corr = _pearson_correlation(dxy_returns, spy_returns)

    abs_rate = abs(rate_corr)
    abs_risk = abs(risk_corr)
    margin = abs(abs_rate - abs_risk)

    if margin < _DOLLAR_ATTRIBUTION_DOMINANCE_MARGIN:
        label = DollarAttributionLabel.TRADE_FLOW_DRIVEN
    elif abs_rate > abs_risk:
        label = DollarAttributionLabel.RATE_DIFFERENTIAL_DRIVEN
    else:
        label = DollarAttributionLabel.RISK_SENTIMENT_DRIVEN

    return DollarAttributionResult(
        label=label,
        rate_correlation=rate_corr,
        risk_correlation=risk_corr,
    )


# ---------------------------------------------------------------------------
# Funding-stress and market-liquidity composites
# ---------------------------------------------------------------------------


FUNDING_STRESS_COMPOSITE_KIND = "funding_stress"
MARKET_LIQUIDITY_COMPOSITE_KIND = "market_liquidity"

# The four named funding-stress components per ``external.md`` § quant 6e.
# A constant so the assembly site and the persistence column constraints
# share a single vocabulary.
FUNDING_STRESS_COMPONENT_NAMES: tuple[str, ...] = (
    "sofr_ois_spread",
    "repo_treasury_spread",
    "term_repo_premium",
    "mmf_flow",
)


@dataclass(frozen=True, slots=True)
class FundingStressResult:
    """Output of :func:`refresh_funding_stress_composite`."""

    composite_value: float
    components: Mapping[str, float]
    component_percentiles: Mapping[str, float]
    components_above_percentile: int
    alert_active: bool
    state: CalibrationState
    bootstrap_reason: str | None


def _select_component_history(
    session: Session,
    *,
    composite_kind: str,
    as_of: str,
) -> list[Mapping[str, float]]:
    """Return component_breakdown_json from prior rows strictly before ``as_of``.

    Each prior row's JSON is parsed once; the caller does the per-component
    extraction. Rows whose JSON cannot be decoded are skipped — they came from
    an older schema or a faulty write and should not poison the percentile
    series.
    """
    stmt = (
        select(DistillationCompositeState.component_breakdown_json)
        .where(
            DistillationCompositeState.composite_kind == composite_kind,
            DistillationCompositeState.as_of < as_of,
        )
        .order_by(DistillationCompositeState.as_of)
    )
    out: list[Mapping[str, float]] = []
    for raw in session.execute(stmt).scalars().all():
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, dict):
            out.append(
                {str(k): float(v) for k, v in decoded.items() if isinstance(v, (int, float))}
            )
    return out


def _component_percentile(value: float, history: Sequence[float]) -> float:
    """Percentile rank of ``value`` against ``history`` in 0..100.

    Uses the "<= value" convention so a value at the top of the distribution
    reports 100. An empty history returns 0.0 — the caller decides whether
    that's signal or noise.
    """
    if not history:
        return 0.0
    le = sum(1 for x in history if x <= value)
    return float(le) / float(len(history)) * 100.0


def refresh_funding_stress_composite(
    session: Session,
    *,
    components: Mapping[str, float],
    as_of: str,
    min_observations: int,
    component_alert_count: int,
    component_alert_percentile: float,
) -> FundingStressResult:
    """Refresh the funding-stress composite with per-component alert logic.

    Per ``external.md`` § quant 6e the composite has four components (SOFR-OIS
    spread, repo-Treasury spread, term repo premium, MMF flow). For each:

    1. Read the trailing component series from prior
       ``distillation_composite_state.component_breakdown_json`` rows.
    2. Compute the current value's percentile against that trailing series.

    Aggregate alert: ``alert_active = True`` when ≥
    ``component_alert_count`` of the four components are at or above the
    ``component_alert_percentile``.

    The composite sum (a number — not the alert) plus the per-component JSON
    is persisted via story 07's :func:`refresh_composite_state` so the row
    plumbing matches every other composite. The aggregate ``alert_active``
    column is overridden after the refresh because story 07's primitive
    derives its alert from the composite-percentile, not from per-component
    counts. The override keeps the read shape (sum + JSON + alert flag)
    consistent across composite kinds without forcing the primitive to know
    about per-component alert math.
    """
    # Read history first so percentiles reflect strictly prior rows.
    prior_history = _select_component_history(
        session, composite_kind=FUNDING_STRESS_COMPOSITE_KIND, as_of=as_of
    )
    component_percentiles: dict[str, float] = {}
    for name, current in components.items():
        trailing = [row[name] for row in prior_history if name in row]
        component_percentiles[name] = _component_percentile(current, trailing)

    components_above = sum(
        1
        for percentile in component_percentiles.values()
        if percentile >= component_alert_percentile
    )
    alert_active = components_above >= component_alert_count

    # Persist via story 07's primitive. We pass ``alert_percentile`` so its
    # internal calculation completes, but immediately overwrite the
    # ``alert_active`` column with our per-component verdict so the row
    # reflects the funding-stress alert semantic, not the composite-sum
    # percentile semantic.
    persistence_result = refresh_composite_state(
        session,
        composite_kind=FUNDING_STRESS_COMPOSITE_KIND,
        components=components,
        as_of=as_of,
        min_observations=min_observations,
        alert_percentile=component_alert_percentile,
        alert_direction="upper",
    )

    # Override the alert column on the row that refresh_composite_state just
    # wrote so the persisted alert flag matches the per-component verdict.
    row = session.execute(
        select(DistillationCompositeState).where(
            DistillationCompositeState.composite_kind == FUNDING_STRESS_COMPOSITE_KIND,
            DistillationCompositeState.as_of == as_of,
        )
    ).scalar_one()
    row.alert_active = int(alert_active)
    session.commit()

    composite_value = float(persistence_result.value["composite_value"])
    return FundingStressResult(
        composite_value=composite_value,
        components=dict(components),
        component_percentiles=component_percentiles,
        components_above_percentile=components_above,
        alert_active=alert_active,
        state=persistence_result.state,
        bootstrap_reason=persistence_result.bootstrap_reason,
    )


@dataclass(frozen=True, slots=True)
class MarketLiquidityResult:
    """Output of :func:`refresh_market_liquidity_composite`."""

    composite_value: float
    components: Mapping[str, float]
    percentile_60d: float
    alert_active: bool
    state: CalibrationState
    bootstrap_reason: str | None


def refresh_market_liquidity_composite(
    session: Session,
    *,
    components: Mapping[str, float],
    as_of: str,
    min_observations: int,
    alert_percentile: float,
) -> MarketLiquidityResult:
    """Refresh the market-wide liquidity composite.

    Per the story Notes, the formula is "a normalized average of (sector-ETF
    spread percentiles + universe-average spread percentiles + universe-
    average volume percentile inverted)" — chosen for simplicity. The caller
    has already done the per-input percentile computation; this function
    receives the named scores via ``components`` and routes them through
    story 07's :func:`refresh_composite_state`.

    Alert direction is ``"lower"``: a market_liquidity composite in the
    bottom ``alert_percentile`` (default 10th) means liquidity has thinned
    relative to the trailing 60-day distribution — a stress signal.
    """
    persistence_result = refresh_composite_state(
        session,
        composite_kind=MARKET_LIQUIDITY_COMPOSITE_KIND,
        components=components,
        as_of=as_of,
        min_observations=min_observations,
        alert_percentile=alert_percentile,
        alert_direction="lower",
    )
    return MarketLiquidityResult(
        composite_value=float(persistence_result.value["composite_value"]),
        components=dict(components),
        percentile_60d=float(persistence_result.value["percentile_60d"]),
        alert_active=bool(persistence_result.value["alert_active"]),
        state=persistence_result.state,
        bootstrap_reason=persistence_result.bootstrap_reason,
    )


# ---------------------------------------------------------------------------
# Macro surprise anomaly
# ---------------------------------------------------------------------------


def _absolute_percentile_rank(history: Sequence[float], value: float) -> float:
    """Percentile of ``|value|`` against the |history| distribution in 0..100.

    Empty history returns 0.0; the caller decides whether that's signal.
    """
    if not history:
        return 0.0
    abs_history = [abs(x) for x in history]
    le = sum(1 for x in abs_history if x <= abs(value))
    return float(le) / float(len(abs_history)) * 100.0


def detect_macro_surprise_anomaly(
    *,
    actual: float,
    consensus: float,
    trailing_surprises: Sequence[float],
    alert_percentile: float,
) -> AnomalyFlag | None:
    """Flag a macro release surprise that lands in the top 10% of trailing magnitudes.

    Per ``external.md`` § 3 Anomaly detection (Macro):

    1. Compute the surprise as ``actual - consensus`` per story 06's
       :func:`macro_surprise` framing.
    2. Rank ``|surprise|`` against the trailing distribution of past
       ``|surprise|``. Default trailing window is 24 months of releases.
    3. Fire when the rank reaches ``alert_percentile`` (default 90 = top 10%).
       Magnitude is the z-score from story 06's
       :func:`macro_surprise_zscore` — the same primitive every macro
       analyst-facing layer uses for surprise scaling.
    4. Severity is ``investigate_now`` per
       ``external.md`` § 3 Anomaly detection — surprise spikes are
       triggers for the adaptive research layer.

    Indicator name is *not* an argument — the caller pairs the returned
    flag with the indicator at the assembly site
    (:func:`assemble_q6_blocks` keys block_ids by indicator). This keeps
    the detector pure with respect to the surprise math.

    Returns ``None`` when the surprise is below the threshold or the
    trailing distribution is too short to compute a z-score.
    """
    surprise = actual - consensus
    if not trailing_surprises:
        return None
    rank = _absolute_percentile_rank(trailing_surprises, surprise)
    if rank < alert_percentile:
        return None
    try:
        z_score = macro_surprise_zscore(surprise, trailing_surprises)
    except ValueError:
        # Zero-variance trailing distribution → cannot z-score; the
        # anomaly is real but unscalable. Surface as a flag carrying the
        # raw surprise magnitude in absolute units.
        z_score = surprise
    return AnomalyFlag(
        name="macro_surprise_anomaly",
        magnitude=float(z_score),
        severity="investigate_now",
    )


# ---------------------------------------------------------------------------
# Output assembly — every Q6 block is UNIVERSAL_BROADCAST
# ---------------------------------------------------------------------------
#
# Per ``external.md`` § 4 Persistent state and composites and the analysis-
# layer agent contract, macro context is not sector-scoped: every analyst
# reads it, the synthesizer reads it via the correlation/regime brief, and
# the strategist reads it through its input bundle. UNIVERSAL_BROADCAST
# encodes that audience choice once per block.

UNIVERSAL_BROADCAST_AUDIENCE: frozenset[OutputAudience] = frozenset(
    {OutputAudience.UNIVERSAL_BROADCAST}
)


def _build_yield_curve_block(
    result: YieldCurveRegimeResult,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    return OutputBlock(
        block_id="q6.yield_curve_regime",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "label": result.label.value,
            "spread_2s10s": result.spread_2s10s,
            "spread_3m10y": result.spread_3m10y,
            "spread_5s30s": result.spread_5s30s,
            "regime_transition": result.regime_transition,
        },
        anomaly_flags=(),
        regime_context=None,
    )


def _build_inflation_block(
    result: InflationRegimeResult,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    return OutputBlock(
        block_id="q6.inflation_regime",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "label": result.label.value,
            "breakeven_trend_bps": result.breakeven_trend_bps,
            "cpi_surprise_signs_positive": result.cpi_surprise_signs_positive,
            "cpi_surprise_signs_negative": result.cpi_surprise_signs_negative,
            "regime_transition": result.regime_transition,
        },
        anomaly_flags=(),
        regime_context=None,
    )


def _build_dollar_attribution_block(
    result: DollarAttributionResult,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    return OutputBlock(
        block_id="q6.dollar_attribution",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "label": result.label.value,
            "rate_correlation": result.rate_correlation,
            "risk_correlation": result.risk_correlation,
        },
        anomaly_flags=(),
        regime_context=None,
    )


def _build_funding_stress_block(
    result: FundingStressResult,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    flags: tuple[AnomalyFlag, ...] = ()
    if result.alert_active:
        # Magnitude carries the count of components above the alert
        # percentile, so a downstream consumer can read severity proxy
        # without re-deriving it. Severity is ``investigate_now`` per
        # ``external.md`` § 3 Anomaly detection (Macro).
        flags = (
            AnomalyFlag(
                name="funding_stress_alert",
                magnitude=float(result.components_above_percentile),
                severity="investigate_now",
            ),
        )
    return OutputBlock(
        block_id="q6.funding_stress",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=result.state,
        bootstrap_reason=result.bootstrap_reason,
        payload={
            "composite_value": result.composite_value,
            "components": dict(result.components),
            "component_percentiles": dict(result.component_percentiles),
            "components_above_percentile": result.components_above_percentile,
            "alert_active": result.alert_active,
        },
        anomaly_flags=flags,
        regime_context=None,
    )


def _build_market_liquidity_block(
    result: MarketLiquidityResult,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    flags: tuple[AnomalyFlag, ...] = ()
    if result.alert_active:
        flags = (
            AnomalyFlag(
                name="market_liquidity_alert",
                magnitude=float(result.percentile_60d),
                severity="investigate_now",
            ),
        )
    return OutputBlock(
        block_id="q6.market_liquidity",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=result.state,
        bootstrap_reason=result.bootstrap_reason,
        payload={
            "composite_value": result.composite_value,
            "components": dict(result.components),
            "percentile_60d": result.percentile_60d,
            "alert_active": result.alert_active,
        },
        anomaly_flags=flags,
        regime_context=None,
    )


def _build_macro_surprise_anomaly_block(
    indicator: str,
    flag: AnomalyFlag,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    return OutputBlock(
        block_id=f"q6.macro_surprise_anomaly.{indicator}",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "indicator": indicator,
            "z_score": flag.magnitude,
        },
        anomaly_flags=(flag,),
        regime_context=None,
    )


def assemble_q6_blocks(
    *,
    yield_curve: YieldCurveRegimeResult,
    inflation: InflationRegimeResult,
    dollar_attribution: DollarAttributionResult,
    funding_stress: FundingStressResult,
    market_liquidity: MarketLiquidityResult,
    macro_surprise_anomalies: Sequence[tuple[str, AnomalyFlag]],
    freshness_ts: datetime,
) -> tuple[OutputBlock, ...]:
    """Pack the Q6 computations into ``OutputBlock`` instances.

    Every block carries ``audience = UNIVERSAL_BROADCAST`` per the story
    scope: macro context is not sector-scoped — analyst, synthesizer, and
    strategist all read it.

    The macro surprise anomalies are emitted as one block per detection
    (block_id ``q6.macro_surprise_anomaly.<indicator>``) so consumers can
    address them individually; the other five blocks are singletons keyed
    by their fixed ``block_id``.
    """
    blocks: list[OutputBlock] = [
        _build_yield_curve_block(yield_curve, freshness_ts=freshness_ts),
        _build_inflation_block(inflation, freshness_ts=freshness_ts),
        _build_dollar_attribution_block(dollar_attribution, freshness_ts=freshness_ts),
        _build_funding_stress_block(funding_stress, freshness_ts=freshness_ts),
        _build_market_liquidity_block(market_liquidity, freshness_ts=freshness_ts),
    ]
    for indicator, flag in macro_surprise_anomalies:
        blocks.append(
            _build_macro_surprise_anomaly_block(indicator, flag, freshness_ts=freshness_ts)
        )
    return tuple(blocks)
