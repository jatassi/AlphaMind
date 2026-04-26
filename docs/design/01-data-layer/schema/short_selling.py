"""Q4: Short Selling and Lending — entity definitions.

5 entities covering short interest snapshots, borrow cost dynamics, daily
short volume, lending market structure, and a composite squeeze score.
iBorrowDesk scraping provides the key borrow cost signal that was previously
a gap.

Design note on latency:
    This category has the worst latency profile of any core category. Short
    interest is bi-monthly (FINRA settlement cycle, ~10 day lag). Borrow cost
    is daily (iBorrowDesk/Fintel). Only daily short volume provides same-day
    signal. Every field carries explicit refresh rate metadata so the system
    knows what's fresh vs. estimated.

Design note on refresh rates:
    - ShortInterestSnapshot: bi-monthly (stale but official)
    - BorrowCost: every 15 minutes (iBorrowDesk scraping during trading hours)
    - ShortVolumeDaily: daily next business day (FINRA official)
    - LendingMarketStructure: on-demand only (not scheduled, too expensive)
    - SqueezeComposite: on-demand derived from 4a–4c (computation, not ingestion)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from ._common import (
    AnomalyFlag,
    DataConfidence,
    Direction,
    InvocationMetadata,
    SignalStrength,
    Ticker,
)


# ── Supporting types ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BorrowCostSnapshot:
    """A single point-in-time borrow cost measurement. Used as building blocks
    within BorrowCost to track fee dynamics."""
    timestamp: datetime                  # UTC timestamp of the measurement
    annualized_fee_pct: float           # Annualized borrow fee as a percentage (e.g., 12.5)
    daily_fee_pct: Optional[float] = None  # Daily equivalent (for convenience)
    available_shares: Optional[int] = None  # Shares available to borrow at this fee


@dataclass(frozen=True)
class ShortVolumeTrend:
    """Rolling trend statistics for short volume over multiple days. Captures
    sustained directional shorting independent of one-day noise."""
    days_in_period: int                 # Number of days in the rolling window (e.g., 5, 20)
    avg_short_volume: int               # Average daily short volume in this window
    avg_short_ratio: float              # Average short volume ratio in this window
    trend_direction: Direction          # BULLISH = avg declining, BEARISH = avg rising,
                                        # NEUTRAL = flat
    trend_magnitude: float = 0.0        # Standard deviations from longer-term mean


@dataclass(frozen=True)
class FeeTrendSnapshot:
    """Fee trajectory measurement: fee level and direction of movement."""
    period_start: date                  # Start date of measurement period
    period_end: date                    # End date of measurement period
    fee_start_pct: float                # Annualized borrow fee at period start
    fee_end_pct: float                  # Annualized borrow fee at period end
    fee_change_pct: float               # Absolute change in percentage points
    fee_change_pct_of_start: float      # Percentage change relative to starting fee
    trend_direction: Direction          # Is fee rising (BEARISH) or falling (BULLISH)?
    is_spike: bool = False              # True if change magnitude exceeds 1 std dev


# ── Primary entities ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ShortInterestSnapshot:
    """Q4:4a — Short interest as % of float, days-to-cover, utilization, and trend.

    The headline short pressure metrics: how much of the float is sold short,
    how long it would take to cover, how much of the lendable supply is in use,
    and whether short interest is accelerating or decelerating. The rate of
    change (SI trend) matters more than the absolute level — a stock going from
    15% to 25% SI over two cycles is more actionable than one sitting at 40% for
    months.

    Source: FINRA via free aggregators (official) + iBorrowDesk estimated shares
            available (partial proxy for utilization)
    Fallback: yfinance (incomplete SI data)
    Cadence: Bi-monthly (FINRA settlement cycle, ~10 day lag)
    Feasibility: HIGH (delayed) — free from FINRA, true utilization rate not
                 available free
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Short interest headline metrics ──
    short_interest_pct_float: float
        # Current short interest as a percentage of the stock's float. E.g., 15.0
        # means 15% of shares outstanding are sold short. This is the primary
        # indicator of short pressure. Values above 25% are considered elevated;
        # above 50% is extreme (though rare). Source: FINRA bi-monthly official.
    short_interest_abs_shares: int
        # Absolute count of shares currently sold short. Provides context on the
        # magnitude — 1M shares short of a 2M-share float is very different from
        # 1M shares short of a 1B-share float, even if both are 50% SI.

    # ── Days to cover (SI ratio) ──
    days_to_cover: float
        # Short interest ratio: (short shares) / (average daily volume).
        # How many days it would take all short sellers to unwind at average
        # daily volume, assuming unlimited supply and no supporting demand.
        # Values > 5 days are elevated; > 10 days signal tight supply.
        # This is a primary squeeze trigger metric for the distillation layer.

    # ── Utilization rate ──
    utilization_rate_pct: Optional[float] = None
        # Percentage of lendable shares currently out on loan. 90%+ utilization
        # is a squeeze setup signal — new shorts must bid up borrow costs
        # (fee pressure) or wait for recalls. Source: securities lending platforms
        # (not free; iBorrowDesk 'available shares' is a partial proxy).
        # Will be None if unavailable from data sources.

    # ── SI trend and acceleration ──
    si_change_cycle_pct: Optional[float] = None
        # Cycle-over-cycle change in SI: (current SI - prior SI) / prior SI × 100.
        # Negative = shorts covering, positive = more shorting. Large positive
        # changes (>20% per cycle) flag acceleration. Refresh: bi-monthly.
    si_trend_direction: Direction = Direction.NEUTRAL
        # Classification of SI trend: BEARISH (SI rising, shorts accumulating),
        # BULLISH (SI falling, shorts covering), NEUTRAL (stable ±5% range).
    si_acceleration: SignalStrength = SignalStrength.NONE
        # How pronounced the SI trend is. STRONG = accelerating (2+ consecutive
        # cycles of >20% change). MODERATE = steady growth (10–20% per cycle).
        # WEAK = slow change (<10%). NONE = stable. Helps distinguish short
        # panic (STRONG) from gradual shorts drifting in.

    # ── Freshness metadata ──
    si_data_as_of: Optional[date] = None
        # Settlement date of the most recent FINRA SI count. FINRA publishes
        # bi-monthly on specific dates with ~5–10 day delay. This tells you
        # exactly how stale the data is.
    utilization_data_as_of: Optional[date] = None
        # Date of the most recent utilization measurement (if available).

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
        # Detected anomalies: SI jumps, utilization approaching 100%, or extreme
        # days-to-cover relative to this name's history.


