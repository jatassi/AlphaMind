"""Pure compute for Q7 intermarket regime signals (ALP-486).

Splits the intermarket-regime compute path along the compute/load
boundary. This module is ORM-free; the session-bound read surface lives
in :mod:`.intermarket_regime_loaders`.

Each helper returns one ``OutputBlock`` per relationship; the public
:func:`compute_intermarket_regime_pure` packages all four.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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

REAL_YIELD_SOURCE = "fred"
REAL_YIELD_SERIES = "DFII10"
"""10-year TIPS yield from FRED — the real-yield series quant 7d cites.

Source string matches the lowercase ``"fred"`` token written by the FRED
collector (``alphamind.data_sources.fred.macro._SOURCE``); the loader's
``source ==`` filter must match the producer's casing for the row to surface.
ALP-537 traced the e2e "0 observations" symptom on this series to a
constants-vs-collector case mismatch (uppercase ``"FRED"``).
"""

VIX_SOURCE = "fred"
VIX_SERIES = "VIXCLS"
"""VIX close from FRED — the volatility-index series intermarket monitors."""

OIL_SOURCE = "fred"
OIL_SERIES = "DCOILWTICO"
"""WTI crude price series from FRED — used as the oil leg of oil-vs-XLE beta.

DCOILWTICO ships in :mod:`alphamind.data_sources.fred.series.DAILY_SERIES`,
not the EIA adapter (whose WTI series is ``eia.wti_spot_price``); ALP-537
traced the e2e "0 observations" symptom on ``oil_xle_beta`` to a constants
mismatch (``OIL_SOURCE = "EIA"``) that filtered the FRED-written rows out.
"""

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
    n_observations = min(len(spy_returns), len(tlt_returns))
    state, reason = _calibration_for_window(
        n_observations=n_observations,
        required=window_days,
        input_name="spy_tlt_correlation_observations",
    )
    correlation: float | None
    regime_label: str | None
    if n_observations == 0:
        correlation = None
        regime_label = None
    else:
        correlation = _pearson_correlation(spy_returns, tlt_returns)
        regime_label = "inflation_environment" if correlation > 0.0 else "growth_environment"
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
    state, reason = _calibration_for_window(
        n_observations=n,
        required=window_days,
        input_name="gld_real_yields_observations",
    )
    correlation: float | None
    flags: list[AnomalyFlag] = []
    if n == 0:
        correlation = None
    else:
        correlation = _pearson_correlation(gld_returns[-n:], yield_deltas[-n:])
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
    n_observations = min(len(xle_returns), len(oil_returns))
    state, reason = _calibration_for_window(
        n_observations=n_observations,
        required=window_days,
        input_name="oil_xle_beta_observations",
    )
    long_beta: float | None
    short_beta: float | None
    drift: float | None
    flags: list[AnomalyFlag] = []
    if n_observations == 0:
        long_beta = None
        short_beta = None
        drift = None
    else:
        long_beta = _rolling_beta(underlying_returns=xle_returns, factor_returns=oil_returns)
        # Recent beta — the trailing ``short_window_days`` returns. The split
        # makes a regime flip detectable distinctly from the long baseline.
        short_n = min(short_window_days, len(xle_returns))
        short_beta = _rolling_beta(
            underlying_returns=xle_returns[-short_n:],
            factor_returns=oil_returns[-short_n:],
        )
        drift = abs(short_beta - long_beta)
        if drift > _OIL_XLE_BETA_DRIFT_THRESHOLD:
            flags.append(
                AnomalyFlag(
                    name="oil_xle_beta_drift",
                    magnitude=drift,
                    severity="investigate_if_persists",
                )
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
    state, reason = _calibration_for_window(
        n_observations=n,
        required=window_days,
        input_name="vix_spy_observations",
    )
    correlation: float | None
    flags: list[AnomalyFlag] = []
    if n == 0:
        correlation = None
    else:
        correlation = _pearson_correlation(spy_returns[-n:], vix_deltas[-n:])
        # Standard regime: VIX falls when SPY rises (negative correlation).
        # When the realized correlation flips non-negative, divergence fires.
        if correlation > 0.0:
            flags.append(
                AnomalyFlag(
                    name="vix_spy_divergence",
                    magnitude=abs(correlation),
                    severity="investigate_if_persists",
                )
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


@dataclass(frozen=True, slots=True)
class IntermarketRegimeInputs:
    """Pre-aligned per-block value sequences for the four intermarket pairs.

    Each tuple holds the two value series for one relationship, position-
    aligned on the date intersection performed by the IO shell (ALP-629).
    A single ticker like SPY appears in two pairs (``spy_tlt`` and
    ``vix_spy``) with different intersections because TLT (NYSE) and VIX
    (FRED) share different overlap sets with the SPY calendar. The SPY
    sequence in ``spy_tlt`` is NOT interchangeable with the one in
    ``vix_spy`` — collapsing them to a single shared series would
    re-introduce the ALP-629 bug on whichever pair lost its dedicated
    intersection.

    ``__post_init__`` enforces equal-length left/right pairs so a future
    direct caller that bypasses :func:`_inner_join_on_date` fails fast at
    construction instead of silently regressing to index-positional
    pairing inside the per-block compute helpers.
    """

    spy_tlt: tuple[Sequence[float], Sequence[float]]
    """``(spy_closes, tlt_closes)`` aligned on the SPY-TLT date intersection."""
    gld_real_yields: tuple[Sequence[float], Sequence[float]]
    """``(gld_closes, real_yield_values)`` aligned on the GLD-DFII10 intersection."""
    oil_xle_beta: tuple[Sequence[float], Sequence[float]]
    """``(xle_closes, oil_values)`` aligned on the XLE-DCOILWTICO intersection."""
    vix_spy: tuple[Sequence[float], Sequence[float]]
    """``(spy_closes, vix_values)`` aligned on the SPY-VIXCLS intersection.

    The ``spy_closes`` here is distinct from the one in :attr:`spy_tlt`;
    its dates intersect with VIX, not TLT.
    """

    def __post_init__(self) -> None:
        for field_name in ("spy_tlt", "gld_real_yields", "oil_xle_beta", "vix_spy"):
            left, right = getattr(self, field_name)
            if len(left) != len(right):
                raise ValueError(
                    f"IntermarketRegimeInputs.{field_name} legs differ in length "
                    f"({len(left)} vs {len(right)}); the IO shell must inner-join "
                    "the two series on common calendar dates before constructing "
                    "this dataclass (ALP-629)."
                )


def compute_intermarket_regime_pure(
    *,
    inputs: IntermarketRegimeInputs,
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

    The four per-block value pairs in ``inputs`` are pre-aligned on shared
    calendar dates by :mod:`alphamind.distillation.q7._loaders` — ALP-629
    moved alignment responsibility out of the pure compute into the IO
    shell so SPY can carry distinct intersections against TLT (NYSE) and
    VIX (FRED) at the same time.
    """
    gld_closes, real_yields = inputs.gld_real_yields
    xle_closes, oil_values = inputs.oil_xle_beta
    spy_closes_for_tlt, tlt_closes = inputs.spy_tlt
    spy_closes_for_vix, vix_values = inputs.vix_spy
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
            spy_closes=spy_closes_for_tlt,
            tlt_closes=tlt_closes,
            as_of=as_of,
            window_days=window_days,
        ),
        _vix_spy_block(
            spy_closes=spy_closes_for_vix,
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
    "IntermarketRegimeInputs",
    "compute_intermarket_regime_pure",
]
