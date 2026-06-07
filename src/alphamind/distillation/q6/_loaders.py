"""Q6 IO shell (ALP-485): pre-load every input the q6 compute path consumes.

The compute path is :func:`assemble_q6_blocks_from_inputs` — a pure
function over :class:`Q6Inputs` (frozen). This module is the only place
Q6 reaches the database for the per-category indicator compute dispatch.

Responsibilities:

- Read FRED macro series (yield-curve DGS*, T10YIE breakevens, DTWEXBGS
  dollar index, funding-stress and market-liquidity component proxies).
- Run the pure-compute classifiers from
  :mod:`alphamind.distillation.q6.{yield_curve,inflation,dollar_attribution,macro_surprise}_compute`
  on the loaded data.
- Perform the funding-stress and market-liquidity composite-refresh
  *writes* (which call ``session.flush()``) synchronously under the
  shared session before the parallel core fires. This is the load-bearing
  reason q6 could not previously parallelize: lifting the flushes here
  decouples q6 from q1 / q3 / qualitative compute that no longer
  serialize on the Session.
- Read ``event_calendar`` + ``macro_observations`` for macro surprise
  anomaly detection.

After this loader returns, :func:`assemble_q6_blocks_from_inputs` consumes
:class:`Q6Inputs` without further DB access — the parallel pure compute
sees already-computed funding-stress / market-liquidity results.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation.baselines import _refresh_transaction, refresh_composite_state
from alphamind.distillation.output import AnomalyFlag
from alphamind.distillation.q6.dollar_attribution_compute import (
    DollarAttributionResult,
    classify_dollar_attribution,
)
from alphamind.distillation.q6.funding_stress_compute import (
    FUNDING_STRESS_COMPONENT_NAMES,
    FUNDING_STRESS_COMPOSITE_KIND,
    FundingStressResult,
    compute_funding_stress_alert,
)
from alphamind.distillation.q6.inflation_compute import (
    DEFLATION_RISK_BREAKEVEN_PP_CUTOFF,
    InflationRegimeResult,
    classify_inflation_regime,
)
from alphamind.distillation.q6.macro_surprise_compute import detect_macro_surprise_anomaly
from alphamind.distillation.q6.market_liquidity_compute import (
    MARKET_LIQUIDITY_COMPOSITE_KIND,
    MarketLiquidityResult,
    normalize_market_liquidity_components,
)
from alphamind.distillation.q6.yield_curve_compute import (
    YieldCurveRegimeResult,
    classify_yield_curve_regime,
)
from alphamind.persistence.models import (
    DistillationCompositeState,
    EventCalendar,
    MacroObservations,
)

# ---------------------------------------------------------------------------
# Trailing-window definitional constants
# ---------------------------------------------------------------------------
#
# These are anchors of the classification spec per ``external.md`` § 2,
# not Class A tunables. The dollar-attribution window coincides with
# ``correlation_short_days`` and routes through config; the three below
# are spec-anchored.

_YIELD_CURVE_TRANSITION_LOOKBACK_DAYS: int = 5
"""Calendar lookback for the 5-day 2s10s change per external.md § quant 6a."""

_INFLATION_BREAKEVEN_TREND_LOOKBACK_DAYS: int = 90
"""Calendar lookback for the 3-month breakeven trend per external.md § quant 6c."""

_INFLATION_DEFLATION_WINDOW_DAYS: int = 30
"""Trailing window for the breakeven < 1.5% sustained check per external.md § quant 6c."""

# FRED series IDs the loader reads. Centralized so the scaffolding for
# "is the series present?" cold-start probes shares a single source of
# truth with the read helpers.

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
# emitting a value (zero or real) on every invocation. Raised eagerly
# (not asserted) so the check survives ``python -O``.
if set(_FUNDING_STRESS_PROXY_SOURCES) != set(FUNDING_STRESS_COMPONENT_NAMES):
    raise RuntimeError(
        "funding-stress proxy-source vocabulary drifted from the canonical "
        "component-name set: "
        f"proxies={sorted(_FUNDING_STRESS_PROXY_SOURCES)}, "
        f"canonical={sorted(FUNDING_STRESS_COMPONENT_NAMES)}"
    )

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

# Macro-release events whose surprise histories the loader scans. Each
# entry maps an ``event_calendar.event_type`` to the FRED series whose
# observation provides the actual value released on that date.
_MACRO_SURPRISE_EVENTS: tuple[tuple[str, str], ...] = (
    ("cpi_release", "CPIAUCSL"),
    ("pce_release", "PCEPI"),
    ("nfp_release", "PAYEMS"),
)


# ---------------------------------------------------------------------------
# Frozen inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Q6Inputs:
    """Frozen pre-loaded inputs for the q6 compute path.

    Carries everything :func:`assemble_q6_blocks_from_inputs` needs:

    - ``as_of`` — the freshness timestamp passed through to every block.
    - ``yield_curve`` / ``inflation`` / ``dollar_attribution`` — the
      per-classifier pure-compute results, or ``None`` when the loader
      could not assemble required FRED inputs (the assembler then emits a
      BOOTSTRAP stub block in their place).
    - ``funding_stress`` / ``market_liquidity`` — the composite-refresh
      results. The corresponding persistence-row writes have already
      been performed under the shared session inside :func:`load_q6_inputs`,
      so the parallel pure compute consumes them as plain values.
    - ``macro_surprise_anomalies`` — per-indicator firing anomalies in the
      order the assembler emits them as blocks.
    """

    as_of: datetime
    yield_curve: YieldCurveRegimeResult | None
    inflation: InflationRegimeResult | None
    dollar_attribution: DollarAttributionResult | None
    funding_stress: FundingStressResult
    market_liquidity: MarketLiquidityResult
    macro_surprise_anomalies: tuple[tuple[str, AnomalyFlag], ...]


# ---------------------------------------------------------------------------
# Shared session-bound helpers
# ---------------------------------------------------------------------------


def _format_iso_utc(dt: datetime) -> str:
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


def _read_proxy_component_history(
    session: Session,
    *,
    as_of: datetime,
    proxy_sources: Mapping[str, str],
    lookback_days: int,
) -> Mapping[str, list[float]]:
    """Read trailing FRED values per component for percentile normalization.

    For each named component the loader pulls the ``lookback_days``-day
    window of FRED observations dated strictly before the ``as_of``
    calendar day. Excluding observations dated ``as_of`` itself keeps the
    rank well-defined when the latest reading is published intraday or
    end-of-day; when the vendor series is stale (weekend, holiday, weekly
    cadence) the most recent reading lives at an earlier ``observation_date``
    and will still appear in the trailing window — the resulting self-rank
    bias is order ``1/N`` on a window of ~8-60 observations.
    """
    range_end_date = (as_of - timedelta(days=1)).strftime("%Y-%m-%d")
    range_start_date = (as_of - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return {
        component_name: _select_macro_series_window(
            session,
            series_id=series_id,
            range_start_date=range_start_date,
            range_end_date=range_end_date,
        )
        for component_name, series_id in proxy_sources.items()
    }


# ---------------------------------------------------------------------------
# Per-classifier session-bound loaders (DB read + pure-compute call)
# ---------------------------------------------------------------------------


def _try_yield_curve_result(
    session: Session,
    *,
    as_of: datetime,
) -> YieldCurveRegimeResult | None:
    """Build a yield-curve result from FRED, or ``None`` when essentials are missing.

    Essentials are the five DGS series at ``as_of`` and the DGS2/DGS10 readings
    five calendar days earlier. When any essential is missing the loader
    surfaces the gap via an ``unavailable`` stub block rather than fabricating
    a label.
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
        v < DEFLATION_RISK_BREAKEVEN_PP_CUTOFF for v in window_values
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
    ``trade_flow_driven`` label per :func:`pearson_correlation`'s zero-
    variance branch. Empty DXY history, however, leaves no signal at all
    so the loader surfaces the gap via an ``unavailable`` stub block.
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


