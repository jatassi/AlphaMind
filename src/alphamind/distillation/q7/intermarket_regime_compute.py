"""Pure compute for Q7 intermarket regime signals (ALP-486).

Splits the intermarket-regime compute path along the compute/load
boundary. This module is ORM-free; the session-bound read surface lives
in :mod:`.intermarket_regime_loaders`.

Each helper returns one ``OutputBlock`` per relationship; the public
:func:`compute_intermarket_regime_pure` packages all four.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import pairwise

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
)

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
    *,
    spy_closes: Sequence[float],
    tlt_closes: Sequence[float],
    as_of: datetime,
    window_days: int,
) -> OutputBlock:
    """Stocks-vs-bonds regime: positive (inflation) vs. negative (growth)."""
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
    *,
    gld_closes: Sequence[float],
    real_yields: Sequence[float],
    as_of: datetime,
    window_days: int,
) -> OutputBlock:
    """Gold vs. real-yields divergence detection."""
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
    *,
    xle_closes: Sequence[float],
    oil_values: Sequence[float],
    as_of: datetime,
    window_days: int,
    short_window_days: int,
) -> OutputBlock:
    """Oil vs. XLE beta stability: fire when beta drifts >0.5 from baseline."""
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
    *,
    spy_closes: Sequence[float],
    vix_values: Sequence[float],
    as_of: datetime,
    window_days: int,
) -> OutputBlock:
    """VIX vs. SPY divergence: VIX rising on flat or rising market."""
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


def compute_intermarket_regime_pure(
    *,
    closes_by_ticker: Mapping[str, Sequence[float]],
    macros_by_series: Mapping[str, Sequence[float]],
    window_days: int,
    short_window_days: int,
    as_of: datetime,
) -> list[OutputBlock]:
    """Pure compute of the four intermarket regime blocks.

    Block ids:
    - ``q7.intermarket_regime.gld_real_yields``
    - ``q7.intermarket_regime.oil_xle_beta``
    - ``q7.intermarket_regime.spy_tlt``
    - ``q7.intermarket_regime.vix_spy``

    Each block carries both
    :attr:`OutputAudience.CORRELATION_REGIME_BRIEF` and
    :attr:`OutputAudience.UNIVERSAL_BROADCAST`.
    """
    spy_closes = closes_by_ticker.get(SPY_TICKER, ())
    tlt_closes = closes_by_ticker.get(TLT_TICKER, ())
    gld_closes = closes_by_ticker.get(GLD_TICKER, ())
    xle_closes = closes_by_ticker.get(XLE_TICKER, ())
    real_yields = macros_by_series.get(REAL_YIELD_SERIES, ())
    vix_values = macros_by_series.get(VIX_SERIES, ())
    oil_values = macros_by_series.get(OIL_SERIES, ())
    return [
        _gld_real_yields_block(
            gld_closes=gld_closes,
            real_yields=real_yields,
            as_of=as_of,
            window_days=window_days,
        ),
        _oil_xle_beta_block(
            xle_closes=xle_closes,
            oil_values=oil_values,
            as_of=as_of,
            window_days=window_days,
            short_window_days=short_window_days,
        ),
        _spy_tlt_regime_block(
            spy_closes=spy_closes,
            tlt_closes=tlt_closes,
            as_of=as_of,
            window_days=window_days,
        ),
        _vix_spy_block(
            spy_closes=spy_closes,
            vix_values=vix_values,
            as_of=as_of,
            window_days=window_days,
        ),
    ]


__all__ = [
    "GLD_TICKER",
    "OIL_SERIES",
    "OIL_SOURCE",
    "REAL_YIELD_SERIES",
    "REAL_YIELD_SOURCE",
    "SPY_TICKER",
    "TLT_TICKER",
    "VIX_SERIES",
    "VIX_SOURCE",
    "XLE_TICKER",
    "compute_intermarket_regime_pure",
]
