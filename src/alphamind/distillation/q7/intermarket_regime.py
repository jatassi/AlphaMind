"""Intermarket regime signals.

Implements every ``compute_intermarket_*`` / ``_classify_intermarket*``
function, including the four sub-blocks: SPY/TLT, GLD/real-yields,
oil/XLE beta, and VIX/SPY.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _calibration_for_window,
    _log_returns_from_closes,
    _pearson_correlation,
    _select_close_series,
    _window_bounds,
)
from alphamind.persistence.models import MacroObservations

# ---------------------------------------------------------------------------
# Series identifiers
# ---------------------------------------------------------------------------

SPY_TICKER = "SPY"
TLT_TICKER = "TLT"
GLD_TICKER = "GLD"
XLE_TICKER = "XLE"

REAL_YIELD_SOURCE = "FRED"
REAL_YIELD_SERIES = "DFII10"
"""10-year TIPS yield from FRED — the real-yield series quant 7d cites."""

VIX_SOURCE = "FRED"
VIX_SERIES = "VIXCLS"
"""VIX close from FRED — the volatility-index series intermarket monitors."""

OIL_SOURCE = "EIA"
OIL_SERIES = "DCOILWTICO"
"""WTI crude price series from EIA — used as the oil leg of oil-vs-XLE beta."""

# Beta-drift threshold in absolute units (story 08d): "flag when beta moves
# > 0.5 from 60-day mean". Encoded as a named constant so the rule is
# discoverable; not a tunable Class A threshold.
_OIL_XLE_BETA_DRIFT_THRESHOLD: float = 0.5

# Threshold (in correlation deviation units) above which the gold/real-yield
# correlation is considered to diverge from the textbook negative-correlation
# regime. The textbook expectation is ``rho ≈ -1``; once the realized
# correlation rises above ``-rho_drift_threshold`` (i.e., loses its negative
# sign) the divergence fires. Encoded as a named constant rather than a
# magic literal in the detection code.
_GOLD_REAL_YIELDS_DIVERGENCE_THRESHOLD: float = 0.0
"""Realized GLD vs. DFII10 correlation above this threshold fires divergence.

The textbook intermarket relationship is negatively correlated; a realized
correlation that has flipped non-negative is a divergence regardless of its
absolute magnitude.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _select_macro_series(
    session: Session,
    *,
    source: str,
    series_id: str,
    range_start_date: str,
    range_end_date: str,
) -> list[float]:
    """Return ascending non-null macro values in a date window."""
    stmt = (
        select(MacroObservations.value)
        .where(
            MacroObservations.source == source,
            MacroObservations.series_id == series_id,
            MacroObservations.observation_date >= range_start_date,
            MacroObservations.observation_date <= range_end_date,
            MacroObservations.value.isnot(None),
        )
        .order_by(MacroObservations.observation_date)
    )
    return [float(v) for v in session.execute(stmt).scalars().all() if v is not None]


def _rolling_beta(
    *,
    underlying_returns: Sequence[float],
    factor_returns: Sequence[float],
) -> float:
    """OLS slope of ``underlying`` regressed on ``factor`` (single-factor beta).

    Returns ``0.0`` when the factor has zero variance.
    """
    n = min(len(underlying_returns), len(factor_returns))
    if n == 0:
        return 0.0
    u = list(underlying_returns)[-n:]
    f = list(factor_returns)[-n:]
    mean_u = sum(u) / n
    mean_f = sum(f) / n
    cov = sum((ui - mean_u) * (fi - mean_f) for ui, fi in zip(u, f, strict=True))
    var = sum((fi - mean_f) * (fi - mean_f) for fi in f)
    if var == 0.0:
        return 0.0
    return float(cov / var)


