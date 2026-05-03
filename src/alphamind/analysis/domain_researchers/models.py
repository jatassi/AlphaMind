"""Domain-researcher output data model — story 03 (ALP-187).

This module defines the typed in-memory representation of a domain researcher's
output: :class:`SectorBrief` and its constituent records (:class:`Finding`,
:class:`Anomaly`, :class:`ThesisCandidate`) plus the closed-set enums local to
the brief.

Distinction: :class:`Anomaly` here is a *brief-level record* carrying narrative,
suggested question, and severity — it is what the domain-researcher LLM produces
inside a :class:`SectorBrief`. This is distinct from
:class:`alphamind.distillation.output.AnomalyFlag`, which is an
*envelope-level flag* carrying name, magnitude, and severity that travels with
a distillation :class:`~alphamind.distillation.output.OutputBlock`. Both names
stand by design; they share the ``severity`` vocabulary via the shared
:data:`~alphamind.analysis._shared.AnomalySeverity` literal imported from
``alphamind.analysis._shared``.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field, model_validator

from alphamind.analysis._shared import AnomalySeverity, Sector, SignalQuality

__all__ = [
    "SECTOR_PREFIX",
    "Anomaly",
    "AnomalyType",
    "ConvictionSketch",
    "Direction",
    "Finding",
    "SectorBrief",
    "SetupType",
    "SignalQuality",
    "SignalType",
    "Strength",
    "ThesisCandidate",
]


# ---------------------------------------------------------------------------
# Closed-set enums local to the brief
# ---------------------------------------------------------------------------


class SignalType(enum.StrEnum):
    """Signal taxonomy from tech-semis.md § Signal type taxonomy."""

    PRICE_ACTION = "price_action"
    FLOW = "flow"
    OPTIONS = "options"
    FUNDAMENTAL = "fundamental"
    SENTIMENT = "sentiment"
    TECHNICAL = "technical"
    CROSS_ASSET = "cross_asset"


class Strength(enum.StrEnum):
    """Finding strength levels."""

    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


class AnomalyType(enum.StrEnum):
    """Anomaly type taxonomy."""

    VOLUME = "volume"
    PRICE_FLOW_DIVERGENCE = "price_flow_divergence"
    CORRELATION_BREAK = "correlation_break"
    OPTIONS_SKEW = "options_skew"
    OTHER = "other"


class Direction(enum.StrEnum):
    """Trade direction for a thesis candidate."""

    LONG = "long"
    SHORT = "short"


class SetupType(enum.StrEnum):
    """Setup type for a thesis candidate."""

    CATALYST = "catalyst"
    MEAN_REVERSION = "mean_reversion"
    MOMENTUM = "momentum"
    DIVERGENCE = "divergence"
    EVENT = "event"


class ConvictionSketch(enum.StrEnum):
    """Conviction level for a thesis candidate."""

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class Finding(BaseModel, frozen=True):
    """A single market finding from a domain researcher.

    ``finding_id`` follows the format ``SA-{SECTOR}-{N}`` where SECTOR is one
    of ``TECH``, ``FIN``, or ``ENERGY`` and N is a positive integer.
    """

    finding_id: str = Field(pattern=r"^SA-(TECH|FIN|ENERGY)-\d+$")
    headline: str
    tickers: tuple[str, ...]
    signal_type: SignalType
    strength: Strength
    detail: str

    @model_validator(mode="after")
    def _tickers_not_empty(self) -> Finding:
        if not self.tickers:
            raise ValueError("tickers must not be empty")
        return self


class Anomaly(BaseModel, frozen=True):
    """A single anomaly observation from a domain researcher.

    ``anomaly_id`` follows the format ``SA-{SECTOR}-ANOM-{N}``.
    See module docstring for the distinction from
    :class:`alphamind.distillation.output.AnomalyFlag`.
    """

    anomaly_id: str = Field(pattern=r"^SA-(TECH|FIN|ENERGY)-ANOM-\d+$")
    description: str
    anomaly_type: AnomalyType
    tickers: tuple[str, ...]
    severity: AnomalySeverity
    suggested_question: str


class ThesisCandidate(BaseModel, frozen=True):
    """A single thesis candidate from a domain researcher.

    ``thesis_candidate_id`` follows the format ``SA-{SECTOR}-TC-{N}``.
    """

    thesis_candidate_id: str = Field(pattern=r"^SA-(TECH|FIN|ENERGY)-TC-\d+$")
    ticker: str
    direction: Direction
    setup_type: SetupType
    catalyst: str
    time_horizon_hours: str
    conviction_sketch: ConvictionSketch
    conviction_justification: str
    key_risk: str

    @model_validator(mode="after")
    def _ticker_not_empty(self) -> ThesisCandidate:
        if not self.ticker:
            raise ValueError("ticker must not be empty")
        return self


class SectorBrief(BaseModel, frozen=True):
    """The complete output of a domain researcher for one invocation.

    ``signal_quality_reason`` is required when ``signal_quality`` is
    :attr:`SignalQuality.DEGRADED` and must be ``None`` otherwise.
    """

    invocation_id: str
    sector: Sector
    signal_quality: SignalQuality
    signal_quality_reason: str | None
    findings: tuple[Finding, ...]
    anomalies: tuple[Anomaly, ...]
    thesis_candidates: tuple[ThesisCandidate, ...]

    @model_validator(mode="after")
    def _signal_quality_reason_invariant(self) -> SectorBrief:
        if self.signal_quality == SignalQuality.DEGRADED and self.signal_quality_reason is None:
            raise ValueError("signal_quality_reason required when signal_quality is DEGRADED")
        if self.signal_quality != SignalQuality.DEGRADED and self.signal_quality_reason is not None:
            raise ValueError(
                "signal_quality_reason must be None when signal_quality is not DEGRADED"
            )
        return self


# ---------------------------------------------------------------------------
# Helper mapping: Sector → brief-format prefix string
# ---------------------------------------------------------------------------

SECTOR_PREFIX: dict[Sector, str] = {
    Sector.TECH_SEMIS: "SA-TECH",
    Sector.FINANCIALS: "SA-FIN",
    Sector.ENERGY: "SA-ENERGY",
}
