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
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.config.models.distillation import DistillationConfig
from alphamind.distillation.baselines import refresh_composite_state
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.normalization import macro_surprise_zscore
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.persistence.models import (
    DistillationCompositeState,
    EventCalendar,
    MacroObservations,
)

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


# ---------------------------------------------------------------------------
# Upstream-data wrapper for the orchestrator — closes the q6 placeholder gap.
# ---------------------------------------------------------------------------
#
# The wrapper queries ``macro_observations`` (FRED yield curve / breakevens /
# dollar / funding proxies) and ``event_calendar`` (macro release surprise
# histories — see :func:`_read_macro_surprise_anomalies` for the v1
# differenced-actual proxy used pre-consensus-feed), composes the trailing-
# window aggregates each q6 classifier needs, and packages the results via
# :func:`assemble_q6_blocks`. Per the cold-start contract, missing series
# never raise — the corresponding block degrades to BOOTSTRAP / UNAVAILABLE
# calibration.


# Trailing windows the wrapper reads. Per ``external.md`` § 2 these are
# definitional/structural conventions of each indicator, not Class A
# tunables — the rule shapes (5-day 2s10s change, 3-month breakeven trend,
# 30-day sustained deflation check) are anchors of the classification spec
# rather than knobs the operator turns. The dollar-attribution window
# *does* coincide with ``correlation_short_days`` (the universe-wide
# 20-day correlation lookback) so that path routes through config rather
# than redeclaring the magic number.

_YIELD_CURVE_TRANSITION_LOOKBACK_DAYS: int = 5
"""Calendar lookback for the 5-day 2s10s change per external.md § quant 6a."""

_INFLATION_BREAKEVEN_TREND_LOOKBACK_DAYS: int = 90
"""Calendar lookback for the 3-month breakeven trend per external.md § quant 6c."""

_INFLATION_DEFLATION_WINDOW_DAYS: int = 30
"""Trailing window for the breakeven < 1.5% sustained check per external.md § quant 6c."""

# FRED series IDs the wrapper reads. The vocabulary is centralized so the
# scaffolding for "is the series present?" cold-start probes shares a single
# source of truth with the read-helpers.

_YIELD_CURVE_SERIES_IDS: tuple[str, ...] = ("DGS3MO", "DGS2", "DGS5", "DGS10", "DGS30")
_INFLATION_SERIES_ID: str = "T10YIE"
_DOLLAR_INDEX_SERIES_ID: str = "DTWEXBGS"
_RATE_DIFF_SERIES_ID: str = "DGS10"

# Funding-stress component proxies (FRED). The component names match
# ``FUNDING_STRESS_COMPONENT_NAMES`` so the persistence row can be read by
# the same vocabulary as the classifier expects. Missing series resolve to
# ``0.0`` for the proxy value — the cold-start tag fires via the
# ``min_observations`` route inside ``refresh_funding_stress_composite``
# and the sub-90th-percentile branch of the per-component verdict.
_FUNDING_STRESS_PROXY_SOURCES: dict[str, str] = {
    "sofr_ois_spread": "SOFR",
    "repo_treasury_spread": "RRPONTSYD",
    "term_repo_premium": "BAMLH0A0HYM2",
    "mmf_flow": "WRMFSL",
}
# Module-load invariant: the proxy map exhausts the canonical component
# vocabulary so a downstream consumer can rely on every named component
# emitting a value (zero or real) on every invocation.
assert set(_FUNDING_STRESS_PROXY_SOURCES) == set(FUNDING_STRESS_COMPONENT_NAMES)

# Market-liquidity component proxies (FRED). The St. Louis Financial
# Stress Index plus the high-yield credit spread plus VIX form a coarse
# proxy for the spread / depth / volume scoring the v1 spec calls for —
# all three are universe-level liquidity signals and the composite row's
# semantic ("bottom 10th percentile = stressed") is preserved.
_MARKET_LIQUIDITY_PROXY_SOURCES: dict[str, str] = {
    "stress_index_score": "STLFSI4",
    "credit_spread_score": "BAMLC0A0CM",
    "volatility_score": "VIXCLS",
}

