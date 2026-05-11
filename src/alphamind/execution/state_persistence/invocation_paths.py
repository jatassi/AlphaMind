"""Shared filename + directory constants for the per-invocation archive layout.

The orchestrator-side row composer (``alphamind.scheduler.invocation``)
and the distillation-side snapshot writer
(``alphamind.distillation.calibration_snapshot``) both write to
``<archive_root>/invocations/<invocation_id>/data_calibration_state.json``;
the constants live here so the two writers share one source of truth.
"""

from __future__ import annotations

INVOCATIONS_DIRNAME: str = "invocations"
"""Subdirectory under ``archive_root`` where per-invocation artifacts land."""

CALIBRATION_SNAPSHOT_FILENAME: str = "data_calibration_state.json"
"""Per-invocation calibration-state snapshot filename.

Pinned by ``docs/design/02-distillation-layer/threshold-calibration.md``
§ Calibration-state snapshot file.
"""

__all__ = [
    "CALIBRATION_SNAPSHOT_FILENAME",
    "INVOCATIONS_DIRNAME",
]
