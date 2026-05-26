"""Stage-artifact dump/load helpers for the verification scripts (ALP-287).

Each phase 2-5 verification script writes its parsed result(s) to a
canonical per-invocation directory under
``<archive_root>/<YYYY-MM-DD>/<invocation_id>/stage_artifacts/``, and the
downstream scripts read those artifacts when given ``--upstream-from``.
That replaces the hand-constructed fixture upstream the phase 4 + 5
scripts used to consume, so a green end-to-end run proves today's actual
upstream artifacts flow correctly through the analysis-layer pipeline.

Each ``dump_<type>(obj, dir)`` / ``load_<type>(dir)`` pair operates on a
fixed filename inside the stage-artifacts directory; callers pass the
directory rather than the file path so the on-disk layout stays
encapsulated. Pydantic-model artifacts (sector briefs, qualitative
brief, adaptive brief, retrieval store) round-trip via
``model_dump_json`` / ``model_validate_json``. The dataclass artifacts
(``DistillationOutputs``, ``CorrelationRegimeBrief``) go through
explicit dict-encoders that handle datetimes, enums, and the nested
``OutputBlock`` / ``SectorOutput`` / ``AnomalyFlag`` shapes — mirroring
the existing fixture loader at
``tests/analysis/adaptive_research/fixtures/__init__.py``.

A consumer script that finds the stage-artifacts directory missing the
artifact it needs raises :class:`MissingArtifactError` carrying the
producer-script name so the operator sees "run phase N first" instead
of a bare ``FileNotFoundError``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from alphamind._kernel.archive_layout import invocation_archive_dir
from alphamind.analysis.adaptive_research.models import AdaptiveBrief
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.analysis.synthesizer.models import BriefSource
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.sector_assembly import SectorOutput

__all__ = [
    "ADAPTIVE_BRIEF_FILENAME",
    "CORRELATION_REGIME_BRIEF_FILENAME",
    "DISTILLATION_OUTPUTS_FILENAME",
    "QUALITATIVE_BRIEF_FILENAME",
    "RETRIEVAL_STORE_FILENAME",
    "SECTOR_BRIEFS_FILENAME",
    "STAGE_ARTIFACTS_SUBDIR",
    "UNIVERSAL_REGIME_LABEL_FILENAME",
    "MissingArtifactError",
    "dump_adaptive_brief",
    "dump_correlation_regime_brief",
    "dump_distillation_outputs",
    "dump_qualitative_brief",
    "dump_retrieval_store",
    "dump_sector_briefs",
    "dump_universal_regime_label",
    "load_adaptive_brief",
    "load_correlation_regime_brief",
    "load_distillation_outputs",
    "load_qualitative_brief",
    "load_retrieval_store",
    "load_sector_briefs",
    "load_universal_regime_label",
    "stage_artifacts_dir",
]


# ---------------------------------------------------------------------------
# Directory + filename contract
# ---------------------------------------------------------------------------

STAGE_ARTIFACTS_SUBDIR = "stage_artifacts"
"""Subdirectory name under each invocation's archive root."""

DISTILLATION_OUTPUTS_FILENAME = "distillation_outputs.json"
CORRELATION_REGIME_BRIEF_FILENAME = "correlation_regime_brief.json"
UNIVERSAL_REGIME_LABEL_FILENAME = "universal_regime_label.json"
SECTOR_BRIEFS_FILENAME = "sector_briefs.json"
QUALITATIVE_BRIEF_FILENAME = "qualitative_brief.json"
ADAPTIVE_BRIEF_FILENAME = "adaptive_brief.json"
RETRIEVAL_STORE_FILENAME = "retrieval_store.json"


def stage_artifacts_dir(archive_root: Path, invocation_id: str, as_of: datetime) -> Path:
    """Return ``<archive_root>/<YYYY-MM-DD>/<invocation_id>/stage_artifacts/``.

    Date-partitioned canonical layout per ALP-689 followup. Mirrors the layout
    under which the analysis-layer agent harnesses write per-invocation
    diagnostics, so the stage-artifact directory sits as a sibling of those
    per-agent archives.
    """
    return (
        invocation_archive_dir(archive_root=archive_root, as_of=as_of, invocation_id=invocation_id)
        / STAGE_ARTIFACTS_SUBDIR
    )


# ---------------------------------------------------------------------------
# Missing-artifact error
# ---------------------------------------------------------------------------


