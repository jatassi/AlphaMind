"""Loaders shim for q1 relative_performance (ALP-467).

See :mod:`.anomalies_loaders`; relative-performance reads bars plus the
pre-computed SPY / sector-ETF window returns the shared loader caches on
:class:`Q1Inputs`.
"""

from __future__ import annotations

from alphamind.distillation.q1._loaders import Q1Inputs, load_q1_inputs

__all__ = ["Q1Inputs", "load_q1_inputs"]