@dataclass(frozen=True)
class BorrowCost:
    """Q4:4b — Borrow fee rate, fee trends, spikes, and hard-to-borrow status.

    The price of shorting. When borrow fees spike, it signals demand to short
    exceeds available lendable supply. Elevated fees indicate informed bearish
    conviction (sophisticated shorts willing to pay up). Fee term structure
    (front-loaded vs. flat) reveals whether bears are positioning for a short-term
    event (expensive near-term, cheaper later) or a structural thesis (flat across
    tenors). Hard-to-borrow status is the extreme end — names that can't be borrowed
    at any price.

    Source: iBorrowDesk (free, scraping IBKR broker data) for rates + available shares;
            Fintel.io (paid API, future fallback)
    Fallback: Limited HTB binary flag from IBKR, no fee data
    Cadence: Every 15 minutes during trading hours (iBorrowDesk scrape)
    Feasibility: HIGH — iBorrowDesk covers the 65-ticker universe reliably,
                 FINRA SLATE (official borrow data) delayed to March 2029
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Current borrow fee ──
    current_fee_annualized_pct: float
        # Annualized borrow cost as a percentage. E.g., 15.0 = 15% annualized.
        # This is the headline number — the rate shorts are paying right now to
        # borrow this stock. Fees > 100% annualized are extreme (typically indicates
        # severe supply constraint). Source: iBorrowDesk, updated every 15 min.
    current_fee_daily_pct: Optional[float] = None
        # Daily equivalent for convenience. annualized / 252 (trading days).

    # ── Available share count ──
    available_shares_to_borrow: Optional[int] = None
        # Number of shares currently available for shorting from the lending pool.
        # Low values (< 100K) suggest tight supply. When combined with elevated
        # fees, signals short panic. Source: iBorrowDesk estimated from IBKR data.

    # ── Fee trend (short-term momentum) ──
    fee_trend_1d: Optional[FeeTrendSnapshot] = None
        # Fee movement over the past day (most recent 1-day rolling window).
        # Spikes in overnight or pre-market hours are high-signal. Detects panic.
    fee_trend_5d: Optional[FeeTrendSnapshot] = None
        # 5-day rolling fee trend. Smooths 1-day noise; captures sustained pressure.
    fee_trend_20d: Optional[FeeTrendSnapshot] = None
        # 20-day rolling fee trend. Shows structural short cost changes.

    # ── Fee spikes ──
    fee_spike_active: bool = False
        # True if current fee is >1.5× the 20-day average and the change happened
        # within the past 24 hours. High signal of short panic or supply shock.
    fee_spike_magnitude: Optional[float] = None
        # How extreme the spike is, measured in standard deviations from the
        # 20-day baseline. E.g., 3.5 = 3.5 std dev above mean (exceptional).
    fee_spike_start_time: Optional[datetime] = None
        # UTC timestamp when the current spike began.

    # ── Hard-to-borrow status ──
    hard_to_borrow: bool = False
        # True if the stock appears on prime broker hard-to-borrow lists or
        # borrow availability is critically low (<50K shares or unavailable
        # at any fee). Binary flag of extreme short supply. Source: IBKR,
        # Fintel, or FINRA SHO Reg threshold list (checked on-demand).

    # ── Fee term structure (future expansion) ──
    fee_term_structure: Optional[str] = None
        # Qualitative description of the fee curve: "front_loaded" = near-term
        # expensive, longer-term cheaper (event-driven short thesis). "flat" =
        # fee is similar across all tenors (structural bear thesis). "inverted" =
        # near-term cheaper, longer-term expensive (rare, indicates supply
        # recovery expected). Requires term data from Fintel or similar;
        # will be None if unavailable.

    # ── Recent snapshots for history ──
    recent_measurements: list[BorrowCostSnapshot] = field(default_factory=list)
        # Last 10–20 fee measurements (timestamps + fees + available shares).
        # Enables downstream systems to compute their own trends without needing
        # full historical data. Most recent first.

    # ── Freshness metadata ──
    last_updated: datetime = field(default_factory=datetime.utcnow)
        # UTC timestamp of the most recent borrow cost measurement. iBorrowDesk
        # updates every 15 min during trading hours; may be stale outside hours.

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
        # Detected anomalies: fee spikes, hard-to-borrow transitions, available
        # shares dropping sharply, or fee reversals.


@dataclass(frozen=True)
class ShortVolumeDaily:
    """Q4:4c — Daily FINRA short volume and short volume ratio.

    Daily short sale activity, distinct from accumulated short interest.
    FINRA publishes daily per ticker, making this the most timely signal in
    the short-selling category. Not all short volume is bearish — market makers
    short as part of hedging and liquidity provision. The distillation layer
    flags directional short volume (sustained, concentrated spikes) vs. mechanical
    (routine market-making). Contextualization requires comparison to order flow
    data (category 2) and tape reading.

    Source: FINRA (free, official)
    Fallback: Quandl/Nasdaq Data Link (free tier, FINRA data)
    Cadence: Daily (published next business day, ~24 hour lag)
    Feasibility: HIGH — FINRA publishes daily, free and reliable
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Daily short volume (most recent day) ──
    report_date: date
        # The trading date for which this short volume is reported. FINRA
        # publishes the prior day's data mid-morning ET.
    daily_short_volume_shares: int
        # Total short sale volume on the report date (shares). The raw count of
        # shares sold short by all market participants. Source: FINRA.
    daily_total_volume_shares: int
        # Total share volume on the same date. Used to compute the ratio.

    # ── Short volume ratio ──
    short_volume_ratio_pct: float
        # Short volume as a percentage of total daily volume. E.g., 65.0 = 65%
        # of the day's volume was short sales. Above 70% is elevated (concentrated
        # shorting). Below 40% is light shorting. Context: use with tape reading
        # to distinguish market-making shorts (routine) from directional shorts
        # (panic, capitulation, or informed bears). Source: FINRA.

    # ── Short volume trend (multi-day rolling) ──
    trend_5d: Optional[ShortVolumeTrend] = None
        # 5-day rolling average short volume and ratio. Smooths single-day noise.
        # Identifies sustained shorting direction independent of one-day spikes.
    trend_20d: Optional[ShortVolumeTrend] = None
        # 20-day rolling trend. Captures structural changes (e.g., shorts ramping
        # position over weeks vs. a single panic spike).

    # ── Contextualization: directional vs. mechanical ──
    likely_directional: Optional[bool] = None
        # True if short volume appears directional (bears building/unwinding
        # positions) rather than mechanical (routine market-making hedging).
        # Heuristics: (1) short ratio > 65% on down day = directional selling.
        # (2) short ratio spike on quiet volume day = unusual shorting interest.
        # (3) sustained >60% for multiple days = accumulating position.
        # (4) declining short ratio on up days = shorts covering. Requires
        # cross-referencing with order flow data (Q2) and price action (Q1).
        # Will be None if insufficient context for determination.
    directional_signal_strength: SignalStrength = SignalStrength.WEAK
        # How confident we are that the short volume is directional vs. routine.
        # STRONG = consistent multi-day pattern + price confirmation. MODERATE =
        # spike on high volume or down day. WEAK = single day or ambiguous context.

    # ── Prior day context ──
    prior_day_ratio_pct: Optional[float] = None
        # Yesterday's short volume ratio (if available). Enables trend detection
        # within a day (ratio increased from 45% to 65% = fresh shorting activity).

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
        # Detected anomalies: extreme short volume ratio (>80%), sustained high
        # ratio over multiple days, or sudden shifts in shorting intensity.