class MissingArtifactError(FileNotFoundError):
    """Raised when a required stage artifact is absent.

    Carries the producer-script name so the consumer can render
    "run scripts/verify_<producer>.py first" without the operator having to
    map filenames to their producers.
    """

    def __init__(self, *, missing_path: Path, producer_script: str) -> None:
        message = (
            f"required stage artifact {missing_path} is missing — "
            f"run scripts/{producer_script} first to produce it"
        )
        super().__init__(message)
        self.missing_path = missing_path
        self.producer_script = producer_script


def _require_path(path: Path, *, producer_script: str) -> Path:
    """Return *path* or raise :class:`MissingArtifactError` if absent."""
    if not path.exists():
        raise MissingArtifactError(missing_path=path, producer_script=producer_script)
    return path


# ---------------------------------------------------------------------------
# Datetime + enum helpers
# ---------------------------------------------------------------------------


def _isoformat(value: datetime) -> str:
    """Render a tz-aware datetime as an ISO-8601 string."""
    return value.isoformat()


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; accept ``Z`` or explicit offset."""
    return datetime.fromisoformat(value)


def _write_json(path: Path, payload: Any) -> None:
    """Write a JSON file with deterministic formatting.

    ``sort_keys=True`` + fixed indent so the on-disk artifact diffs
    cleanly across repeated producer runs that emit equivalent
    payloads. The write is not atomic — single-writer per invocation
    means torn writes are not a concern in operator workflows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, default=_json_default)
        fh.write("\n")


def _read_json(path: Path) -> Any:
    """Read and parse a JSON file."""
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _json_default(value: Any) -> Any:
    """JSON encoder fallback for datetimes (the only non-native type we hit).

    Enum values come through as plain strings via the explicit dict-builders
    below, so the encoder only sees raw datetimes once we've already routed
    every other custom type. ``raise TypeError`` keeps the encoder strict so
    an unexpected payload value (e.g. a NumPy float that a future producer
    forgets to coerce) is loud at write time rather than silently mangled.
    """
    if isinstance(value, datetime):
        return _isoformat(value)
    raise TypeError(f"unserializable value of type {type(value).__name__}: {value!r}")


# ---------------------------------------------------------------------------
# AnomalyFlag / OutputBlock / SectorOutput dict ↔ object helpers
# ---------------------------------------------------------------------------


def _anomaly_flag_to_dict(flag: AnomalyFlag) -> dict[str, Any]:
    return {
        "name": flag.name,
        "magnitude": flag.magnitude,
        "severity": flag.severity,
    }


def _anomaly_flag_from_dict(payload: dict[str, Any]) -> AnomalyFlag:
    return AnomalyFlag(
        name=payload["name"],
        magnitude=float(payload["magnitude"]),
        severity=payload["severity"],
    )


def _output_block_to_dict(block: OutputBlock) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "audience": sorted(audience.value for audience in block.audience),
        "freshness_ts": _isoformat(block.freshness_ts),
        "calibration_state": block.calibration_state.value,
        "bootstrap_reason": block.bootstrap_reason,
        "payload": dict(block.payload),
        "anomaly_flags": [_anomaly_flag_to_dict(flag) for flag in block.anomaly_flags],
        "regime_context": block.regime_context,
    }


def _output_block_from_dict(payload: dict[str, Any]) -> OutputBlock:
    return OutputBlock(
        block_id=payload["block_id"],
        audience=frozenset(OutputAudience(value) for value in payload["audience"]),
        freshness_ts=_parse_dt(payload["freshness_ts"]),
        calibration_state=CalibrationState(payload["calibration_state"]),
        bootstrap_reason=payload["bootstrap_reason"],
        payload=payload["payload"],
        anomaly_flags=tuple(_anomaly_flag_from_dict(flag) for flag in payload["anomaly_flags"]),
        regime_context=payload["regime_context"],
    )


def _sector_output_to_dict(output: SectorOutput) -> dict[str, Any]:
    return {
        "audience": output.audience.value,
        "sector_label": output.sector_label,
        "text": output.text,
        "tickers": list(output.tickers),
        "block_ids": list(output.block_ids),
        "freshness_min": _isoformat(output.freshness_min),
    }


