"""Calibration-state snapshot writer — story 02-distillation-layer/17.

Reduces every :class:`OutputBlock` carried by a :class:`DistillationOutputs`
into the per-invocation JSON file documented in
``docs/design/02-distillation-layer/threshold-calibration.md``
§ Calibration-state snapshot file. The file is the deterministic-analytics-spine
input the feedback loop reads to condition outcome analysis on whether
bootstrap fallback was active during the invocation; the command center's
calibration-mix panel renders the same file
(``docs/design/command-center.md`` § E. Risk and guardrails).

The writer is invoked from the orchestrator's phase 6 (invocation-archive
write) so the snapshot lands alongside the markdown archive on the same
fail-closed path. Determinism is load-bearing — same input must produce
byte-identical output — and the write is atomic (temp file + rename) so a
mid-write process death does not leave a torn JSON document on disk.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphamind._kernel.atomic_io import atomic_write_text
from alphamind._kernel.invocations import (
    CALIBRATION_SNAPSHOT_FILENAME,
    INVOCATIONS_DIRNAME,
)
from alphamind.distillation.calibration import CALIBRATION_STATE_VALUES, CalibrationState
from alphamind.distillation.output import OutputBlock

if TYPE_CHECKING:
    from alphamind.distillation.orchestrator import DistillationOutputs

# ---------------------------------------------------------------------------
# Schema vocabulary
# ---------------------------------------------------------------------------

SCHEMA_VERSION: str = "1"
"""Schema version field value.

Bumped only when the on-disk shape changes incompatibly. Future schema
work owns its own migration; this story lays the bootstrap version.
"""


_JSON_INDENT_UNIT: str = "  "
"""One indentation unit for the on-disk JSON payload.

The serializer reads ``len(_JSON_INDENT_UNIT)`` so the visual unit and the
indent count cannot drift.
"""


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _empty_state_counts() -> dict[str, int]:
    """Return a fully-populated zero-valued state-count dict.

    Every snapshot reports counts for every member of
    :class:`CalibrationState` even when zero — the schema's ``by_state``
    field is a fixed-shape dict so downstream consumers can read keys
    without absence checks.
    """
    return dict.fromkeys(CALIBRATION_STATE_VALUES, 0)


def _aggregate_by_state(blocks: Iterable[OutputBlock]) -> dict[str, int]:
    counts = _empty_state_counts()
    for block in blocks:
        counts[block.calibration_state.value] += 1
    return counts


def _aggregate_by_audience(blocks: Iterable[OutputBlock]) -> dict[str, dict[str, int]]:
    """Per-audience breakdown.

    A multi-audience block contributes one count to every audience in its
    ``audience`` set, so per-audience sums may exceed ``total_blocks``.
    """
    per_audience: dict[str, dict[str, int]] = {}
    for block in blocks:
        for audience in block.audience:
            counts = per_audience.setdefault(audience.value, _empty_state_counts())
            counts[block.calibration_state.value] += 1
    return per_audience


def _aggregate_by_block_kind(blocks: Iterable[OutputBlock]) -> dict[str, dict[str, int]]:
    """Per-block-kind breakdown.

    The block-kind key is the block's ``block_id`` directly (the
    ``<category>.<short_name>`` form pinned in story 05). Per-ticker
    payload entries within a single block contribute one count to the
    block-kind regardless of how many tickers the payload carries — the
    drill-in panel handles the per-ticker breakdown so the summary stays
    scannable.
    """
    per_kind: dict[str, dict[str, int]] = {}
    for block in blocks:
        counts = per_kind.setdefault(block.block_id, _empty_state_counts())
        counts[block.calibration_state.value] += 1
    return per_kind


def _aggregate_reasons(blocks: Iterable[OutputBlock], state: CalibrationState) -> dict[str, str]:
    """Collect ``block_id -> bootstrap_reason`` for every block in ``state``.

    Each distinct ``block_id`` produces one entry. When the same
    ``block_id`` appears more than once in ``state`` the last reason wins
    in iteration order; the orchestrator emits blocks deterministically so
    the choice is reproducible.
    """
    return {
        block.block_id: block.bootstrap_reason or ""
        for block in blocks
        if block.calibration_state is state
    }


# ---------------------------------------------------------------------------
# Serialization and persistence
# ---------------------------------------------------------------------------


def _format_as_of(as_of: datetime) -> str:
    """Format a tz-aware datetime as ISO 8601 ``Z``-suffixed UTC.

    Mirrors :func:`alphamind.distillation.orchestrator._format_as_of` so the
    snapshot's ``as_of`` field aligns with the timestamp the rest of the
    orchestrator emits.
    """
    return as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _serialize(snapshot: dict[str, Any]) -> str:
    """Render the snapshot payload as deterministic JSON.

    Sorted keys plus a fixed indentation guarantees byte-identical output
    across runs against the same input. The trailing newline keeps the
    file POSIX-clean for tooling that expects newline-terminated text.
    """
    return json.dumps(snapshot, sort_keys=True, indent=len(_JSON_INDENT_UNIT)) + "\n"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def write_calibration_state_snapshot(
    outputs: DistillationOutputs,
    invocation_id: str,
    base_path: Path,
) -> Path:
    """Write the per-invocation calibration-state snapshot and return the path.

    Walks every :class:`OutputBlock` carried on ``outputs.all_blocks``
    (the union of sector blocks, correlation/regime brief blocks, and the
    universal regime block — the orchestrator places them all on the
    dataclass for this exact reduction). Aggregates per-state, per-
    audience, and per-block-kind counts; populates the bootstrap- and
    unavailable-reason maps for non-calibrated blocks; and writes the
    deterministic JSON document to
    ``<base_path>/invocations/<invocation_id>/data_calibration_state.json``.

    The returned path is the file location the caller records on the
    invocation row's ``data_calibration_state_reference`` field. Callers
    are not required to read the file back — the writer guarantees
    byte-identical output for the same ``DistillationOutputs``.
    """
    blocks = outputs.all_blocks

    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "invocation_id": invocation_id,
        "as_of": _format_as_of(outputs.as_of),
        "summary": {
            "total_blocks": len(blocks),
            "by_state": _aggregate_by_state(blocks),
            "by_audience": _aggregate_by_audience(blocks),
            "by_block_kind": _aggregate_by_block_kind(blocks),
        },
        "bootstrap_reasons": _aggregate_reasons(blocks, CalibrationState.BOOTSTRAP),
        "unavailable_reasons": _aggregate_reasons(blocks, CalibrationState.UNAVAILABLE),
    }

    target_path = base_path / INVOCATIONS_DIRNAME / invocation_id / CALIBRATION_SNAPSHOT_FILENAME
    atomic_write_text(target_path, _serialize(snapshot))
    return target_path


__all__ = [
    "SCHEMA_VERSION",
    "write_calibration_state_snapshot",
]
