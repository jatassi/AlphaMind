"""Filesystem snapshot writers for invocation provenance (story 02b).

Two narrowly-scoped helpers ship the on-disk side of invocation provenance.
The directory layouts mirror the design doc at
``docs/design/05-execution-layer/state-persistence.md``:

* ``write_pip_freeze_snapshot`` lays down ``pip_freeze.txt`` under
  ``<root>/process_lifetimes/<id>/``.
* ``write_invocation_provenance_snapshots`` lays down both
  ``resolved_config.json`` and ``data_calibration_state.json`` under
  ``<root>/invocations/<id>/``.

Both helpers create parent directories as needed and overwrite existing
files (the per-process / per-invocation IDs are unique, so re-writes only
happen when an operator deliberately re-runs setup).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_pip_freeze_snapshot(
    *,
    process_lifetime_id: str,
    pip_freeze_text: str,
    root: str,
) -> str:
    """Write the per-process-lifetime ``pip_freeze.txt`` and return its path.

    Layout::

        <root>/process_lifetimes/<process_lifetime_id>/pip_freeze.txt
    """
    target_dir = Path(root) / "process_lifetimes" / process_lifetime_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "pip_freeze.txt"
    target.write_text(pip_freeze_text)
    return str(target)


def write_invocation_provenance_snapshots(
    *,
    invocation_id: str,
    resolved_config: dict[str, Any],
    data_calibration_state: dict[str, Any],
    root: str,
) -> tuple[str, str]:
    """Write the two per-invocation provenance snapshots and return their paths.

    Layout::

        <root>/invocations/<invocation_id>/resolved_config.json
        <root>/invocations/<invocation_id>/data_calibration_state.json

    The two payloads are written via ``json.dumps`` with sorted keys for
    deterministic byte-level output (eases diff-on-hash comparisons across
    invocations).
    """
    target_dir = Path(root) / "invocations" / invocation_id
    target_dir.mkdir(parents=True, exist_ok=True)

    config_path = target_dir / "resolved_config.json"
    calibration_path = target_dir / "data_calibration_state.json"

    config_path.write_text(json.dumps(resolved_config, sort_keys=True, indent=2))
    calibration_path.write_text(json.dumps(data_calibration_state, sort_keys=True, indent=2))

    return str(config_path), str(calibration_path)