def _sector_output_from_dict(payload: dict[str, Any]) -> SectorOutput:
    return SectorOutput(
        audience=OutputAudience(payload["audience"]),
        sector_label=payload["sector_label"],
        text=payload["text"],
        tickers=tuple(payload["tickers"]),
        block_ids=tuple(payload["block_ids"]),
        freshness_min=_parse_dt(payload["freshness_min"]),
    )


# ---------------------------------------------------------------------------
# CorrelationRegimeBrief
# ---------------------------------------------------------------------------


def _correlation_regime_brief_to_dict(brief: CorrelationRegimeBrief) -> dict[str, Any]:
    return {
        "text": brief.text,
        "reference_index": dict(brief.reference_index),
        "freshness_min": _isoformat(brief.freshness_min),
    }


def _correlation_regime_brief_from_dict(payload: dict[str, Any]) -> CorrelationRegimeBrief:
    return CorrelationRegimeBrief(
        text=payload["text"],
        reference_index=dict(payload["reference_index"]),
        freshness_min=_parse_dt(payload["freshness_min"]),
    )


def dump_correlation_regime_brief(brief: CorrelationRegimeBrief, stage_dir: Path) -> Path:
    """Write *brief* to ``correlation_regime_brief.json`` under *stage_dir*."""
    path = stage_dir / CORRELATION_REGIME_BRIEF_FILENAME
    _write_json(path, _correlation_regime_brief_to_dict(brief))
    return path


def load_correlation_regime_brief(stage_dir: Path) -> CorrelationRegimeBrief:
    """Read ``correlation_regime_brief.json`` from *stage_dir*."""
    path = _require_path(
        stage_dir / CORRELATION_REGIME_BRIEF_FILENAME,
        producer_script="verify_distillation.py",
    )
    return _correlation_regime_brief_from_dict(_read_json(path))


# ---------------------------------------------------------------------------
# Universal regime label
# ---------------------------------------------------------------------------


def dump_universal_regime_label(label: dict[str, Any], stage_dir: Path) -> Path:
    """Write *label* to ``universal_regime_label.json`` under *stage_dir*.

    The label is already a plain ``dict[str, Any]`` so no transformation is
    required beyond a JSON write.
    """
    path = stage_dir / UNIVERSAL_REGIME_LABEL_FILENAME
    _write_json(path, dict(label))
    return path


def load_universal_regime_label(stage_dir: Path) -> dict[str, Any]:
    """Read ``universal_regime_label.json`` from *stage_dir*."""
    path = _require_path(
        stage_dir / UNIVERSAL_REGIME_LABEL_FILENAME,
        producer_script="verify_distillation.py",
    )
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise TypeError(
            f"universal regime label at {path} is not a JSON object (got {type(payload).__name__})"
        )
    return payload


# ---------------------------------------------------------------------------
# DistillationOutputs
# ---------------------------------------------------------------------------


def _distillation_outputs_to_dict(outputs: DistillationOutputs) -> dict[str, Any]:
    return {
        "invocation_id": outputs.invocation_id,
        "as_of": _isoformat(outputs.as_of),
        "total_blocks": outputs.total_blocks,
        "total_anomalies": outputs.total_anomalies,
        "non_calibrated_block_count": outputs.non_calibrated_block_count,
        "all_blocks": [_output_block_to_dict(block) for block in outputs.all_blocks],
        "sector_outputs": [
            _sector_output_to_dict(output) for output in outputs.sector_outputs.values()
        ],
        "correlation_regime_brief": _correlation_regime_brief_to_dict(
            outputs.correlation_regime_brief
        ),
        "universal_regime_label": dict(outputs.universal_regime_label),
    }


def _distillation_outputs_from_dict(payload: dict[str, Any]) -> DistillationOutputs:
    sector_outputs = {
        OutputAudience(entry["audience"]): _sector_output_from_dict(entry)
        for entry in payload["sector_outputs"]
    }
    return DistillationOutputs(
        sector_outputs=sector_outputs,
        correlation_regime_brief=_correlation_regime_brief_from_dict(
            payload["correlation_regime_brief"]
        ),
        universal_regime_label=dict(payload["universal_regime_label"]),
        invocation_id=payload["invocation_id"],
        as_of=_parse_dt(payload["as_of"]),
        total_blocks=int(payload["total_blocks"]),
        total_anomalies=int(payload["total_anomalies"]),
        non_calibrated_block_count=int(payload["non_calibrated_block_count"]),
        all_blocks=tuple(_output_block_from_dict(block) for block in payload["all_blocks"]),
    )


