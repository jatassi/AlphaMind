"""Compute-alias re-export for q1 technical indicators (ALP-467).

See :mod:`.anomalies_compute` for the convention; the indicators module
is the largest pure compute core in q1 (636 LOC), unchanged in this
story but pinned by the import-linter via the ``*_compute.py`` alias.
"""

from __future__ import annotations

from alphamind.distillation.q1.indicators import (
    AdxResult,
    AtrRegimeResult,
    BollingerResult,
    EmaCrossoverState,
    EmaPairResult,
    KeltnerResult,
    MacdResult,
    RsiResult,
    StochasticResult,
    classify_atr_regime,
    compute_adx,
    compute_bollinger,
    compute_ema_pairs,
    compute_keltner,
    compute_macd,
    compute_rsi,
    compute_stochastic,
)

__all__ = [
    "AdxResult",
    "AtrRegimeResult",
    "BollingerResult",
    "EmaCrossoverState",
    "EmaPairResult",
    "KeltnerResult",
    "MacdResult",
    "RsiResult",
    "StochasticResult",
    "classify_atr_regime",
    "compute_adx",
    "compute_bollinger",
    "compute_ema_pairs",
    "compute_keltner",
    "compute_macd",
    "compute_rsi",
    "compute_stochastic",
]
