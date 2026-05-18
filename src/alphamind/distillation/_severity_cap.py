"""Calibration-state severity cap for anomaly flags (ALP-544).

A distillation module's anomaly flag is no stronger than the data it sits
on. A producer emits its "intended" severity (typically
``investigate_now`` for a fired threshold); this module caps that severity
by the source block's calibration state:

- :attr:`~CalibrationState.CALIBRATED` — full severity range allowed.
- :attr:`~CalibrationState.ACCUMULATING` — capped at ``investigate_if_persists``;
  the signal is real but the baseline distribution is undercalibrated.
- :attr:`~CalibrationState.UNAVAILABLE` — capped at ``note_for_context``;
  the underlying series is missing, so the alert is a "for your context"
  signal rather than an action item.

The cap is applied uniformly at the publishing layer in the distillation
orchestrator before blocks reach Phase 4 (aggregation) — every consumer
(sector assembly, correlation/regime brief, anomaly summary, per-block
renderer) reads the already-capped severity.

A flag whose ``name`` appears in ``exempt_flag_names`` bypasses the cap
entirely; some modules emit structural signals (e.g.,
``macro_surprise_anomaly``) whose magnitude does not depend on history
length, so the calibration state of the surrounding block doesn't gate
their severity. The exempt-flag-names set is loaded from
``config/distillation.yaml`` § ``severity_caps``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    AnomalySeverity,
    OutputBlock,
)

__all__ = [
    "cap_anomaly_severity",
    "cap_block_severities",
    "cap_blocks_for_calibration",
]


# Severity rank — lower index = stronger severity. Used to compose
# "the lower of (producer severity, cap ceiling)" without re-encoding the
# total order in multiple places.
_SEVERITY_RANK: dict[AnomalySeverity, int] = {
    "investigate_now": 0,
    "investigate_if_persists": 1,
    "note_for_context": 2,
}


# Per-state severity ceiling. CALIBRATED has no ceiling (None); the other
# two states pin a maximum severity that overrides any higher-rank
# producer choice.
_CEILING_BY_STATE: dict[CalibrationState, AnomalySeverity | None] = {
    CalibrationState.CALIBRATED: None,
    CalibrationState.ACCUMULATING: "investigate_if_persists",
    CalibrationState.UNAVAILABLE: "note_for_context",
}


def cap_anomaly_severity(
    *,
    severity: AnomalySeverity,
    state: CalibrationState,
    exempt: bool,
) -> AnomalySeverity:
    """Cap ``severity`` by the calibration ceiling for ``state``.

    ``exempt=True`` bypasses the cap — the producer's severity is
    returned unchanged. Otherwise the result is the weaker of
    (``severity``, ceiling-for-state); if ``severity`` is already weaker
    than the ceiling it is returned as-is (the cap never elevates).
    """
    if exempt:
        return severity
    ceiling = _CEILING_BY_STATE[state]
    if ceiling is None:
        return severity
    if _SEVERITY_RANK[severity] >= _SEVERITY_RANK[ceiling]:
        return severity
    return ceiling


def cap_block_severities(
    block: OutputBlock,
    *,
    exempt_flag_names: frozenset[str],
) -> OutputBlock:
    """Return a block whose anomaly_flags have calibration-capped severity.

    Returns the input ``block`` unchanged when no flag's severity changes
    — either because the block is calibrated, because it carries no
    flags, or because every flag is already at or below the ceiling.
    """
    if block.calibration_state is CalibrationState.CALIBRATED or not block.anomaly_flags:
        return block
    new_flags: list[AnomalyFlag] = []
    any_changed = False
    for flag in block.anomaly_flags:
        capped_severity = cap_anomaly_severity(
            severity=flag.severity,
            state=block.calibration_state,
            exempt=flag.name in exempt_flag_names,
        )
        if capped_severity == flag.severity:
            new_flags.append(flag)
        else:
            new_flags.append(replace(flag, severity=capped_severity))
            any_changed = True
    if not any_changed:
        return block
    return replace(block, anomaly_flags=tuple(new_flags))


def cap_blocks_for_calibration(
    blocks: Iterable[OutputBlock],
    *,
    exempt_flag_names: frozenset[str],
) -> list[OutputBlock]:
    """Cap every block's anomaly-flag severities by its calibration state.

    The orchestrator's distillation phase calls this once after every
    per-category compute has produced its blocks but before Phase 4
    (aggregation), so downstream consumers see the capped severities
    uniformly.
    """
    return [cap_block_severities(block, exempt_flag_names=exempt_flag_names) for block in blocks]