def _spy_tlt_regime_block(
    session: Session,
    *,
    as_of: datetime,
    range_start: str,
    range_end: str,
    window_days: int,
) -> OutputBlock:
    """Stocks-vs-bonds regime: positive (inflation) vs. negative (growth)."""
    spy_closes = _select_close_series(
        session, ticker=SPY_TICKER, range_start=range_start, range_end=range_end
    )
    tlt_closes = _select_close_series(
        session, ticker=TLT_TICKER, range_start=range_start, range_end=range_end
    )
    spy_returns = _log_returns_from_closes(spy_closes)
    tlt_returns = _log_returns_from_closes(tlt_closes)
    correlation = _pearson_correlation(spy_returns, tlt_returns)
    regime_label = "inflation_environment" if correlation > 0.0 else "growth_environment"
    state, reason = _calibration_for_window(
        n_observations=min(len(spy_returns), len(tlt_returns)),
        required=window_days,
        input_name="spy_tlt_correlation_observations",
    )
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intermarket_regime.spy_tlt",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload={
            "correlation": correlation,
            "regime_label": regime_label,
            "window_days": window_days,
        },
        anomaly_flags=(),
        regime_context=None,
    )


def _gld_real_yields_block(
    session: Session,
    *,
    as_of: datetime,
    range_start: str,
    range_end: str,
    range_start_date: str,
    range_end_date: str,
    window_days: int,
) -> OutputBlock:
    """Gold vs. real-yields divergence detection."""
    gld_closes = _select_close_series(
        session, ticker=GLD_TICKER, range_start=range_start, range_end=range_end
    )
    real_yields = _select_macro_series(
        session,
        source=REAL_YIELD_SOURCE,
        series_id=REAL_YIELD_SERIES,
        range_start_date=range_start_date,
        range_end_date=range_end_date,
    )
    gld_returns = _log_returns_from_closes(gld_closes)
    # Real-yield deltas (level changes in basis points) drive the
    # correlation; convert to a per-day delta sequence aligned in length to
    # ``gld_returns``.
    yield_deltas: list[float] = [b - a for a, b in pairwise(real_yields)]
    n = min(len(gld_returns), len(yield_deltas))
    correlation = _pearson_correlation(gld_returns[-n:], yield_deltas[-n:])

    flags: list[AnomalyFlag] = []
    # Textbook regime is negative correlation; a realized correlation above
    # the threshold has flipped sign and counts as divergence.
    if correlation > _GOLD_REAL_YIELDS_DIVERGENCE_THRESHOLD:
        flags.append(
            AnomalyFlag(
                name="gold_real_yields_divergence",
                magnitude=abs(correlation),
                severity="investigate_if_persists",
            )
        )

    state, reason = _calibration_for_window(
        n_observations=n,
        required=window_days,
        input_name="gld_real_yields_observations",
    )
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intermarket_regime.gld_real_yields",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload={
            "correlation": correlation,
            "window_days": window_days,
        },
        anomaly_flags=tuple(flags),
        regime_context=None,
    )


def _oil_xle_beta_block(
    session: Session,
    *,
    as_of: datetime,
    range_start: str,
    range_end: str,
    range_start_date: str,
    range_end_date: str,
    window_days: int,
    short_window_days: int,
) -> OutputBlock:
    """Oil vs. XLE beta stability: fire when beta drifts >0.5 from baseline."""
    xle_closes = _select_close_series(
        session, ticker=XLE_TICKER, range_start=range_start, range_end=range_end
    )
    oil_values = _select_macro_series(
        session,
        source=OIL_SOURCE,
        series_id=OIL_SERIES,
        range_start_date=range_start_date,
        range_end_date=range_end_date,
    )
    xle_returns = _log_returns_from_closes(xle_closes)
    oil_returns = _log_returns_from_closes(oil_values)
    long_beta = _rolling_beta(underlying_returns=xle_returns, factor_returns=oil_returns)
    # Recent beta — the trailing ``short_window_days`` returns. The split
    # makes a regime flip detectable distinctly from the long baseline.
    short_n = min(short_window_days, len(xle_returns))
    short_beta = _rolling_beta(
        underlying_returns=xle_returns[-short_n:],
        factor_returns=oil_returns[-short_n:],
    )
    drift = abs(short_beta - long_beta)

    flags: list[AnomalyFlag] = []
    if drift > _OIL_XLE_BETA_DRIFT_THRESHOLD:
        flags.append(
            AnomalyFlag(
                name="oil_xle_beta_drift",
                magnitude=drift,
                severity="investigate_if_persists",
            )
        )

    state, reason = _calibration_for_window(
        n_observations=min(len(xle_returns), len(oil_returns)),
        required=window_days,
        input_name="oil_xle_beta_observations",
    )
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intermarket_regime.oil_xle_beta",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload={
            "long_beta": long_beta,
            "short_beta": short_beta,
            "beta_drift": drift,
            "window_days": window_days,
        },
        anomaly_flags=tuple(flags),
        regime_context=None,
    )


