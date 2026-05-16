"""Q3 ETF IV vs. single-name IV divergence — thin shim (story ALP-484).

The pure compute lives in :mod:`.etf_iv_divergence_compute`; the IO shell
that builds the per-sector input mapping lives in
:mod:`.etf_iv_divergence_loaders`. This module re-exports the public API
(:class:`EtfIvDivergence`, :class:`EtfIvDivergenceDirection`,
:func:`compute_etf_iv_divergences`) so existing call sites do not change.
"""

from __future__ import annotations

from alphamind.distillation.q3.etf_iv_divergence_compute import (
    EtfIvDivergence,
    EtfIvDivergenceDirection,
    EtfIvDivergenceInputs,
    compute_etf_iv_divergences,
)

__all__ = [
    "EtfIvDivergence",
    "EtfIvDivergenceDirection",
    "EtfIvDivergenceInputs",
    "compute_etf_iv_divergences",
]
