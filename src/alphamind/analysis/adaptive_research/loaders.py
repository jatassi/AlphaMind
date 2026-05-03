"""Anomaly-stream loaders for the adaptive researcher — ALP-256.

Pure functions that flatten the two upstream anomaly streams into typed
records the input-bundle assembler (story 04b) renders into the user message.

Design intent (``docs/design/03-analysis-layer/adaptive-research.md`` § Inputs):
the agent's value-add is triage; loaders surface raw streams without collapsing
cross-stream duplicates.

Public names
------------
- :class:`DistillationAnomalyRecord` — one flag from a distillation block.
- :class:`SectorAnomalyRecord` — one anomaly from a domain-researcher brief.
- :class:`AdaptiveAnomalyInputs` — the two streams plus a freshness floor.
- :func:`extract_distillation_anomalies` — flatten ``DistillationOutputs.all_blocks``.
- :func:`extract_sector_anomalies` — walk ``SectorBrief.anomalies`` tuples.
- :func:`assemble_adaptive_anomaly_inputs` — compose the container.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field

from alphamind.analysis._shared import AnomalySeverity, Sector
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.distillation.orchestrator import DistillationOutputs

__all__ = [
    "AdaptiveAnomalyInputs",
    "DistillationAnomalyRecord",
    "SectorAnomalyRecord",
    "assemble_adaptive_anomaly_inputs",
    "extract_distillation_anomalies",
    "extract_sector_anomalies",
]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class DistillationAnomalyRecord(BaseModel, frozen=True):
    """One anomaly flag extracted from ``DistillationOutputs.all_blocks``.

    Trigger-attribution fields the LLM uses to construct its ``Trigger:`` field:

    - ``block_id`` — the OutputBlock's stable identifier (e.g., ``q1.volume_spike``).
    - ``flag_name`` — the ``AnomalyFlag.name`` within that block.
    - ``magnitude`` — the ``AnomalyFlag.magnitude`` (sigma / pct / pp depending on flag).
    - ``severity`` — ``investigate_now`` / ``investigate_if_persists`` / ``note_for_context``.
    - ``regime_context`` — the ``OutputBlock.regime_context`` one-liner when present.
    - ``freshness_ts`` — the ``OutputBlock.freshness_ts`` (UTC timestamp of the data).
    """

    block_id: str = Field(min_length=1)
    flag_name: str = Field(min_length=1)
    magnitude: float
    severity: AnomalySeverity
    regime_context: str | None
    freshness_ts: datetime


class SectorAnomalyRecord(BaseModel, frozen=True):
    """One anomaly extracted from a domain researcher's ``SectorBrief.anomalies``.

    Carries the upstream reference ID directly so the LLM's ``Trigger:`` field
    can cite it verbatim and the validator (story 03c) can resolve it.
    ``tickers`` may be empty — sector-wide anomalies are permitted by the
    upstream :class:`~alphamind.analysis.domain_researchers.models.Anomaly`
    model. ``sector`` is derived from the source ``SectorBrief.sector`` so the
    bundle renderer can group by sector without a second pass.
    """

    anomaly_id: str = Field(pattern=r"^SA-(TECH|FIN|ENERGY)-ANOM-\d+$")
    description: str = Field(min_length=1)
    # str rather than enum — the Anomaly.anomaly_type StrEnum value passes
    # through unchanged.
    anomaly_type: str = Field(min_length=1)
    tickers: tuple[str, ...]
    severity: AnomalySeverity
    suggested_question: str = Field(min_length=1)
    sector: Sector


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptiveAnomalyInputs:
    """The two anomaly streams plus a single freshness floor.

    ``data_freshness`` is the minimum (most stale) of the contributing
    record-set freshness timestamps so the agent can detect that one stream
    is stale and signal that in the eventual brief. Sector anomalies have no
    freshness timestamp (sector briefs are always fresh-as-of-this-invocation
    by construction); only distillation contributes candidates. When the
    distillation stream is empty, ``data_freshness`` falls through to ``as_of``.
    """

    distillation: tuple[DistillationAnomalyRecord, ...]
    sector: tuple[SectorAnomalyRecord, ...]
    data_freshness: datetime


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def extract_distillation_anomalies(
    outputs: DistillationOutputs,
) -> tuple[DistillationAnomalyRecord, ...]:
    """Flatten every ``OutputBlock.anomaly_flags`` into a deterministic tuple.

    Walks ``outputs.all_blocks`` in the order the orchestrator emitted them,
    then within each block iterates ``anomaly_flags`` in the order they were
    attached. Returns ``()`` when no block carries any anomaly flag.
    """
    return tuple(
        DistillationAnomalyRecord(
            block_id=block.block_id,
            flag_name=flag.name,
            magnitude=flag.magnitude,
            severity=flag.severity,
            regime_context=block.regime_context,
            freshness_ts=block.freshness_ts,
        )
        for block in outputs.all_blocks
        for flag in block.anomaly_flags
    )


def extract_sector_anomalies(
    sector_briefs: tuple[SectorBrief, ...],
) -> tuple[SectorAnomalyRecord, ...]:
    """Walk each ``SectorBrief.anomalies`` tuple into typed records.

    Iterates ``sector_briefs`` in given order, then within each brief iterates
    ``brief.anomalies`` in given order. Returns ``()`` when no brief carries
    any anomaly.
    """
    return tuple(
        SectorAnomalyRecord(
            anomaly_id=anomaly.anomaly_id,
            description=anomaly.description,
            anomaly_type=anomaly.anomaly_type.value,
            tickers=anomaly.tickers,
            severity=anomaly.severity,
            suggested_question=anomaly.suggested_question,
            sector=brief.sector,
        )
        for brief in sector_briefs
        for anomaly in brief.anomalies
    )


def assemble_adaptive_anomaly_inputs(
    *,
    distillation_outputs: DistillationOutputs,
    sector_briefs: tuple[SectorBrief, ...],
    as_of: datetime,
) -> AdaptiveAnomalyInputs:
    """Compose the :class:`AdaptiveAnomalyInputs` container from upstream objects.

    ``data_freshness`` is the minimum ``freshness_ts`` across all
    :class:`DistillationAnomalyRecord`s. When the distillation stream is empty,
    ``data_freshness`` falls through to ``as_of``.
    """
    distillation = extract_distillation_anomalies(distillation_outputs)
    sector = extract_sector_anomalies(sector_briefs)
    data_freshness = min(rec.freshness_ts for rec in distillation) if distillation else as_of
    return AdaptiveAnomalyInputs(
        distillation=distillation,
        sector=sector,
        data_freshness=data_freshness,
    )
