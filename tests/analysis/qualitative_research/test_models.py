"""Tests for QualitativeBrief data model — ALP-242.

Exercises every invariant via direct construction (not parser round-trip).
"""

from __future__ import annotations

import pytest

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

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_evidence(**overrides: object) -> EvidenceLine:
    defaults: dict[str, object] = {
        "source_type": "news",
        "observation": "Rates rose sharply",
        "citation": "[ND-M1]",
    }
    return EvidenceLine(**(defaults | overrides))  # type: ignore[arg-type]


def _make_evidence_pair() -> tuple[EvidenceLine, EvidenceLine]:
    return (
        _make_evidence(source_type="news", observation="obs-1", citation="[ND-M1]"),
        _make_evidence(source_type="sentiment", observation="obs-2", citation="social_sentiment"),
    )


def _make_thread(thread_id: str = "QR-1", **overrides: object) -> NarrativeThread:
    defaults: dict[str, object] = {
        "thread_id": thread_id,
        "summary": "Rate expectations shifted hawkish overnight",
        "relevance": "FINANCIALS, rate-sensitive TECH",
        "direction": ThreadDirection.BEARISH,
        "subject": "rate-sensitive equities",
        "time_horizon": TimeHorizon.IMMEDIATE,
        "evidence": _make_evidence_pair(),
        "implication": "Positions with rate sensitivity face headwinds.",
    }
    return NarrativeThread(**(defaults | overrides))  # type: ignore[arg-type]


def _make_catalyst_watch(catalyst_id: str = "QR-CW-1", **overrides: object) -> CatalystWatch:
    defaults: dict[str, object] = {
        "catalyst_id": catalyst_id,
        "ticker": "NVDA",
        "catalyst_name": "Earnings release",
        "hours_to_event": 12,
        "thesis_impact": "Could validate the AI-capex thesis.",
    }
    return CatalystWatch(**(defaults | overrides))  # type: ignore[arg-type]


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
# 1. Imports
# ---------------------------------------------------------------------------


class TestImports:
    def test_all_public_names_importable(self) -> None:
        """All model names listed in acceptance criteria resolve."""
        from alphamind.analysis.qualitative_research import models

        for name in (
            "QualitativeBrief",
            "NarrativeThread",
            "EvidenceLine",
            "CatalystWatch",
            "SentimentSnapshot",
            "ThreadDirection",
            "TimeHorizon",
        ):
            assert hasattr(models, name), f"{name} not found in models module"

    def test_signal_quality_reexport_is_same_object(self) -> None:
        """`SignalQuality` re-exported from models is the same object as `_shared.SignalQuality`."""
        from alphamind.analysis._shared import SignalQuality as SharedSQ
        from alphamind.analysis.qualitative_research.models import SignalQuality as ModelsSQ

        assert ModelsSQ is SharedSQ


# ---------------------------------------------------------------------------
# 2. ThreadDirection and TimeHorizon enums
# ---------------------------------------------------------------------------


class TestEnums:
    def test_thread_direction_values(self) -> None:
        assert ThreadDirection.BULLISH.value == "bullish"
        assert ThreadDirection.BEARISH.value == "bearish"
        assert ThreadDirection.MIXED.value == "mixed"
        assert ThreadDirection.UNCERTAIN.value == "uncertain"

    def test_time_horizon_values(self) -> None:
        assert TimeHorizon.IMMEDIATE.value == "immediate"
        assert TimeHorizon.NEAR_TERM.value == "near_term"
        assert TimeHorizon.DEVELOPING.value == "developing"

    def test_time_horizon_display_map_exists(self) -> None:
        from alphamind.analysis.qualitative_research.models import TIME_HORIZON_DISPLAY

        assert isinstance(TIME_HORIZON_DISPLAY, dict)
        assert set(TIME_HORIZON_DISPLAY.keys()) == set(TimeHorizon)

    def test_time_horizon_display_contains_suffixes(self) -> None:
        from alphamind.analysis.qualitative_research.models import TIME_HORIZON_DISPLAY

        assert "(<24h)" in TIME_HORIZON_DISPLAY[TimeHorizon.IMMEDIATE]
        assert "(24-72h)" in TIME_HORIZON_DISPLAY[TimeHorizon.NEAR_TERM]
        assert "(>72h)" in TIME_HORIZON_DISPLAY[TimeHorizon.DEVELOPING]