# ---------------------------------------------------------------------------
# Composite-refresh writers (load-bearing flushes the per-category indicator
# compute step was waiting on)
# ---------------------------------------------------------------------------


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
    component_percentiles, components_above, alert_active = compute_funding_stress_alert(
        components=components,
        prior_history=prior_history,
        component_alert_count=component_alert_count,
        component_alert_percentile=component_alert_percentile,
    )

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
    # Wrapped in the framework's fail-closed transaction wrapper so a partial
    # write rolls back rather than persisting.
    with _refresh_transaction(session):
        row = session.execute(
            select(DistillationCompositeState).where(
                DistillationCompositeState.composite_kind == FUNDING_STRESS_COMPOSITE_KIND,
                DistillationCompositeState.as_of == as_of,
            )
        ).scalar_one()
        row.alert_active = int(alert_active)

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


def refresh_market_liquidity_composite(
    session: Session,
    *,
    components: Mapping[str, float],
    component_history: Mapping[str, Sequence[float]],
    as_of: str,
    min_observations: int,
    alert_percentile: float,
) -> MarketLiquidityResult:
    """Refresh the market-wide liquidity composite with per-component normalization.

    Each raw FRED component is converted to its percentile rank against its
    own trailing series (``component_history``) before being summed into
    the composite. Without normalization the volatility_score (VIX, order
    ~10-50) dominates by magnitude over credit_spread_score (~0-10) and
    stress_index_score (~-2..+2), so the composite degenerates to "VIX with
    extra steps" (ALP-575).

    Components that lack usable history (empty or zero-variance series)
    fall back to a neutral midpoint score; the resulting
    ``composite_method`` tag tells downstream consumers whether they're
    looking at a fully-calibrated multi-component blend.

    Alert direction is ``"lower"``: a market_liquidity composite in the
    bottom ``alert_percentile`` (default 10th) means liquidity has thinned
    relative to the trailing distribution — a stress signal. Because
    ``composite_value`` is the sum of normalized 0..100 scores rather than
    raw FRED magnitudes, the persisted history under
    ``DistillationCompositeState`` is on the normalized scale only —
    pre-ALP-575 raw-sum rows are deleted by the matching Alembic migration.
    """
    normalization = normalize_market_liquidity_components(components, component_history)
    persistence_result = refresh_composite_state(
        session,
        composite_kind=MARKET_LIQUIDITY_COMPOSITE_KIND,
        components=normalization.normalized,
        as_of=as_of,
        min_observations=min_observations,
        alert_percentile=alert_percentile,
        alert_direction="lower",
    )
    raw_percentile = persistence_result.value["percentile_60d"]
    return MarketLiquidityResult(
        composite_value=float(persistence_result.value["composite_value"]),
        components=dict(components),
        normalized_components=dict(normalization.normalized),
        composite_method=normalization.composite_method,
        percentile_60d=None if raw_percentile is None else float(raw_percentile),
        alert_active=bool(persistence_result.value["alert_active"]),
        state=persistence_result.state,
        bootstrap_reason=persistence_result.bootstrap_reason,
    )


