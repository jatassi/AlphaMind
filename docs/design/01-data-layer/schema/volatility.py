"""Q11: Volatility Regime — entity definitions.

6 entities covering VIX dynamics, VIX term structure, VVIX, realized vs.
implied vol gap, SKEW index, and composite regime classification. This
domain's output is consumed by nearly every analysis agent for position
sizing and strategy selection.

Design note — Market-wide entities:
    Q11 entities are MARKET-WIDE, NOT per-ticker. They have metadata but
    do NOT have a ticker field. The VolatilityRegime classification (11f)
    is broadcast to every agent as universal context, changing how every
    agent interprets its data and makes decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ._common import (
    DataConfidence,
    Direction,
    InvocationMetadata,
    SignalStrength,
    VolatilityRegime,
)


# ── Supporting types ─────────────────────────────────────────────────────────

class VIXTermStructureState(Enum):
    """Classification of the VIX futures curve shape."""
    STEEP_CONTANGO = "steep_contango"      # Near < far by a large margin; deep short-term complacency
    FLAT_CONTANGO = "flat_contango"        # Mild upward slope; normal market conditions
    FLAT = "flat"                          # Near ≈ far; transitional state
    MILD_BACKWARDATION = "mild_backwardation"  # Near > far slightly; early stress signal
    DEEP_BACKWARDATION = "deep_backwardation"  # Near > far significantly; acute crisis signal


class VIXVVIXMatrix(Enum):
    """The four-state combination of VIX and VVIX levels."""
    LOW_VIX_LOW_VVIX = "low_vix_low_vvix"      # Calm and confident — normal operations
    HIGH_VIX_LOW_VVIX = "high_vix_low_vvix"    # Stressed but stable — market knows how bad it is
    HIGH_VIX_HIGH_VVIX = "high_vix_high_vvix"  # Stressed and uncertain — highest risk state
    LOW_VIX_HIGH_VVIX = "low_vix_high_vvix"    # Calm surface, deep anxiety — sneaky dangerous state


class VIXSKEWMatrix(Enum):
    """The critical pairing of VIX and SKEW levels."""
    LOW_VIX_LOW_SKEW = "low_vix_low_skew"      # Genuine complacency — no hedging
    LOW_VIX_HIGH_SKEW = "low_vix_high_skew"    # Surface calm, deep anxiety — fragile market
    HIGH_VIX_LOW_SKEW = "high_vix_low_skew"    # Fear at-the-money, not tails — expects more of same
    HIGH_VIX_HIGH_SKEW = "high_vix_high_skew"  # Broad fear with tail hedging — worst-case protection


class SKEWTrend(Enum):
    """Direction of SKEW evolution."""
    RISING = "rising"          # Institutional anxiety building
    STABLE = "stable"          # SKEW maintaining level
    DECLINING = "declining"    # Anxiety easing


@dataclass(frozen=True)
class VIXDynamics:
    """Q11:11a — VIX level, percentile rank, rate of change, and z-score.

    VIX spot, percentile rank (1Y/5Y), rate of change (daily, multi-day),
    distance from trailing mean (z-score).

    Source: FRED (VIX free — VIXCLS series) + CBOE delayed
    Fallback: Yahoo Finance for VIX futures
    Cadence: Every invocation
    Feasibility: HIGH — VIX on FRED
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── VIX spot level ──
    vix_spot: float
        # Current VIX level. Nominal range 10–80 under normal/stressed conditions,
        # occasionally exceeding 80 during crises (e.g., March 2020). Interpretation
        # requires context — 20 after months at 12 is different from 20 descending
        # from 35. Level alone is almost useless; velocity and direction matter more.

    # ── Percentile rank (positioning) ──
    vix_pctl_1y: float
        # Percentile rank of current VIX relative to its trailing 1-year range.
        # 0 = lowest VIX in past year, 100 = highest VIX in past year.
        # Expresses whether the current level is "high for this year" or "low."
    vix_pctl_5y: float
        # Same, relative to 5-year range. Longer history reveals whether current
        # conditions are extreme by multi-year standards.

    # ── Rate of change (velocity) ──
    vix_change_1d_pct: float
        # 1-day percentage change in VIX. Positive = rising vol, negative = falling vol.
        # A spike from 14 to 22 in two days (57% move) means the market is repricing
        # risk *right now*. VIX sitting at 22 for a month means elevated risk is already priced.
    vix_change_5d_pct: float
        # 5-day percentage change. Captures multi-day velocity.
    vix_change_20d_pct: float
        # 20-day percentage change. Longer-term trend direction.

    # ── Distance from mean (z-score) ──
    vix_zscore_20d: float
        # How many standard deviations VIX is from its trailing 20-day mean.
        # Extreme deviations (|z| > 2.0) tend to mean-revert. Positive = above mean
        # (vol elevated), negative = below mean (vol compressed).

    # ── Anomalies ──
    vix_spike_detected: bool = False
        # True when VIX has moved > 3 standard deviations from its 20-day mean
        # in a single session or multi-session burst. A short-term anomaly flag
        # (different from regime transitions, which are sustained).


