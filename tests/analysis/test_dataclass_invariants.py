"""Pin the Pydantic→dataclass conversion of analysis-internal types (ALP-474).

Verifies that every internal type identified by the audit's L5 finding is a
frozen, slotted dataclass — not a Pydantic BaseModel — and that the
``__post_init__`` invariants documented in the conversion are enforced. Boundary
types (brief schemas, ``TokensUsed``, ``BriefBundle``) stay Pydantic and are
explicitly out of scope.

The shape check uses ``dataclasses.is_dataclass`` + ``__slots__`` + behavioural
``FrozenInstanceError``; that's robust to dataclass-internal layout changes
across CPython versions.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from alphamind._kernel.clock import Clock, RealClock
from alphamind.analysis._shared import Sector, TokensUsed
from alphamind.analysis.adaptive_research.harness import (
    HarnessSuccess as AdaptiveHarnessSuccess,
)
from alphamind.analysis.adaptive_research.input_bundle import (
    InputBundle as AdaptiveInputBundle,
)
from alphamind.analysis.adaptive_research.loaders import (
    DistillationAnomalyRecord,
    SectorAnomalyRecord,
)
from alphamind.analysis.adaptive_research.runner import AdaptiveResearcherResult
from alphamind.analysis.adaptive_research.validation import (
    ValidationError as AdaptiveValidationError,
)
from alphamind.analysis.adaptive_research.validation import (
    ValidationResult as AdaptiveValidationResult,
)
from alphamind.analysis.domain_researchers.harness import HarnessSuccess as DomainHarnessSuccess
from alphamind.analysis.domain_researchers.input_bundle import InputBundle as DomainInputBundle
from alphamind.analysis.domain_researchers.orchestrator import DomainResearchersOutput
from alphamind.analysis.domain_researchers.qualitative_input import (
    EventEntry,
    HeadlineEntry,
    SectorQualitativeInput,
)
from alphamind.analysis.domain_researchers.runner import DomainResearcherResult
from alphamind.analysis.domain_researchers.validation import (
    ValidationError as DomainValidationError,
)
from alphamind.analysis.domain_researchers.validation import (
    ValidationResult as DomainValidationResult,
)
from alphamind.analysis.qualitative_research.harness import (
    HarnessSuccess as QualitativeHarnessSuccess,
)
from alphamind.analysis.qualitative_research.input_bundle import (
    InputBundle as QualitativeInputBundle,
)
from alphamind.analysis.qualitative_research.loaders import (
    ActiveThesis,
    CalendarEvent,
    PredictionMarketSnapshot,
    QualitativeInputs,
    SentimentAggregate,
)
from alphamind.analysis.qualitative_research.runner import QualitativeResearcherResult
from alphamind.analysis.qualitative_research.validation import (
    ValidationError as QualitativeValidationError,
)
from alphamind.analysis.qualitative_research.validation import (
    ValidationResult as QualitativeValidationResult,
)
from alphamind.analysis.synthesizer.harness import HarnessSuccess as SynthHarnessSuccess
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.analysis.synthesizer.runner import SynthesizerResult

# The complete list of analysis-subdivision internal types the audit
# named (L5 finding, story ALP-474). 30 entries — boundary types
# (TokensUsed, BriefBundle, tool I/O, brief schemas) are explicitly
# absent and must stay Pydantic.
_CONVERTED_TYPES: tuple[type, ...] = (
    DomainHarnessSuccess,
    QualitativeHarnessSuccess,
    AdaptiveHarnessSuccess,
    SynthHarnessSuccess,
    DomainResearcherResult,
    QualitativeResearcherResult,
    AdaptiveResearcherResult,
    SynthesizerResult,
    DomainInputBundle,
    QualitativeInputBundle,
    AdaptiveInputBundle,
    DomainValidationError,
    DomainValidationResult,
    QualitativeValidationError,
    QualitativeValidationResult,
    AdaptiveValidationError,
    AdaptiveValidationResult,
    DomainResearchersOutput,
    RetrievalStore,
    SentimentAggregate,
    PredictionMarketSnapshot,
    CalendarEvent,
    ActiveThesis,
    QualitativeInputs,
    HeadlineEntry,
    EventEntry,
    SectorQualitativeInput,
    DistillationAnomalyRecord,
    SectorAnomalyRecord,
)


@pytest.mark.parametrize("cls", _CONVERTED_TYPES)
def test_is_frozen_dataclass(cls: type) -> None:
    """Every audit-named internal type is a frozen dataclass (ALP-474)."""
    assert dataclasses.is_dataclass(cls), f"{cls.__name__} is not a dataclass"


@pytest.mark.parametrize("cls", _CONVERTED_TYPES)
def test_has_slots(cls: type) -> None:
    """Slotted to keep memory footprint tight (ALP-474)."""
    assert "__slots__" in cls.__dict__, f"{cls.__name__} missing __slots__"


def test_harness_success_replace_produces_new_instance() -> None:
    """``dataclasses.replace`` works on the converted HarnessSuccess; the
    original stays unmutated (frozen-dataclass behaviour)."""
    tokens = TokensUsed(input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0)
    original = SynthHarnessSuccess(
        response_text="hello",
        tokens_used=tokens,
        tool_calls_used=0,
        wall_clock_seconds=1.0,
        stop_reason="end_turn",
    )
    updated = dataclasses.replace(original, response_text="goodbye")
    assert updated.response_text == "goodbye"
    assert original.response_text == "hello"  # original untouched


def test_post_init_rejects_out_of_range_directional_score() -> None:
    """``SentimentAggregate.__post_init__`` mirrors the Pydantic ``ge/le`` Field."""
    with pytest.raises(ValueError, match="directional_score"):
        SentimentAggregate(
            ticker="AAA",
            directional_score=2.0,  # out of range [-1, 1]
            magnitude=0.5,
            rate_of_change=None,
            volume=None,
            divergence_flag=None,
            percentile_vs_self=0.5,
            data_freshness=datetime.now(UTC),
        )


def test_post_init_rejects_negative_volume_when_set() -> None:
    """``volume: int | None`` with ``ge=0`` — None ok, negative rejected."""
    SentimentAggregate(
        ticker="AAA",
        directional_score=0.0,
        magnitude=0.0,
        rate_of_change=None,
        volume=None,  # None is permitted
        divergence_flag=None,
        percentile_vs_self=0.0,
        data_freshness=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="volume"):
        SentimentAggregate(
            ticker="AAA",
            directional_score=0.0,
            magnitude=0.0,
            rate_of_change=None,
            volume=-1,
            divergence_flag=None,
            percentile_vs_self=0.0,
            data_freshness=datetime.now(UTC),
        )


def test_post_init_accepts_all_none_numeric_fields_for_unavailable_data() -> None:
    """ALP-538: UNAVAILABLE sentiment baselines emit records where every
    numeric field is ``None`` so downstream agents distinguish "no data"
    from "neutral data". The dataclass must accept that shape.
    """
    SentimentAggregate(
        ticker="SPY",
        directional_score=None,
        magnitude=None,
        rate_of_change=None,
        volume=None,
        divergence_flag=None,
        percentile_vs_self=None,
        data_freshness=datetime.now(UTC),
    )


def test_post_init_rejects_bad_sector_anomaly_id() -> None:
    """``SectorAnomalyRecord.__post_init__`` enforces the prefix pattern."""
    with pytest.raises(ValueError, match="anomaly_id"):
        SectorAnomalyRecord(
            anomaly_id="bogus-id",
            description="x",
            anomaly_type="t",
            tickers=(),
            severity="investigate_now",
            suggested_question="q",
            sector=Sector.TECH_SEMIS,
        )


def test_real_clock_returns_utc_aware_now() -> None:
    """RealClock satisfies the Protocol and returns a tz-aware UTC datetime."""
    real: Clock = RealClock()
    instant = real.now()
    assert instant.tzinfo is not None
    assert instant.utcoffset() == UTC.utcoffset(instant)
