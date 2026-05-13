"""Loaders shim for q1 indicators (ALP-467).

See :mod:`.anomalies_loaders`; indicators read bars only.
"""

from __future__ import annotations

from alphamind.distillation.q1._loaders import Q1Inputs, load_q1_inputs

__all__ = ["Q1Inputs", "load_q1_inputs"]