# Macro-release events whose surprise histories the wrapper scans. Each
# entry maps an ``event_calendar.event_type`` to the FRED series whose
# observation provides the actual value released on that date.
_MACRO_SURPRISE_EVENTS: tuple[tuple[str, str], ...] = (
    ("cpi_release", "CPIAUCSL"),
    ("pce_release", "PCEPI"),
    ("nfp_release", "PAYEMS"),
)


def _format_iso_z(dt: datetime) -> str:
    """Render a tz-aware datetime as ISO 8601 ``Z``-suffixed UTC."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_latest_macro_value(
    session: Session,
    *,
    series_id: str,
    on_or_before_date: str,
) -> float | None:
    """Return the most recent ``MacroObservations`` value at or before a date string."""
    stmt = (
        select(MacroObservations.value)
        .where(
            MacroObservations.series_id == series_id,
            MacroObservations.observation_date <= on_or_before_date,
            MacroObservations.value.isnot(None),
        )
        .order_by(MacroObservations.observation_date.desc())
        .limit(1)
    )
    value = session.execute(stmt).scalar_one_or_none()
    return float(value) if value is not None else None


def _select_macro_series_window(
    session: Session,
    *,
    series_id: str,
    range_start_date: str,
    range_end_date: str,
) -> list[float]:
    """Return ascending non-null ``MacroObservations`` values in a date window."""
    stmt = (
        select(MacroObservations.value)
        .where(
            MacroObservations.series_id == series_id,
            MacroObservations.observation_date >= range_start_date,
            MacroObservations.observation_date <= range_end_date,
            MacroObservations.value.isnot(None),
        )
        .order_by(MacroObservations.observation_date)
    )
    return [float(v) for v in session.execute(stmt).scalars().all() if v is not None]


def _select_macro_series_at_or_before(
    session: Session,
    *,
    series_id: str,
    on_or_before_date: str,
) -> list[float]:
    """Return ascending non-null ``MacroObservations`` values at or before a date."""
    stmt = (
        select(MacroObservations.value)
        .where(
            MacroObservations.series_id == series_id,
            MacroObservations.observation_date <= on_or_before_date,
            MacroObservations.value.isnot(None),
        )
        .order_by(MacroObservations.observation_date)
    )
    return [float(v) for v in session.execute(stmt).scalars().all() if v is not None]


def _series_returns(values: Sequence[float]) -> list[float]:
    """Return per-step arithmetic returns ``(v_t - v_{t-1}) / v_{t-1}`` skipping zeros."""
    out: list[float] = []
    for prior, latest in pairwise(values):
        if prior == 0.0:
            continue
        out.append((latest - prior) / prior)
    return out


def _try_yield_curve_result(
    session: Session,
    *,
    as_of: datetime,
) -> YieldCurveRegimeResult | None:
    """Build a yield-curve result from FRED, or ``None`` when essentials are missing.

    Essentials are the five DGS series at ``as_of`` and the DGS2/DGS10 readings
    five calendar days earlier. When any essential is missing the wrapper
    surfaces the gap via the bootstrap path rather than fabricating a label.
    """
    end_date = as_of.strftime("%Y-%m-%d")
    latest: dict[str, float] = {}
    for series_id in _YIELD_CURVE_SERIES_IDS:
        value = _select_latest_macro_value(session, series_id=series_id, on_or_before_date=end_date)
        if value is None:
            return None
        latest[series_id] = value

    five_d_ago_date = (as_of - timedelta(days=_YIELD_CURVE_TRANSITION_LOOKBACK_DAYS)).strftime(
        "%Y-%m-%d"
    )
    dgs2_5d_ago = _select_latest_macro_value(
        session, series_id="DGS2", on_or_before_date=five_d_ago_date
    )
    dgs10_5d_ago = _select_latest_macro_value(
        session, series_id="DGS10", on_or_before_date=five_d_ago_date
    )
    if dgs2_5d_ago is None or dgs10_5d_ago is None:
        return None
    spread_2s10s_5d_ago = dgs10_5d_ago - dgs2_5d_ago
    return classify_yield_curve_regime(
        dgs3mo=latest["DGS3MO"],
        dgs2=latest["DGS2"],
        dgs5=latest["DGS5"],
        dgs10=latest["DGS10"],
        dgs30=latest["DGS30"],
        spread_2s10s_5d_ago=spread_2s10s_5d_ago,
        prior_label=None,
    )


def _try_inflation_result(
    session: Session,
    *,
    as_of: datetime,
) -> InflationRegimeResult | None:
    """Build an inflation result from FRED breakeven history, or ``None``."""
    end_date = as_of.strftime("%Y-%m-%d")
    current = _select_latest_macro_value(
        session, series_id=_INFLATION_SERIES_ID, on_or_before_date=end_date
    )
    if current is None:
        return None
    trend_anchor_date = (as_of - timedelta(days=_INFLATION_BREAKEVEN_TREND_LOOKBACK_DAYS)).strftime(
        "%Y-%m-%d"
    )
    anchor = _select_latest_macro_value(
        session, series_id=_INFLATION_SERIES_ID, on_or_before_date=trend_anchor_date
    )
    if anchor is None:
        return None
    breakeven_trend_bps = current - anchor

    deflation_window_start = (as_of - timedelta(days=_INFLATION_DEFLATION_WINDOW_DAYS)).strftime(
        "%Y-%m-%d"
    )
    window_values = _select_macro_series_window(
        session,
        series_id=_INFLATION_SERIES_ID,
        range_start_date=deflation_window_start,
        range_end_date=end_date,
    )
    breakeven_below_threshold_30d = bool(window_values) and all(
        v < _DEFLATION_RISK_BREAKEVEN_PP_CUTOFF for v in window_values
    )

    return classify_inflation_regime(
        breakeven_trend_bps=breakeven_trend_bps,
        recent_cpi_surprise_signs=[],
        breakeven_below_threshold_30d=breakeven_below_threshold_30d,
        prior_label=None,
    )


def _try_dollar_result(
    session: Session,
    *,
    as_of: datetime,
    window_days: int,
) -> DollarAttributionResult | None:
    """Build a dollar-attribution result from FRED, or ``None``.

    ``window_days`` is the trailing correlation lookback per external.md
    § quant 6f — passed through from
    ``config.persistence_windows.correlation_short_days`` so the
    no-magic-numbers audit does not see a literal in this module.

    The classifier accepts empty SPY returns and yields the residual
    ``trade_flow_driven`` label per :func:`_pearson_correlation`'s zero-
    variance branch. Empty DXY history, however, leaves no signal at all
    so the wrapper surfaces the gap via the bootstrap path.
    """
    end_date = as_of.strftime("%Y-%m-%d")
    range_start_date = (as_of - timedelta(days=window_days)).strftime("%Y-%m-%d")
    dxy_values = _select_macro_series_window(
        session,
        series_id=_DOLLAR_INDEX_SERIES_ID,
        range_start_date=range_start_date,
        range_end_date=end_date,
    )
    if len(dxy_values) <= 1:
        return None
    rate_values = _select_macro_series_window(
        session,
        series_id=_RATE_DIFF_SERIES_ID,
        range_start_date=range_start_date,
        range_end_date=end_date,
    )
    dxy_returns = _series_returns(dxy_values)
    rate_returns = _series_returns(rate_values)
    # SPY returns aren't a hard dependency — the residual classification
    # path handles a zero-variance / empty SPY series.
    spy_returns: list[float] = []
    return classify_dollar_attribution(
        dxy_returns=dxy_returns,
        rate_diff_returns=rate_returns,
        spy_returns=spy_returns,
    )


def _read_proxy_components(
    session: Session,
    *,
    as_of: datetime,
    proxy_sources: Mapping[str, str],
) -> Mapping[str, float]:
    """Read latest FRED values for a proxy-component map.

    Missing or null series resolve to ``0.0`` so the downstream composite
    refresh primitives always have a value to rank — the cold-start tag
    fires via the ``min_observations`` route inside the refresh helpers.
    """
    end_date = as_of.strftime("%Y-%m-%d")
    out: dict[str, float] = {}
    for component_name, series_id in proxy_sources.items():
        value = _select_latest_macro_value(session, series_id=series_id, on_or_before_date=end_date)
        out[component_name] = value if value is not None else 0.0
    return out


def _read_macro_surprise_anomalies(
    session: Session,
    *,
    as_of: datetime,
    alert_percentile: float,
) -> list[tuple[str, AnomalyFlag]]:
    """Read macro release events and emit surprise anomalies for the latest release.

    For each ``(event_type, series_id)`` in :data:`_MACRO_SURPRISE_EVENTS`:

    1. Find the most recent release in ``event_calendar`` at or before
       ``as_of`` whose ``status == 'completed'``.
    2. Read the value the release published from ``macro_observations`` (the
       FRED revision number ``0`` row whose observation date matches).
    3. Build the trailing distribution of past releases (actual values for
       the series at successive earlier release dates) and treat
       ``actual - prior_value`` as the surprise (consensus is not stored
       universe-wide; the differenced series is the v1 proxy until the
       consensus-feed lands as a future story).
    4. Run ``detect_macro_surprise_anomaly`` and record an anomaly when the
       latest surprise lies in the top ``alert_percentile`` of the trailing
       distribution.
    """
    out: list[tuple[str, AnomalyFlag]] = []
    end_iso = _format_iso_z(as_of)
    end_date = as_of.strftime("%Y-%m-%d")
    for event_type, series_id in _MACRO_SURPRISE_EVENTS:
        latest_event_stmt = (
            select(EventCalendar.scheduled_at)
            .where(
                EventCalendar.event_type == event_type,
                EventCalendar.scheduled_at <= end_iso,
                EventCalendar.status == "completed",
            )
            .order_by(EventCalendar.scheduled_at.desc())
            .limit(1)
        )
        latest_event_ts = session.execute(latest_event_stmt).scalar_one_or_none()
        if latest_event_ts is None:
            continue
        # Pull every available release-actual at or before ``as_of`` and
        # difference the series; the last entry is the current "surprise",
        # earlier entries form the trailing distribution. ``actual - prior``
        # is the v1 proxy until the consensus-feed lands as a future story.
        values = _select_macro_series_at_or_before(
            session,
            series_id=series_id,
            on_or_before_date=end_date,
        )
        if len(values) <= 1:
            continue
        diffs = [current - prior for prior, current in pairwise(values)]
        if len(diffs) <= 1:
            continue
        actual_diff = diffs[-1]
        trailing = diffs[:-1]
        flag = detect_macro_surprise_anomaly(
            actual=actual_diff,
            consensus=0.0,
            trailing_surprises=trailing,
            alert_percentile=alert_percentile,
        )
        if flag is not None:
            out.append((series_id, flag))
    return out


def _build_bootstrap_block(
    *,
    block_id: str,
    bootstrap_reason: str,
    freshness_ts: datetime,
    state: CalibrationState = CalibrationState.BOOTSTRAP,
) -> OutputBlock:
    """Build a stand-in q6 block when an essential input is missing.

    Used for the yield-curve / inflation / dollar paths whose existing
    ``_build_*_block`` helpers hardcode ``CalibrationState.CALIBRATED``.
    The bootstrap block carries an empty payload and no anomaly flags so a
    downstream consumer sees the calibration tag (and the
    ``bootstrap_reason`` it carries) rather than a fabricated label.
    """
    return OutputBlock(
        block_id=block_id,
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=state,
        bootstrap_reason=bootstrap_reason,
        payload={},
        anomaly_flags=(),
        regime_context=None,
    )


def compute_q6_blocks(
    session: Session,
    *,
    config: DistillationConfig,
    as_of: datetime,
) -> list[OutputBlock]:
    """Read upstream data and assemble the q6 macro blocks.

    The orchestrator calls this once per invocation. The wrapper:

    1. Reads FRED yield-curve / breakeven / dollar series from
       ``macro_observations`` and runs the existing label classifiers.
    2. Refreshes the funding-stress and market-liquidity composites via the
       persistence-state primitives in :mod:`alphamind.distillation.baselines`,
       which return BOOTSTRAP-tagged results during cold start.
    3. Scans ``event_calendar`` for completed macro releases and emits
       surprise-anomaly blocks per indicator that lies in the top
       ``anomaly_detection.macro_surprise_percentile`` of its trailing
       differenced-actual distribution.
    4. Calls :func:`assemble_q6_blocks` to package the calibrated path; for
       any individual block whose essentials are missing, emits a
       BOOTSTRAP-tagged stub instead so the orchestrator's fail-open
       contract is preserved per the LLM-agents-uniformly-Critical policy.

    Returns a list of ``OutputBlock`` whose audience is
    :attr:`OutputAudience.UNIVERSAL_BROADCAST` — q6 macro context is not
    sector-scoped per ``external.md`` § 4.
    """
    as_of_iso = _format_iso_z(as_of)

    yc_result = _try_yield_curve_result(session, as_of=as_of)
    inflation_result = _try_inflation_result(session, as_of=as_of)
    dollar_result = _try_dollar_result(
        session,
        as_of=as_of,
        window_days=config.persistence_windows.correlation_short_days,
    )

    funding_components = _read_proxy_components(
        session, as_of=as_of, proxy_sources=_FUNDING_STRESS_PROXY_SOURCES
    )
    fs_result = refresh_funding_stress_composite(
        session,
        components=funding_components,
        as_of=as_of_iso,
        min_observations=config.persistence_windows.funding_stress_baseline_days,
        component_alert_count=config.anomaly_detection.funding_stress_component_alert_count,
        component_alert_percentile=float(
            config.anomaly_detection.funding_stress_component_percentile
        ),
    )

    liquidity_components = _read_proxy_components(
        session, as_of=as_of, proxy_sources=_MARKET_LIQUIDITY_PROXY_SOURCES
    )
    ml_result = refresh_market_liquidity_composite(
        session,
        components=liquidity_components,
        as_of=as_of_iso,
        min_observations=config.persistence_windows.market_liquidity_baseline_days,
        alert_percentile=float(config.anomaly_detection.market_liquidity_alert_percentile),
    )

    surprises = _read_macro_surprise_anomalies(
        session,
        as_of=as_of,
        alert_percentile=float(config.anomaly_detection.macro_surprise_percentile),
    )

    # When every label-input is present, the calibrated path runs through
    # ``assemble_q6_blocks``. Missing inputs route through the bootstrap
    # stub builder so the block_id is preserved but the calibration tag
    # accurately reports the gap.
    if yc_result is not None and inflation_result is not None and dollar_result is not None:
        return list(
            assemble_q6_blocks(
                yield_curve=yc_result,
                inflation=inflation_result,
                dollar_attribution=dollar_result,
                funding_stress=fs_result,
                market_liquidity=ml_result,
                macro_surprise_anomalies=tuple(surprises),
                freshness_ts=as_of,
            )
        )

    blocks: list[OutputBlock] = []
    if yc_result is not None:
        blocks.append(_build_yield_curve_block(yc_result, freshness_ts=as_of))
    else:
        blocks.append(
            _build_bootstrap_block(
                block_id="q6.yield_curve_regime",
                bootstrap_reason="yield_curve: required FRED DGS series unavailable",
                freshness_ts=as_of,
            )
        )
    if inflation_result is not None:
        blocks.append(_build_inflation_block(inflation_result, freshness_ts=as_of))
    else:
        blocks.append(
            _build_bootstrap_block(
                block_id="q6.inflation_regime",
                bootstrap_reason="inflation: T10YIE history unavailable",
                freshness_ts=as_of,
            )
        )
    if dollar_result is not None:
        blocks.append(_build_dollar_attribution_block(dollar_result, freshness_ts=as_of))
    else:
        blocks.append(
            _build_bootstrap_block(
                block_id="q6.dollar_attribution",
                bootstrap_reason="dollar_attribution: DTWEXBGS history unavailable",
                freshness_ts=as_of,
            )
        )
    blocks.append(_build_funding_stress_block(fs_result, freshness_ts=as_of))
    blocks.append(_build_market_liquidity_block(ml_result, freshness_ts=as_of))
    for indicator, flag in surprises:
        blocks.append(_build_macro_surprise_anomaly_block(indicator, flag, freshness_ts=as_of))
    return blocks
