"""Shared filename + directory constants for the per-invocation archive layout.

Both the orchestrator-side row composer (``alphamind.scheduler.invocation``)
and the distillation-side snapshot writer
(``alphamind.distillation.calibration_snapshot``) write to the same
``<archive_root>/invocations/<invocation_id>/data_calibration_state.json``
target. The original code carried the filename + dirname as private
constants in both modules, so a rename touched two places. ALP-450 item 4
lifted them here so the contract lives in one location.
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
