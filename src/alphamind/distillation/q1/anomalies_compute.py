"""Compute-alias re-export for q1 anomalies (ALP-467).

The :mod:`.anomalies` module is already a pure compute core (no
sqlalchemy edges). This module re-exports the same surface under the
``*_compute.py`` naming convention introduced by the ALP-467 compute/load
boundary split, so the import-linter contract
``distillation-compute-no-sqlalchemy`` can enumerate it explicitly.

There is no corresponding ``anomalies_loaders.py`` because the inputs the
anomaly detections consume (bars, baselines) are loaded by the shared
:mod:`._loaders` module — every q1 sub-module reads off the same
pre-loaded :class:`Q1Inputs`.
"""

from __future__ import annotations

from alphamind.distillation.q1.anomalies import (
    detect_price_move_anomaly,
    detect_volume_anomaly,
)

__all__ = [
    "detect_price_move_anomaly",
    "detect_volume_anomaly",
]