@dataclass(frozen=True)
class VIXTermStructure:
    """Q11:11b — VIX futures term structure shape and roll yield signal.

    Term structure (VIX spot vs. VX1/VX2/VX3), contango/backwardation state,
    steepness, regime classification (steep contango, flat, backwardation),
    roll yield signal.

    Source: FRED + CBOE delayed + Yahoo Finance (VIX futures)
    Cadence: Every invocation
    Feasibility: HIGH
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Futures curve levels ──
    vix_spot: float
        # Current VIX spot for reference.
    vx1_level: float
        # VIX front-month futures (roughly 1-month forward volatility expectations)
    vx2_level: float
        # VIX second-month futures (roughly 2-month forward)
    vx3_level: float
        # VIX third-month futures (roughly 3-month forward). Longer-term expectations.

    # ── Contango/backwardation assessment ──
    term_structure_state: VIXTermStructureState
        # Classification of the curve shape: steep contango (normal but complacent),
        # flat contango (balanced), flat (transition), mild backwardation (early stress),
        # or deep backwardation (crisis).
    vx1_vs_spot_spread: float
        # VX1 - VIX spot. Positive = contango (normal), negative = backwardation (stress).
        # Absolute value indicates steepness.
    vx3_vs_spot_spread: float
        # VX3 - VIX spot. Longer-term spread. Smaller than VX1 spread in normal contango.

    # ── Steepness quantification ──
    curve_steepness: float
        # Numerical measure of curve slope: (VX3 - VX1) / VX1. Positive = upward slope
        # (contango), negative = downward slope (backwardation). Magnitude shows severity.
    contango_regime_flag: str
        # "steep_contango" — VX1 premium > 5% of spot (complacency building, breakout risk)
        # "normal_contango" — 2–5% premium (balanced)
        # "flat" — <2% difference
        # "backwardation" — spot > futures (stress signal)

    # ── Roll yield signal ──
    roll_yield_direction: Direction
        # BULLISH = contango (volatility sellers rewarded, long-vol expensive)
        # BEARISH = backwardation (short-vol expensive, volatility buyers rewarded)
        # NEUTRAL = flat
    roll_yield_magnitude: float
        # Absolute percent difference between front and second month. Magnitude of
        # the roll yield opportunity.


@dataclass(frozen=True)
class VVIX:
    """Q11:11c — Volatility of volatility: VVIX level and VIX/VVIX regime matrix.

    VVIX level and percentile rank, VIX/VVIX 4-state matrix
    (low/low, high/low, high/high, low/high).

    Source: CBOE (delayed)
    Cadence: Every invocation
    Feasibility: HIGH
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── VVIX level ──
    vvix_spot: float
        # Current VVIX level. Measures implied volatility of the VIX itself —
        # "the volatility of volatility." Normal range 10–25; extremes >30 or <8
        # are rare and significant. VVIX answers: how uncertain is the market
        # about the volatility regime itself?
    vvix_pctl_1y: float
        # Percentile rank of VVIX relative to 1-year range. 0 = lowest VVIX in
        # past year (market is confident about VIX), 100 = highest (high uncertainty).

    # ── VIX/VVIX four-state matrix ──
    vix_vvix_state: VIXVVIXMatrix
        # The combination defines four distinct regime states:
        # - Low VIX + low VVIX: calm and confident — normal operations
        # - High VIX + low VVIX: stressed but stable — market thinks it knows how bad it is
        # - High VIX + high VVIX: stressed and uncertain — highest risk, market doesn't know
        #   how much worse it could get
        # - Low VIX + high VVIX: calm surface, deep anxiety — sneaky dangerous state;
        #   historically precedes sharp moves (institutional hedging despite surface calm)

    # ── Thresholds used for classification ──
    vix_threshold_high: float = 20.0
        # VIX level above which we classify as "high VIX" for the matrix.
        # Default 20, adjustable based on regime history.
    vvix_threshold_high: float = 15.0
        # VVIX level above which we classify as "high VVIX" for the matrix.
        # Default 15, adjustable; historically VVIX > 15 is elevated.