def _vix_spy_block(
    session: Session,
    *,
    as_of: datetime,
    range_start: str,
    range_end: str,
    range_start_date: str,
    range_end_date: str,
    window_days: int,
) -> OutputBlock:
    """VIX vs. SPY divergence: VIX rising on flat or rising market."""
    spy_closes = _select_close_series(
        session, ticker=SPY_TICKER, range_start=range_start, range_end=range_end
    )
    vix_values = _select_macro_series(
        session,
        source=VIX_SOURCE,
        series_id=VIX_SERIES,
        range_start_date=range_start_date,
        range_end_date=range_end_date,
    )
    spy_returns = _log_returns_from_closes(spy_closes)
    # VIX is a level series; compute day-over-day deltas as the "return".
    vix_deltas = [b - a for a, b in pairwise(vix_values)]
    n = min(len(spy_returns), len(vix_deltas))
    correlation = _pearson_correlation(spy_returns[-n:], vix_deltas[-n:])

    flags: list[AnomalyFlag] = []
    # Standard regime: VIX falls when SPY rises (negative correlation). When
    # the realized correlation flips non-negative, divergence fires.
    if correlation > 0.0:
        flags.append(
            AnomalyFlag(
                name="vix_spy_divergence",
                magnitude=abs(correlation),
                severity="investigate_if_persists",
            )
        )

    state, reason = _calibration_for_window(
        n_observations=n,
        required=window_days,
        input_name="vix_spy_observations",
    )
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intermarket_regime.vix_spy",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload={
            "correlation": correlation,
            "window_days": window_days,
        },
        anomaly_flags=tuple(flags),
        regime_context=None,
    )


# ---------------------------------------------------------------------------
# Public compute function
# ---------------------------------------------------------------------------


def compute_intermarket_regime(
    session: Session,
    *,
    as_of: datetime,
    window_days: int,
    short_window_days: int,
) -> list[OutputBlock]:
    """Compute the four intermarket regime blocks.

    Returns one :class:`OutputBlock` per relationship:

    - ``q7.intermarket_regime.spy_tlt``
    - ``q7.intermarket_regime.gld_real_yields``
    - ``q7.intermarket_regime.oil_xle_beta``
    - ``q7.intermarket_regime.vix_spy``

    Each block carries both
    :attr:`OutputAudience.CORRELATION_REGIME_BRIEF` and
    :attr:`OutputAudience.UNIVERSAL_BROADCAST`.
    """
    range_start, range_end = _window_bounds(as_of=as_of, window_days=window_days)
    range_start_date = (as_of - timedelta(days=window_days)).strftime("%Y-%m-%d")
    range_end_date = as_of.strftime("%Y-%m-%d")
    return [
        _gld_real_yields_block(
            session,
            as_of=as_of,
            range_start=range_start,
            range_end=range_end,
            range_start_date=range_start_date,
            range_end_date=range_end_date,
            window_days=window_days,
        ),
        _oil_xle_beta_block(
            session,
            as_of=as_of,
            range_start=range_start,
            range_end=range_end,
            range_start_date=range_start_date,
            range_end_date=range_end_date,
            window_days=window_days,
            short_window_days=short_window_days,
        ),
        _spy_tlt_regime_block(
            session,
            as_of=as_of,
            range_start=range_start,
            range_end=range_end,
            window_days=window_days,
        ),
        _vix_spy_block(
            session,
            as_of=as_of,
            range_start=range_start,
            range_end=range_end,
            range_start_date=range_start_date,
            range_end_date=range_end_date,
            window_days=window_days,
        ),
    ]
