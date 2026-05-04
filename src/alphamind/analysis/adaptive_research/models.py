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

from pydantic import BaseModel, Field, model_validator

from alphamind.analysis._shared import Sector

__all__ = [
    "REQUIRED_BY_ASSESSMENT",
    "AdaptiveBrief",
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
