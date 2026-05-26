"""AdaptiveBrief data model — ALP-255.

Typed in-memory representation of the adaptive-researcher agent's output.
See ``docs/design/03-analysis-layer/adaptive-research.md`` § Output § Output schema
and ``prompts/analysis/adaptive_researcher.md`` § ``<output_contract>``.

Public names
------------
- :class:`Assessment` — three-way verdict (signal/noise/inconclusive) per thread
- :class:`Confidence` — agent's confidence in its own assessment
- :class:`InvestigationThread` — a single investigation thread within a brief
- :class:`AdaptiveBrief` — complete adaptive-researcher output for one invocation

Conditional-field discipline
----------------------------
Each :class:`InvestigationThread` carries five mutually-exclusive optional fields
keyed off :attr:`InvestigationThread.assessment`:

- :attr:`Assessment.SIGNAL`       — ``implication``, ``strengthens``, ``weakens`` REQUIRED;
                                    ``dismissal_reason``, ``missing`` MUST be ``None``.
- :attr:`Assessment.NOISE`        — ``dismissal_reason`` REQUIRED; the other four MUST be ``None``.
- :attr:`Assessment.INCONCLUSIVE` — ``missing`` REQUIRED; the other four MUST be ``None``.

The ``strengthens``/``weakens`` fields are tuples (not strings) so the Layer-3
validator can iterate them for referential resolution. The wire literal ``none``
parses to an empty tuple ``()``, NOT to ``None`` — ``None`` means "not applicable
to this assessment", ``()`` means "applicable, explicitly empty".
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alphamind.analysis._shared import AnomalySeverity, Sector, TokensUsed

__all__ = [
    "REQUIRED_BY_ASSESSMENT",
    "AdaptiveBrief",
    "AdaptiveResearcherResultModel",
    "Assessment",
    "Confidence",
    "InvestigationThread",
]


# ---------------------------------------------------------------------------
# Closed-set enums local to the brief
# ---------------------------------------------------------------------------


class Assessment(enum.StrEnum):
    """Three-way verdict on an investigation thread."""

    SIGNAL = "signal"
    NOISE = "noise"
    INCONCLUSIVE = "inconclusive"


class Confidence(enum.StrEnum):
    """Agent's confidence in its own assessment."""

    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"


# ---------------------------------------------------------------------------
# Conditional-field invariant table
# ---------------------------------------------------------------------------

#: Fields required by each :class:`Assessment` value. The set of all conditional
#: fields is :data:`_CONDITIONAL_FIELDS`; for a given assessment, fields not in
#: its required set must be ``None``. Public so the harness can pass it to
#: :func:`alphamind.analysis._schema_tightening._tighten_conditional_schema`.
REQUIRED_BY_ASSESSMENT: dict[Assessment, frozenset[str]] = {
    Assessment.SIGNAL: frozenset({"implication", "strengthens", "weakens"}),
    Assessment.NOISE: frozenset({"dismissal_reason"}),
    Assessment.INCONCLUSIVE: frozenset({"missing"}),
}

_CONDITIONAL_FIELDS: frozenset[str] = frozenset().union(*REQUIRED_BY_ASSESSMENT.values())


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class InvestigationThread(BaseModel, frozen=True):
    """A single investigation thread within an :class:`AdaptiveBrief`.

    ``thread_id`` follows ``AR-{N}`` where N is a positive integer.
    Conditional-field discipline is enforced by :meth:`_assessment_invariant`
    (see module docstring).
    """

    thread_id: str = Field(pattern=r"^AR-\d+$")
    trigger: str = Field(min_length=1)
    question: str = Field(min_length=1)
    tickers: tuple[str, ...]
    sector: Sector
    tools_used: tuple[str, ...]
    findings: tuple[str, ...]
    assessment: Assessment
    confidence: Confidence

    # Conditional fields — exactly one set is populated per assessment.
    implication: str | None = None
    strengthens: tuple[str, ...] | None = None
    weakens: tuple[str, ...] | None = None
    dismissal_reason: str | None = None
    missing: str | None = None

    @model_validator(mode="after")
    def _assessment_invariant(self) -> InvestigationThread:
        required = REQUIRED_BY_ASSESSMENT[self.assessment]
        for name in required:
            if getattr(self, name) is None:
                raise ValueError(f"{self.assessment.value} assessment requires {name} to be set")
        for name in _CONDITIONAL_FIELDS - required:
            if getattr(self, name) is not None:
                raise ValueError(
                    f"{self.assessment.value} assessment forbids {name}; got non-None value"
                )
        return self


