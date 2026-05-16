"""Q3 options anomaly detections — thin orchestration shim (story ALP-484).

The pure compute lives in :mod:`.anomalies_compute`; the IO shell (DB reads
through ``Session`` and the :class:`DistillationRepository` Protocol) lives
in :mod:`.anomalies_loaders`. This module preserves the legacy
session-accepting public API so existing callers (and the external test
suite at ``tests/distillation/external/test_q3_options.py``) continue to
work without import churn.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.orm import Session

from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.distillation.q3.anomalies_compute import (
    LowOiSnapshotPair,
    LowOiVolumeAnomaly,
    LowOiVolumeAnomalyInputs,
    SectorSweepInputs,
    SectorWideSweep,
    SweepDirection,
    compute_low_oi_volume_anomalies,
    compute_sector_wide_sweeps,
)
from alphamind.distillation.q3.anomalies_loaders import (
    load_low_oi_volume_anomaly_inputs,
    load_sector_sweep_inputs,
)
from alphamind.distillation.q3.pair_trade import FlowZScore


def detect_low_oi_volume_anomalies(
    session: Session,
    *,
    as_of: str,
    volume_multiple_threshold: float,
    oi_threshold: int,
) -> list[LowOiVolumeAnomaly]:
    """Session-accepting shim that pre-loads inputs and delegates to compute."""
    inputs = load_low_oi_volume_anomaly_inputs(session, as_of=as_of)
    return compute_low_oi_volume_anomalies(
        inputs,
        volume_multiple_threshold=volume_multiple_threshold,
        oi_threshold=oi_threshold,
    )


def detect_sector_wide_sweeps(
    session: Session,
    *,
    flow_zscores: Mapping[str, FlowZScore],
    sigma_threshold: float,
    min_names: int,
) -> list[SectorWideSweep]:
    """Session-accepting shim that pre-loads sector membership and delegates."""
    repository = SqlDistillationRepository(session)
    inputs = load_sector_sweep_inputs(repository, flow_zscores=flow_zscores)
    return compute_sector_wide_sweeps(
        inputs,
        sigma_threshold=sigma_threshold,
        min_names=min_names,
    )


__all__ = [
    "LowOiSnapshotPair",
    "LowOiVolumeAnomaly",
    "LowOiVolumeAnomalyInputs",
    "SectorSweepInputs",
    "SectorWideSweep",
    "SweepDirection",
    "detect_low_oi_volume_anomalies",
    "detect_sector_wide_sweeps",
]