@dataclass(frozen=True)
class RealizedImpliedVolGap:
    """Q11:11d — VIX vs. SPX realized vol gap and risk premium tracking.

    VIX vs. SPX realized vol (10-day, 20-day), risk premium (implied - realized),
    gap direction of travel, historical gap percentile.

    Source: Derived from Q11:11a + SPX OHLCV
    Cadence: Every invocation
    Feasibility: HIGH — realized vol from SPX prices
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Realized volatility (SPX) ──
    spx_realized_vol_10d: float
        # Trailing 10-day realized volatility of SPX (annualized %).
        # Computed as the standard deviation of daily returns.
    spx_realized_vol_20d: float
        # Trailing 20-day realized volatility of SPX. Captures recent actual movement.

    # ── Implied volatility ──
    vix_spot: float
        # Current VIX level for reference. VIX is the implied vol market, so VIX ≈ SPX
        # annualized implied volatility (roughly).

    # ── Risk premium (the gap) ──
    risk_premium_10d: float
        # Implied vol (VIX) minus realized vol (10-day SPX realized vol).
        # Positive = market is paying for protection not justified by actual movement.
        # Indicates fear outrunning reality; potential for vol mean-reversion.
        # Negative = actual realized vol exceeding implied vol; market was wrong about risk.
    risk_premium_20d: float
        # Same calculation with 20-day realized vol. Longer-term gap perspective.

    # ── Gap direction and travel ──
    gap_direction: Direction
        # BULLISH = realized vol > implied vol (risk is actually materializing)
        # BEARISH = implied vol > realized vol (fear outrunning reality)
        # NEUTRAL = gap is near its historical center

    # ── Percentile context ──
    premium_pctl_1y: float
        # Historical percentile of the current risk premium relative to 1-year range.
        # 0 = premium at its lowest (best prices for volatility buyers), 100 = highest.
        # Extreme premiums (>90th or <10th percentile) are actionable.

    # ── Gap closing and travel (optional, default values) ──
    gap_closing: bool = False
        # True if realized vol is converging toward implied vol (or vice versa).
        # Closing gap = acceleration signal. Widening gap = potential for mean-reversion.
    gap_travel_pct_change_5d: float = 0.0
        # How much the gap has widened or closed in the last 5 days, as a percentage.
        # Positive = gap widening (fear outrunning reality), negative = gap closing
        # (risk actually materializing).


@dataclass(frozen=True)
class SKEWIndex:
    """Q11:11e — CBOE SKEW level, percentile, and VIX/SKEW combination states.

    SKEW level and percentile rank, VIX/SKEW 4-state combination, SKEW trend.

    Source: CBOE (delayed)
    Cadence: Every invocation
    Feasibility: HIGH
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── SKEW level ──
    skew_spot: float
        # Current SKEW level. Measures implied vol of deep OTM puts relative to ATM
        # options. Normal range 100–150 (index). Values >150 = elevated tail hedging.
        # SKEW is the tail risk barometer — one of the most underappreciated signals.
    skew_pctl_1y: float
        # Percentile rank of SKEW relative to 1-year range. 0 = lowest SKEW (no one
        # hedging), 100 = highest (maximum tail anxiety). Rising SKEW over days/weeks
        # signals building institutional anxiety.

    # ── VIX/SKEW combination matrix ──
    vix_skew_state: VIXSKEWMatrix
        # The critical pairing:
        # - Low VIX + low SKEW: genuine complacency — no one is hedging anything
        # - Low VIX + high SKEW: "surface calm, deep anxiety" — institutional money is
        #   quietly buying disaster insurance while market looks fine. Historically
        #   precedes some of the sharpest selloffs because the market was structurally
        #   fragile despite appearing calm.
        # - High VIX + low SKEW: fear concentrated in ATM, not tails — market expects
        #   more of the same, not a regime change.
        # - High VIX + high SKEW: broad fear with tail hedging — market is scared and
        #   protecting against worst case.

    # ── SKEW trend ──
    skew_trend: SKEWTrend
        # Rising = institutional anxiety building (even if VIX is flat, tail hedging
        # is increasing). Stable = SKEW maintaining level. Declining = anxiety easing.
    skew_trend_days: int = 0
        # How many days the current trend has been in place. Longer = more confirmed.

    # ── Thresholds used for classification ──
    vix_threshold_high: float = 20.0
        # VIX level above which we classify as "high VIX" for the matrix.
    skew_threshold_high: float = 130.0
        # SKEW level above which we classify as "high SKEW" for the matrix.
        # Default 130; above this is elevated institutional hedging.