class AdaptiveBrief(BaseModel, frozen=True):
    """Complete output of the adaptive researcher for one invocation.

    Invariants:

    * ``threads_investigated_count`` equals ``len(threads)``.
    * ``anomalies_triaged_count >= threads_investigated_count``.
    * ``anomalies_deferred`` is a (possibly empty) tuple of strings; the wire
      literal ``"none"`` parses to ``()``.
    * Empty ``threads`` is valid (quiet cycle) — the design doc explicitly names
      zero-thread output as correct.
    """

    invocation_id: str
    threads_investigated_count: int = Field(ge=0)
    anomalies_triaged_count: int = Field(ge=0)
    anomalies_deferred: tuple[str, ...]
    threads: tuple[InvestigationThread, ...]

    @model_validator(mode="after")
    def _brief_invariants(self) -> AdaptiveBrief:
        if self.threads_investigated_count != len(self.threads):
            raise ValueError(
                f"threads_investigated_count ({self.threads_investigated_count}) "
                f"must equal len(threads) ({len(self.threads)})"
            )
        if self.anomalies_triaged_count < self.threads_investigated_count:
            raise ValueError(
                f"anomalies_triaged_count ({self.anomalies_triaged_count}) "
                f"must be >= threads_investigated_count ({self.threads_investigated_count})"
            )
        return self


# ---------------------------------------------------------------------------
# Phase-output boundary models — story ALP-691
# ---------------------------------------------------------------------------


class _DistillationAnomalyRecordModel(BaseModel, frozen=True):
    """Pydantic mirror of the :class:`DistillationAnomalyRecord` loader dataclass."""

    block_id: str
    flag_name: str
    magnitude: float
    severity: AnomalySeverity
    regime_context: str | None
    freshness_ts: datetime

    @classmethod
    def _from_domain(cls, dc: object) -> _DistillationAnomalyRecordModel:
        return cls(
            block_id=dc.block_id,  # type: ignore[attr-defined]
            flag_name=dc.flag_name,  # type: ignore[attr-defined]
            magnitude=dc.magnitude,  # type: ignore[attr-defined]
            severity=dc.severity,  # type: ignore[attr-defined]
            regime_context=dc.regime_context,  # type: ignore[attr-defined]
            freshness_ts=dc.freshness_ts,  # type: ignore[attr-defined]
        )

    def _to_domain(self) -> object:
        import importlib

        _loaders = importlib.import_module("alphamind.analysis.adaptive_research.loaders")
        DistillationAnomalyRecord = _loaders.DistillationAnomalyRecord  # noqa: N806

        return DistillationAnomalyRecord(
            block_id=self.block_id,
            flag_name=self.flag_name,
            magnitude=self.magnitude,
            severity=self.severity,
            regime_context=self.regime_context,
            freshness_ts=self.freshness_ts,
        )


class _SectorAnomalyRecordModel(BaseModel, frozen=True):
    """Pydantic mirror of the :class:`SectorAnomalyRecord` loader dataclass."""

    anomaly_id: str
    description: str
    anomaly_type: str
    tickers: tuple[str, ...]
    severity: AnomalySeverity
    suggested_question: str
    sector: Sector

    @classmethod
    def _from_domain(cls, dc: object) -> _SectorAnomalyRecordModel:
        return cls(
            anomaly_id=dc.anomaly_id,  # type: ignore[attr-defined]
            description=dc.description,  # type: ignore[attr-defined]
            anomaly_type=dc.anomaly_type,  # type: ignore[attr-defined]
            tickers=dc.tickers,  # type: ignore[attr-defined]
            severity=dc.severity,  # type: ignore[attr-defined]
            suggested_question=dc.suggested_question,  # type: ignore[attr-defined]
            sector=dc.sector,  # type: ignore[attr-defined]
        )

    def _to_domain(self) -> object:
        import importlib

        _loaders = importlib.import_module("alphamind.analysis.adaptive_research.loaders")
        SectorAnomalyRecord = _loaders.SectorAnomalyRecord  # noqa: N806

        return SectorAnomalyRecord(
            anomaly_id=self.anomaly_id,
            description=self.description,
            anomaly_type=self.anomaly_type,
            tickers=self.tickers,
            severity=self.severity,
            suggested_question=self.suggested_question,
            sector=self.sector,
        )


