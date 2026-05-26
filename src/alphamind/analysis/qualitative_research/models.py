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
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alphamind.analysis._shared import SignalQuality, TokensUsed

if TYPE_CHECKING:
    from alphamind.analysis.qualitative_research.news_digest import NewsDigest

__all__ = [
    "TIME_HORIZON_DISPLAY",
    "CatalystWatch",
    "EvidenceLine",
    "NarrativeThread",
    "QualitativeBrief",
    "QualitativeResearcherResultModel",
    "SentimentSnapshot",
    "SignalQuality",
    "ThreadDirection",
    "TimeHorizon",
]


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


# ---------------------------------------------------------------------------
# Phase-output boundary model — story ALP-691
# ---------------------------------------------------------------------------


class _QualInputBundleModel(BaseModel, frozen=True):
    """Pydantic mirror of the qualitative-researcher :class:`InputBundle` dataclass."""

    invocation_id: str
    as_of: datetime
    regime_text: str
    digest_text: str
    sentiment_text: str
    prediction_market_text: str
    calendar_text: str
    thesis_text: str
    bundle_text: str

    @classmethod
    def _from_domain(cls, dc: Any) -> _QualInputBundleModel:
        return cls(
            invocation_id=dc.invocation_id,
            as_of=dc.as_of,
            regime_text=dc.regime_text,
            digest_text=dc.digest_text,
            sentiment_text=dc.sentiment_text,
            prediction_market_text=dc.prediction_market_text,
            calendar_text=dc.calendar_text,
            thesis_text=dc.thesis_text,
            bundle_text=dc.bundle_text,
        )

    def _to_domain(self) -> Any:
        from alphamind.analysis.qualitative_research.input_bundle import InputBundle

        return InputBundle(
            invocation_id=self.invocation_id,
            as_of=self.as_of,
            regime_text=self.regime_text,
            digest_text=self.digest_text,
            sentiment_text=self.sentiment_text,
            prediction_market_text=self.prediction_market_text,
            calendar_text=self.calendar_text,
            thesis_text=self.thesis_text,
            bundle_text=self.bundle_text,
        )


class QualitativeResearcherResultModel(BaseModel, frozen=True):
    """Frozen Pydantic boundary model for the qualitative researcher result.

    Used by the debug-e2e phase-output persistence layer (story ALP-691).
    ``from_domain`` / ``to_domain`` provide lossless round-trip.
    """

    model_config = ConfigDict(frozen=True)

    brief: QualitativeBrief
    input_bundle: _QualInputBundleModel
    # NewsDigest is a frozen Pydantic BaseModel imported lazily to avoid
    # pulling the SQLAlchemy-heavy news_digest module at models.py load time.
    # The type annotation uses a string so Pydantic resolves it on first use.
    news_digest: NewsDigest  # resolved lazily via _rebuild_qualitative_models()
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    retry_count: int

    @classmethod
    def from_domain(cls, dc: object) -> QualitativeResearcherResultModel:
        """Project a :class:`QualitativeResearcherResult` onto this model."""
        return cls(
            brief=dc.brief,  # type: ignore[attr-defined]
            input_bundle=_QualInputBundleModel._from_domain(dc.input_bundle),  # type: ignore[attr-defined]
            news_digest=dc.news_digest,  # type: ignore[attr-defined]
            tokens_used=dc.tokens_used,  # type: ignore[attr-defined]
            tool_calls_used=dc.tool_calls_used,  # type: ignore[attr-defined]
            wall_clock_seconds=dc.wall_clock_seconds,  # type: ignore[attr-defined]
            retry_count=dc.retry_count,  # type: ignore[attr-defined]
        )

    def to_domain(self) -> object:
        """Recover the original :class:`QualitativeResearcherResult`."""
        from alphamind.analysis.qualitative_research.runner import QualitativeResearcherResult

        return QualitativeResearcherResult(
            brief=self.brief,
            input_bundle=self.input_bundle._to_domain(),
            news_digest=self.news_digest,
            tokens_used=self.tokens_used,
            tool_calls_used=self.tool_calls_used,
            wall_clock_seconds=self.wall_clock_seconds,
            retry_count=self.retry_count,
        )


# Resolve forward references so Pydantic can serialize NewsDigest correctly.
# This must run after NewsDigest is importable at runtime.
def _rebuild_qualitative_models() -> None:
    from alphamind.analysis.qualitative_research.news_digest import NewsDigest  # noqa: F401

    QualitativeResearcherResultModel.model_rebuild()


_rebuild_qualitative_models()
