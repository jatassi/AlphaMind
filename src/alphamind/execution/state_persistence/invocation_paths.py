"""Shared filename + directory constants for the per-invocation archive layout.

Every writer and reader of files under ``<archive_root>/invocations/<id>/``
imports its directory and filename strings from this module so the on-disk
contract lives in one location. Renaming a path component is a single-line
change here.
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
