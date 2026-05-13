"""Loaders shim for q1 anomalies (ALP-467).

Anomaly detection consumes the same ``bars_by_ticker`` + per-ticker
baselines the rest of the q1 sub-modules read. Loading happens once per
invocation in the shared :mod:`._loaders` module and is carried via
:class:`Q1Inputs`. This module re-exports the relevant projection helpers
for direct consumers.

The compute-side helpers live in :mod:`.anomalies_compute`.
"""

from __future__ import annotations

from alphamind.distillation.q1._loaders import Q1Inputs, load_q1_inputs

__all__ = ["Q1Inputs", "load_q1_inputs"]
