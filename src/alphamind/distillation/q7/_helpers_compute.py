"""Compute-alias re-export for q7 shared helpers (ALP-486).

The :mod:`._helpers` module is the pure-math core for the q7 sub-package
(ALP-486 retired its session-bound ``_select_close_series`` helper into
:mod:`._loaders`). This module re-exports the same surface under the
``*_compute.py`` naming convention introduced by the ALP-467 compute/load
boundary split, so the import-linter contract
``distillation-compute-no-sqlalchemy`` can enumerate it explicitly.
"""

from __future__ import annotations

from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _calibration_for_window,
    _correlation_matrix,
    _format_iso_utc,
    _log_returns_from_closes,
    _max_lagged_correlation,
    _pearson_correlation,
    _window_bounds,
    _zscore,
)

__all__ = [
    "_BLOCK_NAMESPACE",
    "_calibration_for_window",
    "_correlation_matrix",
    "_format_iso_utc",
    "_log_returns_from_closes",
    "_max_lagged_correlation",
    "_pearson_correlation",
    "_window_bounds",
    "_zscore",
]
