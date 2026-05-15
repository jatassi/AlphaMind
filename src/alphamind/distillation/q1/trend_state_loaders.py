"""Loaders shim for q1 trend_state (ALP-467).

See :mod:`.anomalies_loaders`; trend-state reads bars plus the per-ticker
ATR baselines the shared loader returns on :class:`Q1Inputs`.
"""

from __future__ import annotations

from alphamind.distillation.q1._loaders import Q1Inputs, load_q1_inputs

__all__ = ["Q1Inputs", "load_q1_inputs"]