class _AdaptiveAnomalyInputsModel(BaseModel, frozen=True):
    """Pydantic mirror of the :class:`AdaptiveAnomalyInputs` loader dataclass."""

    distillation: tuple[_DistillationAnomalyRecordModel, ...]
    sector: tuple[_SectorAnomalyRecordModel, ...]
    data_freshness: datetime

    @classmethod
    def _from_domain(cls, dc: Any) -> _AdaptiveAnomalyInputsModel:
        return cls(
            distillation=tuple(
                _DistillationAnomalyRecordModel._from_domain(r) for r in dc.distillation
            ),
            sector=tuple(_SectorAnomalyRecordModel._from_domain(r) for r in dc.sector),
            data_freshness=dc.data_freshness,
        )

    def _to_domain(self) -> Any:
        import importlib

        _loaders = importlib.import_module("alphamind.analysis.adaptive_research.loaders")
        AdaptiveAnomalyInputs = _loaders.AdaptiveAnomalyInputs  # noqa: N806

        return AdaptiveAnomalyInputs(
            distillation=tuple(r._to_domain() for r in self.distillation),
            sector=tuple(r._to_domain() for r in self.sector),
            data_freshness=self.data_freshness,
        )


class _AdaptiveInputBundleModel(BaseModel, frozen=True):
    """Pydantic mirror of the adaptive-researcher :class:`InputBundle` dataclass."""

    invocation_id: str
    as_of: datetime
    regime_text: str
    distillation_text: str
    sector_text: str
    bundle_text: str

    @classmethod
    def _from_domain(cls, dc: Any) -> _AdaptiveInputBundleModel:
        return cls(
            invocation_id=dc.invocation_id,
            as_of=dc.as_of,
            regime_text=dc.regime_text,
            distillation_text=dc.distillation_text,
            sector_text=dc.sector_text,
            bundle_text=dc.bundle_text,
        )

    def _to_domain(self) -> Any:
        import importlib

        _input_bundle = importlib.import_module("alphamind.analysis.adaptive_research.input_bundle")
        InputBundle = _input_bundle.InputBundle  # noqa: N806

        return InputBundle(
            invocation_id=self.invocation_id,
            as_of=self.as_of,
            regime_text=self.regime_text,
            distillation_text=self.distillation_text,
            sector_text=self.sector_text,
            bundle_text=self.bundle_text,
        )


class AdaptiveResearcherResultModel(BaseModel, frozen=True):
    """Frozen Pydantic boundary model for the adaptive researcher result.

    Used by the debug-e2e phase-output persistence layer (story ALP-691).
    ``from_domain`` / ``to_domain`` provide lossless round-trip.
    """

    model_config = ConfigDict(frozen=True)

    brief: AdaptiveBrief
    input_bundle: _AdaptiveInputBundleModel
    anomaly_inputs: _AdaptiveAnomalyInputsModel
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    retry_count: int

    @classmethod
    def from_domain(cls, dc: object) -> AdaptiveResearcherResultModel:
        """Project a :class:`AdaptiveResearcherResult` onto this model."""
        return cls(
            brief=dc.brief,  # type: ignore[attr-defined]
            input_bundle=_AdaptiveInputBundleModel._from_domain(dc.input_bundle),  # type: ignore[attr-defined]
            anomaly_inputs=_AdaptiveAnomalyInputsModel._from_domain(dc.anomaly_inputs),  # type: ignore[attr-defined]
            tokens_used=dc.tokens_used,  # type: ignore[attr-defined]
            tool_calls_used=dc.tool_calls_used,  # type: ignore[attr-defined]
            wall_clock_seconds=dc.wall_clock_seconds,  # type: ignore[attr-defined]
            retry_count=dc.retry_count,  # type: ignore[attr-defined]
        )

    def to_domain(self) -> Any:
        """Recover the original :class:`AdaptiveResearcherResult`."""
        import importlib

        _runner = importlib.import_module("alphamind.analysis.adaptive_research.runner")
        AdaptiveResearcherResult = _runner.AdaptiveResearcherResult  # noqa: N806

        return AdaptiveResearcherResult(
            brief=self.brief,
            input_bundle=self.input_bundle._to_domain(),
            anomaly_inputs=self.anomaly_inputs._to_domain(),
            tokens_used=self.tokens_used,
            tool_calls_used=self.tool_calls_used,
            wall_clock_seconds=self.wall_clock_seconds,
            retry_count=self.retry_count,
        )
