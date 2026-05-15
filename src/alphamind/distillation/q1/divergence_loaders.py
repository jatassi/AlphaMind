"""Loaders shim for q1 divergence (ALP-467).

See :mod:`.anomalies_loaders`; divergence reads bars only and the shared
:mod:`._loaders` covers the load.
"""

from __future__ import annotations

from alphamind.distillation.q1._loaders import Q1Inputs, load_q1_inputs

__all__ = ["Q1Inputs", "load_q1_inputs"]