def dump_distillation_outputs(outputs: DistillationOutputs, stage_dir: Path) -> Path:
    """Write *outputs* to ``distillation_outputs.json`` under *stage_dir*.

    The serialization is self-contained — the embedded brief and label
    round-trip without needing the sibling files. The sibling files exist
    so downstream scripts that only need a slice (e.g., the qualitative
    researcher only needs the regime label) can load it cheaply.
    """
    path = stage_dir / DISTILLATION_OUTPUTS_FILENAME
    _write_json(path, _distillation_outputs_to_dict(outputs))
    return path


def load_distillation_outputs(stage_dir: Path) -> DistillationOutputs:
    """Read ``distillation_outputs.json`` from *stage_dir*."""
    path = _require_path(
        stage_dir / DISTILLATION_OUTPUTS_FILENAME,
        producer_script="verify_distillation.py",
    )
    return _distillation_outputs_from_dict(_read_json(path))


# ---------------------------------------------------------------------------
# Pydantic-model briefs
# ---------------------------------------------------------------------------


def dump_sector_briefs(briefs: tuple[SectorBrief, ...], stage_dir: Path) -> Path:
    """Write *briefs* to ``sector_briefs.json`` under *stage_dir*.

    The file is a JSON array — one object per brief — so the loader can
    rebuild the tuple in the producer's order.
    """
    path = stage_dir / SECTOR_BRIEFS_FILENAME
    payload = [json.loads(brief.model_dump_json()) for brief in briefs]
    _write_json(path, payload)
    return path


def load_sector_briefs(stage_dir: Path) -> tuple[SectorBrief, ...]:
    """Read ``sector_briefs.json`` from *stage_dir*."""
    path = _require_path(
        stage_dir / SECTOR_BRIEFS_FILENAME,
        producer_script="verify_domain_researchers.py",
    )
    payload = _read_json(path)
    return tuple(SectorBrief.model_validate(entry) for entry in payload)


def dump_qualitative_brief(brief: QualitativeBrief, stage_dir: Path) -> Path:
    """Write *brief* to ``qualitative_brief.json`` under *stage_dir*."""
    path = stage_dir / QUALITATIVE_BRIEF_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_qualitative_brief(stage_dir: Path) -> QualitativeBrief:
    """Read ``qualitative_brief.json`` from *stage_dir*."""
    path = _require_path(
        stage_dir / QUALITATIVE_BRIEF_FILENAME,
        producer_script="verify_qualitative_researcher.py",
    )
    return QualitativeBrief.model_validate_json(path.read_text(encoding="utf-8"))


def dump_adaptive_brief(brief: AdaptiveBrief, stage_dir: Path) -> Path:
    """Write *brief* to ``adaptive_brief.json`` under *stage_dir*."""
    path = stage_dir / ADAPTIVE_BRIEF_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_adaptive_brief(stage_dir: Path) -> AdaptiveBrief:
    """Read ``adaptive_brief.json`` from *stage_dir*."""
    path = _require_path(
        stage_dir / ADAPTIVE_BRIEF_FILENAME,
        producer_script="verify_adaptive_researcher.py",
    )
    return AdaptiveBrief.model_validate_json(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# RetrievalStore
# ---------------------------------------------------------------------------


def dump_retrieval_store(store: RetrievalStore, stage_dir: Path) -> Path:
    """Write *store* to ``retrieval_store.json`` under *stage_dir*.

    ``RetrievalStore`` is now a frozen dataclass (ALP-474); the JSON
    encoder explicitly handles ``BriefSource`` keys and ``datetime``
    values in ``freshness_by_source``.
    """
    path = stage_dir / RETRIEVAL_STORE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "entries": store.entries,
        "freshness_by_source": {
            source.value: ts.isoformat() for source, ts in store.freshness_by_source.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_retrieval_store(stage_dir: Path) -> RetrievalStore:
    """Read ``retrieval_store.json`` from *stage_dir*."""
    path = _require_path(
        stage_dir / RETRIEVAL_STORE_FILENAME,
        producer_script="verify_synthesizer.py",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RetrievalStore(
        entries=dict(payload["entries"]),
        freshness_by_source={
            BriefSource(source): datetime.fromisoformat(ts)
            for source, ts in payload["freshness_by_source"].items()
        },
    )