# ---------------------------------------------------------------------------
# Macro-release surprise scanner
# ---------------------------------------------------------------------------


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
    end_iso = _format_iso_utc(as_of)
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


# ---------------------------------------------------------------------------
# Top-level loader
# ---------------------------------------------------------------------------


def load_q6_inputs(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
) -> Q6Inputs:
    """Pre-load every q6 input the pure compute consumes.

    Performs all session-bound work:

    1. FRED reads + per-classifier pure-compute for yield-curve, inflation,
       and dollar attribution (each may return ``None`` if essentials are
       missing — the assembler emits an ``unavailable`` stub in their place).
    2. Funding-stress composite refresh, including the
       :func:`refresh_composite_state` write and the per-component
       ``alert_active`` override.
    3. Market-liquidity composite refresh.
    4. Macro-release surprise scan over the configured event types.

    After this returns, :func:`assemble_q6_blocks_from_inputs` runs in a
    thread without further DB access. The composite-refresh flushes have
    all executed under the shared session before the parallel core fires —
    no shared-mutable-Session conflict with q1 / q3 / qualitative.
    """
    as_of_iso = _format_iso_utc(as_of)

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
    liquidity_component_history = _read_proxy_component_history(
        session,
        as_of=as_of,
        proxy_sources=_MARKET_LIQUIDITY_PROXY_SOURCES,
        lookback_days=config.persistence_windows.market_liquidity_baseline_days,
    )
    ml_result = refresh_market_liquidity_composite(
        session,
        components=liquidity_components,
        component_history=liquidity_component_history,
        as_of=as_of_iso,
        min_observations=config.persistence_windows.market_liquidity_baseline_days,
        alert_percentile=float(config.anomaly_detection.market_liquidity_alert_percentile),
    )

    surprises = _read_macro_surprise_anomalies(
        session,
        as_of=as_of,
        alert_percentile=float(config.anomaly_detection.macro_surprise_percentile),
    )

    return Q6Inputs(
        as_of=as_of,
        yield_curve=yc_result,
        inflation=inflation_result,
        dollar_attribution=dollar_result,
        funding_stress=fs_result,
        market_liquidity=ml_result,
        macro_surprise_anomalies=tuple(surprises),
    )


__all__ = [
    "Q6Inputs",
    "load_q6_inputs",
    "refresh_funding_stress_composite",
    "refresh_market_liquidity_composite",
]
