"""QualitativeBrief data model — ALP-242.

Typed in-memory representation of the qualitative-researcher agent's output.
See docs/design/03-analysis-layer/qualitative-research.md § Output § Output schema.

Public names
------------
- :class:`ThreadDirection` — bullish/bearish/mixed/uncertain
- :class:`TimeHorizon` — immediate/near_term/developing
- :data:`TIME_HORIZON_DISPLAY` — parser-facing display string mapping
- :class:`EvidenceLine` — single evidence entry within a narrative thread
- :class:`NarrativeThread` — coherent story drawing from multiple sources
- :class:`CatalystWatch` — per-thesis upcoming catalyst entry
- :class:`SentimentSnapshot` — three-field sentiment footer
- :class:`QualitativeBrief` — complete qualitative researcher output for one invocation
- :data:`SignalQuality` — re-exported from :mod:`alphamind.analysis._shared`
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field, model_validator

from alphamind.analysis._shared import SignalQuality

__all__ = [
    "REQUIRED_BY_SIGNAL_QUALITY",
    "TIME_HORIZON_DISPLAY",
    "CatalystWatch",
    "EvidenceLine",
    "NarrativeThread",
    "QualitativeBrief",
    "SentimentSnapshot",
    "SignalQuality",
    "ThreadDirection",
    "TimeHorizon",
]


# ---------------------------------------------------------------------------
# Per-signal-quality required-conditional-field map (ALP-288)
# ---------------------------------------------------------------------------

#: Fields required by each :class:`SignalQuality` value on
#: :class:`QualitativeBrief`. Mirrors the per-Assessment map in
#: ``adaptive_research.models``: shape consumed by
#: :func:`alphamind.analysis._schema_tightening._tighten_conditional_schema`
#: so the API rejects payloads that emit ``null`` for
#: ``signal_quality_reason`` on the DEGRADED branch.
REQUIRED_BY_SIGNAL_QUALITY: dict[SignalQuality, frozenset[str]] = {}  # populated below


# ---------------------------------------------------------------------------
# Closed-set enums local to the brief
# ---------------------------------------------------------------------------


class ThreadDirection(enum.StrEnum):
    """Directional stance of a narrative thread."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


class TimeHorizon(enum.StrEnum):
    """Time horizon over which a narrative thread is expected to play out."""

    IMMEDIATE = "immediate"
    NEAR_TERM = "near_term"
    DEVELOPING = "developing"


#: Display-string mapping for parser/validator use.
#: Keys are :class:`TimeHorizon` members; values include the parenthetical
#: suffix the parser recognises (``(<24h)``, ``(24-72h)``, ``(>72h)``).
TIME_HORIZON_DISPLAY: dict[TimeHorizon, str] = {
    TimeHorizon.IMMEDIATE: "immediate (<24h)",
    TimeHorizon.NEAR_TERM: "near-term (24-72h)",
    TimeHorizon.DEVELOPING: "developing (>72h)",
}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class EvidenceLine(BaseModel, frozen=True):
    """A single evidence entry within a :class:`NarrativeThread`.

    All three fields are required and non-empty.
    """

    source_type: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    citation: str = Field(min_length=1)


class NarrativeThread(BaseModel, frozen=True):
    """A coherent narrative story drawing from multiple input sources.

    ``thread_id`` follows the format ``QR-{N}`` where N is a positive integer.
    The design doc requires at least two distinct input sources per thread
    (``len(evidence) >= 2``).
    """

    thread_id: str = Field(pattern=r"^QR-\d+$")
    summary: str = Field(min_length=1)
    relevance: str = Field(min_length=1)
    direction: ThreadDirection
    subject: str = Field(min_length=1)
    time_horizon: TimeHorizon
    evidence: tuple[EvidenceLine, ...]
    implication: str = Field(min_length=1)

    @model_validator(mode="after")
    def _minimum_two_sources(self) -> NarrativeThread:
        if len(self.evidence) < 2:
            raise ValueError(
                "narrative thread must draw from at least two input sources; "
                f"got {len(self.evidence)}"
            )
        return self


class CatalystWatch(BaseModel, frozen=True):
    """A single catalyst-watch entry for a specific ticker and active thesis.

    ``catalyst_id`` follows the format ``QR-CW-{N}`` where N is a positive integer.
    Ticker-universe membership validation belongs in story 03b's validator, not here.
    """

    catalyst_id: str = Field(pattern=r"^QR-CW-\d+$")
    ticker: str = Field(min_length=1)
    catalyst_name: str
    hours_to_event: int = Field(ge=0)
    thesis_impact: str


class SentimentSnapshot(BaseModel, frozen=True):
    """Fixed-size three-field sentiment footer for the qualitative brief.

    All three fields are required and non-empty.
    The design doc specifies ``"none"`` as the valid quiet-day value, not absence.
    """

    extremes: str = Field(min_length=1)
    divergences: str = Field(min_length=1)
    regime: str = Field(min_length=1)


class QualitativeBrief(BaseModel, frozen=True):
    """Complete output of the qualitative researcher for one invocation.

    Invariants enforced here:

    * ``signal_quality_reason`` is required when ``signal_quality`` is
      :attr:`SignalQuality.DEGRADED` and must be ``None`` otherwise.
    * ``len(threads) >= 1`` — the narrative-threads section always carries
      at least one entry, including a "nothing is happening" thread on quiet days.
    """

    invocation_id: str
    signal_quality: SignalQuality
    signal_quality_reason: str | None
    threads: tuple[NarrativeThread, ...]
    catalyst_watches: tuple[CatalystWatch, ...]
    sentiment_snapshot: SentimentSnapshot

    @model_validator(mode="after")
    def _brief_invariants(self) -> QualitativeBrief:
        # signal_quality_reason invariant (mirrors SectorBrief)
        if self.signal_quality == SignalQuality.DEGRADED and self.signal_quality_reason is None:
            raise ValueError("signal_quality_reason required when signal_quality is DEGRADED")
        if self.signal_quality != SignalQuality.DEGRADED and self.signal_quality_reason is not None:
            raise ValueError(
                "signal_quality_reason must be None when signal_quality is not DEGRADED"
            )
        # at-least-one-thread invariant
        if not self.threads:
            raise ValueError(
                "QualitativeBrief must contain at least one narrative thread; "
                "use a 'nothing is happening' thread on quiet days"
            )
        return self


# Populate after class definitions so the dict literal can name SignalQuality.
REQUIRED_BY_SIGNAL_QUALITY.update(
    {
        SignalQuality.HIGH: frozenset(),
        SignalQuality.MODERATE: frozenset(),
        SignalQuality.LOW: frozenset(),
        SignalQuality.DEGRADED: frozenset({"signal_quality_reason"}),
    }
)
