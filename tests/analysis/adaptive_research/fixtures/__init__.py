"""Recorded fixture upstream-brief tuple for adaptive-researcher e2e tests.

Per ALP-265 § Scope, the e2e test fakes the upstream pipeline composition by
loading a recorded ``(tuple[SectorBrief, ...], QualitativeBrief,
CorrelationRegimeBrief, DistillationOutputs, universal_regime_label)`` tuple
rather than running domain researchers + qualitative + distillation live.

Generation strategy
-------------------
The fixtures are hand-constructed JSON payloads, not captured from the
production database. The dev-machine production DB at
``/Volumes/Users/jacks/AlphaMind/data/alphamind.db`` is unreachable from the
worktree sandbox where this story was implemented. Hand-construction is
deterministic, has no DB dependency, and is sufficient to exercise the
runner's all-four-sections code path through the input-bundle assembler and
the validator's six-prefix Layer-3 reference universe — that is the contract
the e2e test verifies, not data realism.

Fixture layout
--------------
* ``sector_briefs.json`` — three :class:`SectorBrief` payloads (one per
  sector) carrying findings, anomalies, and thesis candidates with
  ``SA-{SECTOR}-N`` / ``SA-{SECTOR}-ANOM-N`` / ``SA-{SECTOR}-TC-N`` IDs.
* ``qualitative_brief.json`` — one :class:`QualitativeBrief` payload with
  ``QR-N`` narrative threads, ``QR-CW-N`` catalyst watches, and a
  populated sentiment snapshot.
* ``correlation_regime_brief.json`` — one :class:`CorrelationRegimeBrief`
  payload with a ``CR-N``-keyed reference index.
* ``distillation_outputs.json`` — multi-block :class:`DistillationOutputs`
  payload with mixed audience and mixed anomaly counts (volume-spike,
  correlation-breakdown, macro-surprise flags).
* ``universal_regime_label.json`` — the regime payload as a
  ``dict[str, Any]`` with the four canonical keys the input-bundle
  renderer reads (``regime``, ``transition_flag``, ``confidence``,
  ``freshness_ts``).

Refresh policy
--------------
Re-generate fixtures only when an upstream contract changes (a Pydantic
model field renamed, a dataclass field added, a referential-prefix
family extended). The fixture files are committed; refreshing them is a
deliberate, reviewable change.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.sector_assembly import SectorOutput

__all__ = ["load_e2e_fixtures"]


_FIXTURES_DIR = Path(__file__).resolve().parent


def _read_json(name: str) -> Any:
    """Read and parse a fixture JSON file by basename."""
    with (_FIXTURES_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; accept ``Z`` or explicit offset."""
    # ``datetime.fromisoformat`` on 3.11+ accepts a trailing ``Z`` directly.
    return datetime.fromisoformat(value)


def _build_anomaly_flag(payload: dict[str, Any]) -> AnomalyFlag:
    return AnomalyFlag(
        name=payload["name"],
        magnitude=float(payload["magnitude"]),
        severity=payload["severity"],
    )


def _build_output_block(payload: dict[str, Any]) -> OutputBlock:
    return OutputBlock(
        block_id=payload["block_id"],
        audience=frozenset(OutputAudience(value) for value in payload["audience"]),
        freshness_ts=_parse_dt(payload["freshness_ts"]),
        calibration_state=CalibrationState(payload["calibration_state"]),
        bootstrap_reason=payload["bootstrap_reason"],
        payload=payload["payload"],
        anomaly_flags=tuple(_build_anomaly_flag(flag) for flag in payload["anomaly_flags"]),
        regime_context=payload["regime_context"],
    )


def _build_sector_output(payload: dict[str, Any]) -> SectorOutput:
    return SectorOutput(
        audience=OutputAudience(payload["audience"]),
        sector_label=payload["sector_label"],
        text=payload["text"],
        tickers=tuple(payload["tickers"]),
        block_ids=tuple(payload["block_ids"]),
        freshness_min=_parse_dt(payload["freshness_min"]),
    )


def _build_correlation_regime_brief() -> CorrelationRegimeBrief:
    payload = _read_json("correlation_regime_brief.json")
    return CorrelationRegimeBrief(
        text=payload["text"],
        reference_index=dict(payload["reference_index"]),
        freshness_min=_parse_dt(payload["freshness_min"]),
    )


def _build_distillation_outputs(
    correlation_regime_brief: CorrelationRegimeBrief,
    universal_regime_label: dict[str, Any],
) -> DistillationOutputs:
    payload = _read_json("distillation_outputs.json")
    sector_outputs = {
        OutputAudience(entry["audience"]): _build_sector_output(entry)
        for entry in payload["sector_outputs"]
    }
    all_blocks = tuple(_build_output_block(block) for block in payload["all_blocks"])
    return DistillationOutputs(
        sector_outputs=sector_outputs,
        correlation_regime_brief=correlation_regime_brief,
        universal_regime_label=universal_regime_label,
        invocation_id=payload["invocation_id"],
        as_of=_parse_dt(payload["as_of"]),
        total_blocks=int(payload["total_blocks"]),
        total_anomalies=int(payload["total_anomalies"]),
        bootstrap_block_count=int(payload["bootstrap_block_count"]),
        all_blocks=all_blocks,
    )


def load_e2e_fixtures() -> tuple[
    tuple[SectorBrief, ...],
    QualitativeBrief,
    CorrelationRegimeBrief,
    DistillationOutputs,
    dict[str, Any],
]:
    """Load and reconstruct the recorded upstream-brief tuple.

    Returns
    -------
    tuple
        ``(sector_briefs, qualitative_brief, correlation_regime_brief,
        distillation_outputs, universal_regime_label)`` — typed objects
        ready to pass to
        :func:`alphamind.analysis.adaptive_research.runner.run_adaptive_researcher`.
    """
    sector_payload = _read_json("sector_briefs.json")
    sector_briefs = tuple(SectorBrief.model_validate(entry) for entry in sector_payload)

    qualitative_payload = _read_json("qualitative_brief.json")
    qualitative_brief = QualitativeBrief.model_validate(qualitative_payload)

    correlation_regime_brief = _build_correlation_regime_brief()
    universal_regime_label = _read_json("universal_regime_label.json")
    distillation_outputs = _build_distillation_outputs(
        correlation_regime_brief, universal_regime_label
    )
    return (
        sector_briefs,
        qualitative_brief,
        correlation_regime_brief,
        distillation_outputs,
        universal_regime_label,
    )