# ---------------------------------------------------------------------------
# 3. EvidenceLine
# ---------------------------------------------------------------------------


class TestEvidenceLine:
    def test_valid_construction(self) -> None:
        e = _make_evidence()
        assert e.source_type == "news"
        assert e.observation == "Rates rose sharply"
        assert e.citation == "[ND-M1]"

    def test_empty_source_type_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_evidence(source_type="")

    def test_empty_observation_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_evidence(observation="")

    def test_empty_citation_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_evidence(citation="")

    def test_immutable(self) -> None:
        e = _make_evidence()
        with pytest.raises((ValueError, TypeError, TypeError)):
            e.source_type = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 4. NarrativeThread
# ---------------------------------------------------------------------------


class TestNarrativeThread:
    def test_valid_construction(self) -> None:
        t = _make_thread()
        assert t.thread_id == "QR-1"
        assert t.direction == ThreadDirection.BEARISH

    def test_thread_id_pattern_valid(self) -> None:
        for tid in ("QR-1", "QR-12", "QR-100"):
            t = _make_thread(thread_id=tid)
            assert t.thread_id == tid

    def test_wrong_prefix_rejected(self) -> None:
        """QR-CW-1 does not match ^QR-\\d+$."""
        with pytest.raises((ValueError, TypeError)):
            _make_thread(thread_id="QR-CW-1")

    def test_non_qr_prefix_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_thread(thread_id="SA-TECH-1")

    def test_single_evidence_line_rejected(self) -> None:
        """Design doc requires minimum two input sources per thread."""
        with pytest.raises((ValueError, TypeError), match="two"):
            _make_thread(evidence=(_make_evidence(),))

    def test_zero_evidence_lines_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_thread(evidence=())

    def test_two_evidence_lines_accepted(self) -> None:
        t = _make_thread(evidence=_make_evidence_pair())
        assert len(t.evidence) == 2

    def test_three_or_more_evidence_lines_accepted(self) -> None:
        third = _make_evidence(
            source_type="prediction_markets",
            observation="obs-3",
            citation="prediction_markets",
        )
        t = _make_thread(evidence=(*_make_evidence_pair(), third))
        assert len(t.evidence) == 3

    def test_empty_summary_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_thread(summary="")

    def test_empty_relevance_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_thread(relevance="")

    def test_empty_subject_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_thread(subject="")

    def test_empty_implication_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_thread(implication="")

    def test_immutable(self) -> None:
        t = _make_thread()
        with pytest.raises((ValueError, TypeError, TypeError)):
            t.summary = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 5. CatalystWatch
# ---------------------------------------------------------------------------


class TestCatalystWatch:
    def test_valid_construction(self) -> None:
        c = _make_catalyst_watch()
        assert c.catalyst_id == "QR-CW-1"
        assert c.ticker == "NVDA"
        assert c.hours_to_event == 12

    def test_catalyst_id_pattern_valid(self) -> None:
        for cid in ("QR-CW-1", "QR-CW-10", "QR-CW-999"):
            c = _make_catalyst_watch(catalyst_id=cid)
            assert c.catalyst_id == cid

    def test_wrong_prefix_qr_only_rejected(self) -> None:
        """QR-1 does not match ^QR-CW-\\d+$."""
        with pytest.raises((ValueError, TypeError)):
            _make_catalyst_watch(catalyst_id="QR-1")

    def test_wrong_prefix_sa_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_catalyst_watch(catalyst_id="SA-TECH-1")

    def test_negative_hours_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_catalyst_watch(hours_to_event=-1)

    def test_zero_hours_accepted(self) -> None:
        c = _make_catalyst_watch(hours_to_event=0)
        assert c.hours_to_event == 0

    def test_empty_ticker_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_catalyst_watch(ticker=Symbol(""))

    def test_immutable(self) -> None:
        c = _make_catalyst_watch()
        with pytest.raises((ValueError, TypeError, TypeError)):
            c.ticker = "AAPL"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 6. SentimentSnapshot
