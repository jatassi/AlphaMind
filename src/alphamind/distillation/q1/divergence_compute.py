"""Compute-alias re-export for q1 divergence (ALP-467).

See :mod:`.anomalies_compute` for the convention; the divergence module
is already a pure compute core, this re-export lets the import-linter
contract pin it explicitly.
"""

from __future__ import annotations

from alphamind.distillation.q1.divergence import (
    DivergenceFlag,
    detect_rsi_divergences,
)

__all__ = [
    "DivergenceFlag",
    "detect_rsi_divergences",
]