@dataclass(frozen=True)
class VolRegimeClassification:
    """Q11:11f — Composite volatility regime label with transition detection.

    Regime states: low-vol compression (Bollinger squeezes, mean-reversion),
    vol expansion (momentum favorable), crisis/spike (risk reduction),
    vol normalization (oversold bounce). Transition detection with confidence.

    This is the composite state assessment maintained continuously by the distillation
    layer. This output gets broadcast to ALL agents as universal context metadata
    because regime changes affect how every agent interprets data and makes decisions.

    Source: Derived from Q11:11a–11e
    Cadence: Every invocation
    Feasibility: HIGH — composite classification, pure distillation logic
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Regime classification ──
    regime_label: VolatilityRegime
        # The composite regime state broadcast to all agents:
        # - LOW_VOL_COMPRESSION: VIX low, term structure in steep contango, VVIX low,
        #   realized vol declining. Squeeze building, mean-reversion works well.
        #   Breakout risk is building. Individual name Bollinger squeezes are more
        #   likely to resolve into trending moves.
        # - VOL_EXPANSION: VIX rising, term structure flattening, realized vol increasing.
        #   Momentum and breakout strategies work, mean-reversion becomes dangerous.
        #   System favors thesis-driven directional trades over range-bound setups.
        # - CRISIS_SPIKE: VIX elevated, term structure in backwardation, VVIX high.
        #   Reduce position sizes, widen stops, favor defensive theses, bias to closing
        #   positions rather than opening new ones. Pre-close run should be aggressive
        #   about risk reduction.
        # - VOL_NORMALIZATION: VIX declining from elevated levels, term structure
        #   returning to contango. Opportunities in names that overreacted during spike.
        #   System increases position sizes cautiously as regime stabilizes.

    # ── Regime transition detection ──
    regime_changed: bool = False
        # True when the regime label changed since the previous invocation.
        # Critical for agents to know about regime shifts immediately.
    prior_regime: Optional[VolatilityRegime] = None
        # Previous regime label for context.
    regime_change_confidence: float = 0.5
        # 0.0–1.0. Early transitions (1–2 invocations into the new regime) are less
        # certain but more actionable. Confirmed transitions (5+ invocations) are
        # certain but already priced. Agents should weight both signal and confidence.
        # Typical early transition: 0.4–0.6. Confirmed: 0.85–1.0.

    # ── Regime duration ──
    regime_duration_invocations: int = 0
        # How many invocations (usually ~4 per trading day during regular session)
        # the current regime has persisted. Helps agents distinguish early signals
        # from confirmed moves. Higher = regime is more established.

    # ── Supporting context ──
    regime_drivers: list[str] = field(default_factory=list)
        # List of the dominant factors driving the current classification:
        # e.g., ["vix_spike_detected", "term_structure_backwardation", "vvix_elevated"]
        # Helps agents understand the "why" behind the regime call.
    regime_risk_score: float = 0.5
        # 0.0–1.0. Composite risk level of the current regime. 0.0 = lowest risk
        # (low-vol compression), 1.0 = highest risk (crisis spike). Used by agents
        # for position sizing and stop-width guidance.
    next_regime_probability: Optional[dict[str, float]] = None
        # Optional: forward-looking probabilities for the next regime. If included,
        # helps agents anticipate transitions. E.g., {"vol_expansion": 0.35,
        # "crisis_spike": 0.15, "continued_compression": 0.50}
