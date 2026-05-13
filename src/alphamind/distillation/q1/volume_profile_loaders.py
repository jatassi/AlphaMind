"""Loaders shim for q1 volume_profile (ALP-467).

See :mod:`.anomalies_loaders`; volume profile reads bars only.
"""

from __future__ import annotations

from alphamind.distillation.q1._loaders import Q1Inputs, load_q1_inputs

__all__ = ["Q1Inputs", "load_q1_inputs"]
