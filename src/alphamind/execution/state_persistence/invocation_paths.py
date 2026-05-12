"""Shared filename + directory constants for the per-invocation archive layout.

Pins the ``invocations`` subdirectory and the two top-level per-invocation
snapshot filenames (``resolved_config.json``, ``data_calibration_state.json``)
that the orchestrator writes into ``<archive_root>/invocations/<id>/``.
Renaming one of these components is a single-line change here.
"""

from __future__ import annotations

INVOCATIONS_DIRNAME: str = "invocations"
"""Subdirectory under ``archive_root`` where per-invocation artifacts land."""

RESOLVED_CONFIG_FILENAME: str = "resolved_config.json"
"""Per-invocation resolved-config snapshot filename.

Pinned by ``docs/design/05-execution-layer/state-persistence.md``
§ Filesystem snapshot layout.
"""

CALIBRATION_SNAPSHOT_FILENAME: str = "data_calibration_state.json"
"""Per-invocation calibration-state snapshot filename.

Pinned by ``docs/design/02-distillation-layer/threshold-calibration.md``
§ Calibration-state snapshot file.
"""

__all__ = [
    "CALIBRATION_SNAPSHOT_FILENAME",
    "INVOCATIONS_DIRNAME",
    "RESOLVED_CONFIG_FILENAME",
]