@dataclass(frozen=True)
class LendingMarketStructure:
    """Q4:4d — Securities lending supply, recall risk, and lender concentration.

    The supply side of short borrowing: how many shares are available to lend,
    whether the pool is shrinking (large holders recalling), lender concentration
    (fragile if concentrated), and net lending flows. Detects "the squeeze is
    coming" before the price move: supply contracting while demand holds steady
    forces covering regardless of the short's conviction.

    Data is daily from securities lending platforms (IHS Markit, S3 Partners,
    etc.) — too slow and too expensive to justify routine ingestion. This entity
    exists as ON-DEMAND RESEARCH INFRASTRUCTURE for the adaptive research layer
    when investigating a specific squeeze thesis. Build the API connection;
    don't schedule it on a timetable. Most fields are Optional because they'll
    only be populated when explicitly requested.

    Source: S3 Partners, IHS Markit, Reg SHO threshold list (SEC, on-demand)
    Fallback: iBorrowDesk estimated 'available shares' (partial proxy only)
    Cadence: ON-DEMAND only (too expensive for routine ingestion)
    Feasibility: VERY LOW — true institutional-grade data requires paid APIs;
                 iBorrowDesk available shares is an incomplete proxy
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Lendable supply snapshot (on-demand) ──
    lendable_supply_shares: Optional[int] = None
        # Total shares available for lending in the securities lending market.
        # This is NOT the shares short — it's the *available* pool. Combined
        # with short interest, gives you the cushion before covering is forced
        # (lendable_supply - short_interest_shares). Tight supply is a squeeze
        # precondition. Source: S3 Partners or IHS Markit (paid).

    # ── Supply trend ──
    supply_trend_direction: Optional[Direction] = None
        # Is the lendable supply growing (BULLISH, shorts have room) or shrinking
        # (BEARISH, squeeze pressure building)? Measured over 5–20 day window.
    supply_change_pct_20d: Optional[float] = None
        # 20-day percentage change in lendable supply. E.g., -30.0 = supply
        # contracted 30% (large holders recalling shares).

    # ── Lender concentration ──
    lender_concentration_herfindahl: Optional[float] = None
        # Herfindahl concentration index (0.0–1.0) of lenders supplying the
        # lendable pool. Values > 0.6 indicate concentrated lending (fragile —
        # one or two major lenders control supply). Values < 0.3 indicate
        # distributed lending (robust to recalls). Higher concentration = higher
        # squeeze risk if a major lender recalls. Source: S3 Partners.

    # ── Net lending flow (new borrows vs. returns) ──
    net_new_loans_shares_1d: Optional[int] = None
        # Net new share borrowing over the past day (new loans initiated minus
        # returns). Positive = shorts ramping. Negative = shorts covering.
        # Magnitude indicates pace of position changes.
    net_new_loans_trend_5d: Optional[Direction] = None
        # Direction of net borrowing over 5 days. BEARISH = consistent new
        # borrowing (shorts accumulating). BULLISH = net returns (shorts covering).

    # ── Reg SHO threshold list status ──
    reg_sho_threshold_listed: bool = False
        # True if the ticker appears on the SEC Reg SHO threshold list (persistent
        # fails-to-deliver requiring mandatory close-out). Binary flag of extreme
        # squeeze catalyst potential. Checkable on-demand from SEC; FTD data
        # underlying this is ~2 week delayed, but threshold list membership is
        # useful for squeeze investigations. Source: SEC Reg SHO threshold list.
    days_on_threshold_list: Optional[int] = None
        # How many consecutive days the name has been on the threshold list.
        # >5 days = persistent problem, mandatory closes imminent.

    # ── Freshness metadata ──
    data_as_of: Optional[date] = None
        # Date of the most recent lending market data. Securities lending data
        # is slower to publish than borrow costs or short volume.

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
        # Detected anomalies: supply collapse, lender concentration spike,
        # threshold list entry, or net lending direction reversal.


@dataclass(frozen=True)
class SqueezeComposite:
    """Q4:4e — Composite short squeeze scoring from SI + borrow + short volume.

    Not a raw data source but a derived state assessment synthesized from
    4a–4d. Given current short interest, utilization, borrow cost, daily short
    volume, and price/flow context, where does this name sit on the squeeze
    spectrum? Feeds directly into the analyst — squeeze setups are
    high-conviction, time-bound asymmetric trades, exactly the system's target
    profile.

    This is a computation the adaptive research layer runs when pursuing a
    squeeze thesis, not a metric maintained on a schedule. Reliability depends
    on the freshness of underlying data: 4a–4c provide near-real-time-ish inputs
    (4c is daily, 4b is 15 min), 4d provides deeper context on-demand. The
    analyst uses this to size thesis confidence and coverage velocity
    expectations.

    Source: Derived from Q4:4a (ShortInterestSnapshot), Q4:4b (BorrowCost),
            Q4:4c (ShortVolumeDaily), plus Q1:1a (price) and Q2:2b (depth)
    Fallback: Derived from 4a + 4c only (without borrow costs)
    Cadence: ON-DEMAND (computed when requested, not scheduled)
    Feasibility: HIGH — computation is straightforward with iBorrowDesk borrow
                 costs available; reliability limited only by underlying data
                 staleness (SI is bi-monthly)
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Squeeze score (continuum) ──
    squeeze_score: float
        # Composite metric on a 0.0–100.0 scale capturing squeeze intensity.
        # 0–20: NORMAL — routine short activity, no pressure.
        # 20–50: ELEVATED — short pressure building, squeeze conditions developing.
        # 50–80: ACTIVE — tight supply and high short interest, covering risk imminent.
        # 80–100: EXTREME — all metrics flashing red, historical squeeze precedent.
        # Computed from: SI%, utilization, fee level, fee trend, days-to-cover,
        # short volume trend, supply trend. Weighted toward elevated SI (>25%)
        # + high fee (>50% annualized) + rising trend as the primary signals.
    squeeze_state: str = ""
        # Classification label for LLM consumption: "normal" | "elevated" |
        # "active" | "extreme". Maps from squeeze_score ranges.

    # ── Trigger proximity ──
    catalyst_proximity_score: float = 0.0
        # 0.0–100.0 score of how close the name is to conditions that historically
        # precipitate short covering cascades. Factors: (1) utilization near 100%,
        # (2) fee spiking or sustained >75% annualized, (3) borrow availability
        # <100K shares, (4) short volume ratio sustained >65%, (5) Reg SHO
        # threshold status. Proximity score > 70 often precedes covering rushes.
    days_to_catalyst_estimate: Optional[int] = None
        # If a squeeze is building, rough estimate of days until trigger (catalyst).
        # Heuristic: if supply is contracting 5% per day and shorts are stable,
        # the crossing point is calculable. Highly uncertain; for thesis sizing
        # only. None if not enough data or squeeze is already active.

    # ── Covering velocity estimate ──
    covering_velocity_atr_per_day: Optional[float] = None
        # If covering begins, estimated daily price move (in ATR multiples) needed
        # to unwind the accumulated short position at current average daily volume
        # and depth (from Q1:1a and Q2:2b). E.g., 2.5 = covering would move the
        # name ~2.5 ATR per day, reaching full unwind in (days_to_cover / covering_pace)
        # days. Helps analyst size the expected volatility and thesis duration.
        # Requires depth data (Q2:2b) to compute. Will be None if unavailable.

    # ── Catalyst sensitivity ──
    price_move_for_active_status_pct: Optional[float] = None
        # Rough estimate: a move of X% would push the name from current state
        # (e.g., "elevated") into "active" squeeze. Helps analyst understand how
        # tight the powder keg is. E.g., if the name is at "elevated" and a 5%
        # move would trigger panic covering, this field = 5.0. Heuristic based on
        # recent price volatility and fee response sensitivity.

    # ── Supporting metrics ──
    short_interest_pct_float: Optional[float] = None
        # Latest SI% (from 4a) for context. Included for reference without
        # requiring the analyst to fetch ShortInterestSnapshot separately.
    borrow_fee_annualized_pct: Optional[float] = None
        # Latest borrow fee (from 4b) for context.
    short_volume_ratio_pct: Optional[float] = None
        # Most recent short volume ratio (from 4c) for context.

    # ── Freshness warning ──
    data_freshness_warning: Optional[str] = None
        # String flagging stale data. E.g., "SI is bi-monthly, last update 8 days
        # ago" or "borrow cost is 15 minutes old, very fresh". Helps analyst
        # weight the composite score appropriately.

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
        # Detected anomalies feeding into the squeeze score: SI spikes, fee
        # explosions, covering cascades detected via short volume collapse,
        # threshold list entries, or supply drops.
