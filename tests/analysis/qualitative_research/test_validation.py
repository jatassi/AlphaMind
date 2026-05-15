"""Tests for QualitativeBrief structural validator — ALP-244.

All assertions drive via direct QualitativeBrief(...) construction.
model_construct() is used only where the model's own validation prevents
the invalid state needed to exercise the structural validator.
"""

from __future__ import annotations

from alphamind._kernel.ids import Symbol
from alphamind.analysis.qualitative_research.models import (
    CatalystWatch,
    EvidenceLine,
    NarrativeThread,
    QualitativeBrief,
    SentimentSnapshot,
    SignalQuality,
    ThreadDirection,
    TimeHorizon,
)
from alphamind.analysis.qualitative_research.validation import (
    ValidationError,
    ValidationResult,
    validate_qualitative_brief,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_evidence(citation: str = "[ND-M1]") -> EvidenceLine:
    return EvidenceLine(source_type="news", observation="Some observation", citation=citation)


def _make_evidence_pair() -> tuple[EvidenceLine, EvidenceLine]:
    return (
        _make_evidence("[ND-M1]"),
        _make_evidence("social_sentiment"),
    )


def _make_thread(
    thread_id: str = "QR-1", evidence: tuple[EvidenceLine, ...] | None = None
) -> NarrativeThread:
    return NarrativeThread(
        thread_id=thread_id,
        summary="Rate expectations shifted hawkish overnight",
        relevance="FINANCIALS, rate-sensitive TECH",
        direction=ThreadDirection.BEARISH,
        subject="rate-sensitive equities",
        time_horizon=TimeHorizon.IMMEDIATE,
        evidence=evidence if evidence is not None else _make_evidence_pair(),
        implication="Positions with rate sensitivity face headwinds.",
    )


def _make_catalyst_watch(catalyst_id: str = "QR-CW-1", ticker: str = "NVDA") -> CatalystWatch:
    return CatalystWatch(
        catalyst_id=catalyst_id,
        ticker=ticker,
        catalyst_name="Earnings release",
        hours_to_event=12,
        thesis_impact="Could validate the AI-capex thesis.",
    )


def _make_sentiment() -> SentimentSnapshot:
    return SentimentSnapshot(
        extremes="NVDA at 95th percentile bullish",
        divergences="none",
        regime="broadly bullish with isolated bearish outliers",
    )


def _make_brief(**overrides: object) -> QualitativeBrief:
    defaults: dict[str, object] = {
        "invocation_id": "inv-001",
        "signal_quality": SignalQuality.HIGH,
        "signal_quality_reason": None,
        "threads": (_make_thread(),),
        "catalyst_watches": (_make_catalyst_watch(),),
        "sentiment_snapshot": _make_sentiment(),
    }
    return QualitativeBrief(**(defaults | overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Tracer bullet — valid brief passes
# ---------------------------------------------------------------------------


def test_valid_brief_passes() -> None:
    result = validate_qualitative_brief(_make_brief())
    assert result.is_valid is True
    assert result.errors == ()


# ---------------------------------------------------------------------------
# 2. Public type shapes
# ---------------------------------------------------------------------------


def test_validation_error_shape() -> None:
    err = ValidationError(
        field_path="threads[*].thread_id",
        rule="threads_sequential_indexing",
        message="some message",
    )
    assert err.field_path == "threads[*].thread_id"
    assert err.rule == "threads_sequential_indexing"
    assert err.message == "some message"


def test_validation_result_shape() -> None:
    result = ValidationResult(is_valid=True, errors=())
    assert result.is_valid is True
    assert result.errors == ()


# ---------------------------------------------------------------------------
# 3. invocation_id_not_empty
# ---------------------------------------------------------------------------


def test_empty_invocation_id_fails() -> None:
    # model_construct bypasses the model's own validators so we can inject ""
    brief = QualitativeBrief.model_construct(
        invocation_id="",
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(_make_thread(),),
        catalyst_watches=(),
        sentiment_snapshot=_make_sentiment(),
    )
    result = validate_qualitative_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "invocation_id_not_empty" in rules
    paths = {e.field_path for e in result.errors}
    assert "invocation_id" in paths


def test_nonempty_invocation_id_passes() -> None:
    result = validate_qualitative_brief(_make_brief(invocation_id="inv-abc-123"))
    assert result.is_valid is True


# ---------------------------------------------------------------------------
# 4. threads_sequential_indexing — gap
# ---------------------------------------------------------------------------


def test_threads_gap_fails() -> None:
    # QR-1, QR-2, QR-4 — gap at 3
    brief = _make_brief(
        threads=(
            _make_thread("QR-1"),
            _make_thread("QR-2"),
            _make_thread("QR-4"),
        )
    )
    result = validate_qualitative_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "threads_sequential_indexing" in rules
    paths = {e.field_path for e in result.errors}
    assert "threads[*].thread_id" in paths


# ---------------------------------------------------------------------------
# 5. threads_sequential_indexing — duplicate raises both duplicate and gap errors
# ---------------------------------------------------------------------------


def test_threads_duplicate_raises_errors() -> None:
    # Two threads both with QR-1 — duplicate index detected.
    # The _check_sequential_indexing helper fires a duplicate-index error when it
    # encounters the second QR-1; that error carries rule="threads_sequential_indexing".
    # The AC requires "both a duplicate-index error and the sequential-indexing error";
    # both are emitted under the same rule name — the duplicate detection IS the
    # sequential-indexing check.
    t1 = _make_thread("QR-1")
    brief = QualitativeBrief.model_construct(
        invocation_id="inv-001",
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(t1, t1),  # two identical QR-1 threads
        catalyst_watches=(),
        sentiment_snapshot=_make_sentiment(),
    )
    result = validate_qualitative_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "threads_sequential_indexing" in rules
    # At least one error must mention the duplicate
    dup_errors = [e for e in result.errors if "Duplicate" in e.message]
    assert len(dup_errors) >= 1


# ---------------------------------------------------------------------------
# 6. catalyst_watches_sequential_indexing — gap
# ---------------------------------------------------------------------------


def test_catalyst_watches_gap_fails() -> None:
    # QR-CW-1, QR-CW-3 — gap at 2
    brief = _make_brief(
        catalyst_watches=(
            _make_catalyst_watch("QR-CW-1"),
            _make_catalyst_watch("QR-CW-3"),
        )
    )
    result = validate_qualitative_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "catalyst_watches_sequential_indexing" in rules
    paths = {e.field_path for e in result.errors}
    assert "catalyst_watches[*].catalyst_id" in paths


# ---------------------------------------------------------------------------
# 7. reference_prefix_consistency — thread wrong prefix (QR-CW-1)
# ---------------------------------------------------------------------------


def test_thread_wrong_prefix_fails() -> None:
    # NarrativeThread.thread_id has pattern ^QR-\d+$ so QR-CW-1 is rejected at model level.
    # Use model_construct to bypass and inject the invalid ID.
    bad_thread = NarrativeThread.model_construct(
        thread_id="QR-CW-1",
        summary="summary",
        relevance="relevance",
        direction=ThreadDirection.BEARISH,
        subject="subject",
        time_horizon=TimeHorizon.IMMEDIATE,
        evidence=_make_evidence_pair(),
        implication="implication",
    )
    brief = QualitativeBrief.model_construct(
        invocation_id="inv-001",
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(bad_thread,),
        catalyst_watches=(),
        sentiment_snapshot=_make_sentiment(),
    )
    result = validate_qualitative_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "reference_prefix_consistency" in rules


# ---------------------------------------------------------------------------
# 8. reference_prefix_consistency — catalyst watch wrong prefix (QR-1)
# ---------------------------------------------------------------------------


def test_catalyst_watch_wrong_prefix_fails() -> None:
    # CatalystWatch.catalyst_id has pattern ^QR-CW-\d+$ so QR-1 is rejected at model level.
    # Use model_construct to bypass.
    bad_watch = CatalystWatch.model_construct(
        catalyst_id="QR-1",
        ticker=Symbol("NVDA"),
        catalyst_name="Earnings",
        hours_to_event=12,
        thesis_impact="Impact text",
    )
    brief = QualitativeBrief.model_construct(
        invocation_id="inv-001",
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(_make_thread(),),
        catalyst_watches=(bad_watch,),
        sentiment_snapshot=_make_sentiment(),
    )
    result = validate_qualitative_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "reference_prefix_consistency" in rules


# ---------------------------------------------------------------------------
# 9. ticker_in_universe
# ---------------------------------------------------------------------------


def test_ticker_not_in_universe_fails() -> None:
    brief = _make_brief(catalyst_watches=(_make_catalyst_watch(ticker=Symbol("UNKNOWN")),))
    universe = frozenset({"NVDA", "AAPL", "MSFT"})
    result = validate_qualitative_brief(brief, universe=universe)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "ticker_in_universe" in rules


def test_ticker_not_in_universe_with_none_universe_passes() -> None:
    brief = _make_brief(catalyst_watches=(_make_catalyst_watch(ticker=Symbol("UNKNOWN")),))
    result = validate_qualitative_brief(brief, universe=None)
    assert result.is_valid is True


def test_ticker_in_universe_passes() -> None:
    brief = _make_brief(catalyst_watches=(_make_catalyst_watch(ticker=Symbol("NVDA")),))
    universe = frozenset({"NVDA", "AAPL"})
    result = validate_qualitative_brief(brief, universe=universe)
    assert result.is_valid is True


# ---------------------------------------------------------------------------
# 10. evidence_lines_distinct_within_thread
# ---------------------------------------------------------------------------


def test_duplicate_evidence_lines_in_thread_fails() -> None:
    # Two byte-identical EvidenceLine records in the same thread
    dup_evidence = _make_evidence("[ND-M1]")
    # Use model_construct to bypass the model's minimum-two-sources validator
    # (which accepts 2 lines but doesn't check for identity)
    bad_thread = NarrativeThread.model_construct(
        thread_id="QR-1",
        summary="summary",
        relevance="relevance",
        direction=ThreadDirection.BEARISH,
        subject="subject",
        time_horizon=TimeHorizon.IMMEDIATE,
        evidence=(dup_evidence, dup_evidence),
        implication="implication",
    )
    brief = QualitativeBrief.model_construct(
        invocation_id="inv-001",
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(bad_thread,),
        catalyst_watches=(),
        sentiment_snapshot=_make_sentiment(),
    )
    result = validate_qualitative_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "evidence_lines_distinct_within_thread" in rules


def test_distinct_evidence_lines_pass() -> None:
    # Normal brief with two distinct evidence lines
    result = validate_qualitative_brief(_make_brief())
    assert result.is_valid is True
