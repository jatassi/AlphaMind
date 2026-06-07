"""Calibration-state severity cap for anomaly flags.

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
orchestrator before the aggregation step — every consumer (sector assembly,
correlation/regime brief, anomaly summary, per-block renderer) reads the
already-capped severity.

A flag whose ``name`` appears in ``exempt_flag_names`` bypasses the cap
entirely; some modules emit structural signals (e.g.,
``macro_surprise_anomaly``) whose magnitude does not depend on history
length, so the calibration state of the surrounding block doesn't gate
their severity. The exempt-flag-names set is loaded from
``config/distillation.yaml`` § ``severity_caps``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    AnomalySeverity,
    OutputBlock,
    severity_rank,
)

__all__ = [
    "cap_anomaly_severity",
    "cap_block_severities",
    "cap_blocks_for_calibration",
]

# Payload contract for blocks that stage producer severity into per-ticker
# rows (currently only the q1 anomaly producers — see ALP-627). The cap
# rewrites these in lockstep with ``block.anomaly_flags`` so the per-ticker
# renderer and the rollup renderer agree on a single capped severity.
_PER_TICKER_KEY = "per_ticker"
_SEVERITY_KEY = "severity"


# Per-state severity ceiling. CALIBRATED has no ceiling (None); the other
# two states pin a maximum severity that overrides any higher-rank
# producer choice. The total order over severity literals lives next to
# the type in :mod:`alphamind.distillation.output`.
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
    if severity_rank(severity) >= severity_rank(ceiling):
        return severity
    return ceiling


def cap_block_severities(
    block: OutputBlock,
    *,
    exempt_flag_names: frozenset[str],
) -> OutputBlock:
    """Return a block whose anomaly_flags and per-ticker payload severities are capped.

    Two slices of the same block carry severity: ``block.anomaly_flags``
    (rollup) and — for the q1 anomaly producers — ``payload["per_ticker"]
    [ticker]["severity"]`` (per-ticker view). Both must reflect the
    calibration-state cap so the per-ticker renderer and the rollup
    renderer never disagree (ALP-627).

    Returns the input ``block`` unchanged when nothing changes — either
    because the block is calibrated, because it carries no flags, or
    because every flag (and every mirrored per-ticker severity) is
    already at or below the ceiling.
    """
    if block.calibration_state is CalibrationState.CALIBRATED or not block.anomaly_flags:
        return block
    new_flags: list[AnomalyFlag] = []
    flag_changed = False
    capped_severity_by_name: dict[str, AnomalySeverity] = {}
    for flag in block.anomaly_flags:
        capped_severity = cap_anomaly_severity(
            severity=flag.severity,
            state=block.calibration_state,
            exempt=flag.name in exempt_flag_names,
        )
        capped_severity_by_name[flag.name] = capped_severity
        if capped_severity == flag.severity:
            new_flags.append(flag)
        else:
            new_flags.append(replace(flag, severity=capped_severity))
            flag_changed = True
    new_payload, payload_changed = _cap_per_ticker_payload(
        block.payload, block.block_id, capped_severity_by_name
    )
    if not flag_changed and not payload_changed:
        return block
    return replace(block, anomaly_flags=tuple(new_flags), payload=new_payload)


def _cap_per_ticker_payload(
    payload: Mapping[str, Any],
    block_id: str,
    capped_severity_by_name: Mapping[str, AnomalySeverity],
) -> tuple[Mapping[str, Any], bool]:
    """Return ``(new_payload, changed)`` with per-ticker severities capped.

    Only ``payload["per_ticker"][ticker]["severity"]`` is rewritten. The
    target severity is the unique capped value from the block's flags —
    q1 anomaly blocks emit one flag-name per block (``volume_anomaly`` or
    ``price_move_anomaly``), so the mapping is unambiguous. A block whose
    flags split across multiple distinct names while also baking severity
    into the per-ticker payload would have no defined per-row mapping;
    that combination doesn't occur in production today and is rejected
    rather than silently regressing to the ALP-627 bug.
    """
    per_ticker = payload.get(_PER_TICKER_KEY)
    if not isinstance(per_ticker, Mapping):
        return payload, False
    distinct_severities = set(capped_severity_by_name.values())
    if len(distinct_severities) != 1:
        if any(
            isinstance(entry, Mapping) and _SEVERITY_KEY in entry for entry in per_ticker.values()
        ):
            raise AssertionError(
                f"block {block_id!r}: per-ticker payload severity requires a single "
                f"flag-name per block, but flags resolved to "
                f"{sorted(capped_severity_by_name)} with severities "
                f"{sorted(distinct_severities)}"
            )
        return payload, False
    target_severity = next(iter(distinct_severities))
    new_per_ticker: dict[str, Any] = {}
    changed = False
    for ticker, entry in per_ticker.items():
        if isinstance(entry, Mapping) and _SEVERITY_KEY in entry:
            current = entry[_SEVERITY_KEY]
            if current != target_severity:
                new_per_ticker[ticker] = {**entry, _SEVERITY_KEY: target_severity}
                changed = True
            else:
                new_per_ticker[ticker] = entry
        else:
            new_per_ticker[ticker] = entry
    if not changed:
        return payload, False
    return {**payload, _PER_TICKER_KEY: new_per_ticker}, True


def cap_blocks_for_calibration(
    blocks: Iterable[OutputBlock],
    *,
    exempt_flag_names: frozenset[str],
) -> list[OutputBlock]:
    """Cap every block's anomaly-flag severities by its calibration state.

    The orchestrator calls this once after every per-category compute step
    has produced its blocks but before aggregation, so downstream consumers
    see the capped severities uniformly.
    """
    return [cap_block_severities(block, exempt_flag_names=exempt_flag_names) for block in blocks]