# ---------------------------------------------------------------------------


class TestSentimentSnapshot:
    def test_valid_construction(self) -> None:
        s = _make_sentiment()
        assert s.extremes == "NVDA at 95th percentile bullish"
        assert s.divergences == "none"
        assert s.regime == "broadly bullish with isolated bearish outliers"

    def test_none_values_are_valid_strings(self) -> None:
        """Design doc says 'none' is the valid quiet-day value."""
        s = SentimentSnapshot(
            extremes="none", divergences="none", regime="neutral, no notable shifts"
        )
        assert s.extremes == "none"

    def test_empty_extremes_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            SentimentSnapshot(extremes="", divergences="none", regime="neutral")

    def test_empty_divergences_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            SentimentSnapshot(extremes="none", divergences="", regime="neutral")

    def test_empty_regime_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            SentimentSnapshot(extremes="none", divergences="none", regime="")

    def test_immutable(self) -> None:
        s = _make_sentiment()
        with pytest.raises((ValueError, TypeError, TypeError)):
            s.regime = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 7. QualitativeBrief
# ---------------------------------------------------------------------------


class TestQualitativeBrief:
    def test_valid_construction(self) -> None:
        b = _make_brief()
        assert b.invocation_id == "inv-001"
        assert len(b.threads) == 1

    def test_degraded_requires_reason(self) -> None:
        """DEGRADED signal quality must carry a reason string."""
        with pytest.raises((ValueError, TypeError)):
            _make_brief(signal_quality=SignalQuality.DEGRADED, signal_quality_reason=None)

    def test_degraded_with_reason_accepted(self) -> None:
        b = _make_brief(
            signal_quality=SignalQuality.DEGRADED,
            signal_quality_reason="news API returned partial results",
        )
        assert b.signal_quality == SignalQuality.DEGRADED
        assert b.signal_quality_reason == "news API returned partial results"

    def test_non_degraded_with_reason_rejected(self) -> None:
        """Reason must be None when quality is not DEGRADED."""
        for quality in (SignalQuality.HIGH, SignalQuality.MODERATE, SignalQuality.LOW):
            with pytest.raises((ValueError, TypeError)):
                _make_brief(signal_quality=quality, signal_quality_reason="some reason")

    def test_empty_threads_rejected(self) -> None:
        """At least one narrative thread is always required."""
        with pytest.raises((ValueError, TypeError), match=r"(?i)thread"):
            _make_brief(threads=())

    def test_empty_catalyst_watches_accepted(self) -> None:
        """Catalyst watch section may be empty when no catalysts are imminent."""
        b = _make_brief(catalyst_watches=())
        assert b.catalyst_watches == ()

    def test_immutable(self) -> None:
        b = _make_brief()
        with pytest.raises((ValueError, TypeError, TypeError)):
            b.invocation_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 8. __all__ completeness
# ---------------------------------------------------------------------------


class TestDunderAll:
    def test_all_contains_public_names(self) -> None:
        import alphamind.analysis.qualitative_research.models as m

        expected = {
            "CatalystWatch",
            "EvidenceLine",
            "NarrativeThread",
            "QualitativeBrief",
            "SentimentSnapshot",
            "SignalQuality",
            "ThreadDirection",
            "TimeHorizon",
            "TIME_HORIZON_DISPLAY",
        }
        assert expected.issubset(set(m.__all__))
