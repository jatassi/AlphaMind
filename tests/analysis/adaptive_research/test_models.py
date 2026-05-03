"""Tests for AdaptiveBrief data model — ALP-255.

Exercises every invariant via direct construction (not parser round-trip).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from alphamind.analysis._shared import Sector
from alphamind.analysis.adaptive_research.models import (
    AdaptiveBrief,
    Assessment,
    Confidence,
    InvestigationThread,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _signal_thread(thread_id: str = "AR-1", **overrides: object) -> InvestigationThread:
    defaults: dict[str, object] = {
        "thread_id": thread_id,
        "trigger": "[SA-TECH-ANOM-1]",
        "question": "What drove NVDA's volume spike?",
        "tickers": ("NVDA",),
        "sector": Sector.TECH_SEMIS,
        "tools_used": ("news_search", "options_flow"),
        "findings": ("news_search returned three sell-side notes",),
        "assessment": Assessment.SIGNAL,
        "confidence": Confidence.MODERATE,
        "implication": "Pre-earnings repositioning, not information asymmetry.",
        "strengthens": ("[SA-TECH-2]",),
        "weakens": (),
    }
    return InvestigationThread(**(defaults | overrides))  # type: ignore[arg-type]


def _noise_thread(thread_id: str = "AR-2", **overrides: object) -> InvestigationThread:
    defaults: dict[str, object] = {
        "thread_id": thread_id,
        "trigger": "Distillation: Q2 volume spike, NVDA, 3.2 sigma",
        "question": "Single-name flow event?",
        "tickers": ("NVDA",),
        "sector": Sector.TECH_SEMIS,
        "tools_used": ("news_search",),
        "findings": ("news_search returned no headlines",),
        "assessment": Assessment.NOISE,
        "confidence": Confidence.HIGH,
        "dismissal_reason": "Routine end-of-quarter rebalance flow.",
    }
    return InvestigationThread(**(defaults | overrides))  # type: ignore[arg-type]


def _inconclusive_thread(thread_id: str = "AR-3", **overrides: object) -> InvestigationThread:
    defaults: dict[str, object] = {
        "thread_id": thread_id,
        "trigger": "[SA-ENERGY-ANOM-1]",
        "question": "Refining-group correlation break — single-name or sector-wide?",
        "tickers": ("VLO", "MPC", "PSX"),
        "sector": Sector.ENERGY,
        "tools_used": ("news_search", "macro_data"),
        "findings": (
            "no recent unplanned-outage headlines",
            "crack spread widened gradually over 5 days",
        ),
        "assessment": Assessment.INCONCLUSIVE,
        "confidence": Confidence.LOW,
        "missing": "Per-name capacity-utilization data for the past two weeks.",
    }
    return InvestigationThread(**(defaults | overrides))  # type: ignore[arg-type]


def _make_brief(**overrides: object) -> AdaptiveBrief:
    threads = (_signal_thread(),)
    defaults: dict[str, object] = {
        "invocation_id": "inv-2026-04-23T14-30Z",
        "threads_investigated_count": len(threads),
        "anomalies_triaged_count": len(threads),
        "anomalies_deferred": (),
        "threads": threads,
    }
    return AdaptiveBrief(**(defaults | overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------


class TestImports:
    def test_all_public_names_importable(self) -> None:
        """All model names listed in acceptance criteria resolve."""
        from alphamind.analysis.adaptive_research import models

        for name in (
            "AdaptiveBrief",
            "InvestigationThread",
            "Assessment",
            "Confidence",
        ):
            assert hasattr(models, name), f"{name} not found in models module"


# ---------------------------------------------------------------------------
# 2. Assessment and Confidence enums
# ---------------------------------------------------------------------------


class TestEnums:
    def test_assessment_values(self) -> None:
        assert Assessment("signal") is Assessment.SIGNAL
        assert Assessment("noise") is Assessment.NOISE
        assert Assessment("inconclusive") is Assessment.INCONCLUSIVE

    def test_assessment_rejects_other_strings(self) -> None:
        with pytest.raises(ValueError):
            Assessment("unknown")

    def test_confidence_values(self) -> None:
        assert Confidence("high") is Confidence.HIGH
        assert Confidence("moderate") is Confidence.MODERATE
        assert Confidence("low") is Confidence.LOW

    def test_confidence_rejects_other_strings(self) -> None:
        with pytest.raises(ValueError):
            Confidence("medium")


# ---------------------------------------------------------------------------
# 3. InvestigationThread — basic shape and `thread_id` pattern
# ---------------------------------------------------------------------------


class TestThreadIdPattern:
    def test_ar_pattern_valid(self) -> None:
        for tid in ("AR-1", "AR-12", "AR-100"):
            t = _signal_thread(thread_id=tid)
            assert t.thread_id == tid

    def test_qr_prefix_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _signal_thread(thread_id="QR-1")

    def test_sa_prefix_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _signal_thread(thread_id="SA-TECH-1")


# ---------------------------------------------------------------------------
# 4. InvestigationThread — SIGNAL invariants
# ---------------------------------------------------------------------------


class TestSignalThread:
    def test_valid_signal_thread(self) -> None:
        t = _signal_thread()
        assert t.assessment is Assessment.SIGNAL
        assert t.implication is not None
        assert t.strengthens is not None
        assert t.weakens is not None

    def test_signal_missing_implication_rejected(self) -> None:
        with pytest.raises(ValidationError, match="implication"):
            _signal_thread(implication=None)

    def test_signal_missing_strengthens_rejected(self) -> None:
        with pytest.raises(ValidationError, match="strengthens"):
            _signal_thread(strengthens=None)

    def test_signal_missing_weakens_rejected(self) -> None:
        with pytest.raises(ValidationError, match="weakens"):
            _signal_thread(weakens=None)

    def test_signal_with_dismissal_reason_rejected(self) -> None:
        with pytest.raises(ValidationError, match="dismissal_reason"):
            _signal_thread(dismissal_reason="should not be set")

    def test_signal_with_missing_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="missing"):
            _signal_thread(missing="should not be set")

    def test_signal_explicit_empty_strengthens_accepted(self) -> None:
        """Empty tuple is valid: 'applicable, explicitly empty' (wire `none` parses to ())."""
        t = _signal_thread(strengthens=(), weakens=())
        assert t.strengthens == ()
        assert t.weakens == ()


# ---------------------------------------------------------------------------
# 5. InvestigationThread — NOISE invariants
# ---------------------------------------------------------------------------


class TestNoiseThread:
    def test_valid_noise_thread(self) -> None:
        t = _noise_thread()
        assert t.assessment is Assessment.NOISE
        assert t.dismissal_reason is not None

    def test_noise_missing_dismissal_reason_rejected(self) -> None:
        with pytest.raises(ValidationError, match="dismissal_reason"):
            _noise_thread(dismissal_reason=None)

    def test_noise_with_implication_rejected(self) -> None:
        with pytest.raises(ValidationError, match="implication"):
            _noise_thread(implication="should not be set")

    def test_noise_with_strengthens_rejected(self) -> None:
        with pytest.raises(ValidationError, match="strengthens"):
            _noise_thread(strengthens=())

    def test_noise_with_weakens_rejected(self) -> None:
        with pytest.raises(ValidationError, match="weakens"):
            _noise_thread(weakens=())

    def test_noise_with_missing_rejected(self) -> None:
        with pytest.raises(ValidationError, match="missing"):
            _noise_thread(missing="should not be set")


# ---------------------------------------------------------------------------
# 6. InvestigationThread — INCONCLUSIVE invariants
# ---------------------------------------------------------------------------


class TestInconclusiveThread:
    def test_valid_inconclusive_thread(self) -> None:
        t = _inconclusive_thread()
        assert t.assessment is Assessment.INCONCLUSIVE
        assert t.missing is not None

    def test_inconclusive_missing_field_required(self) -> None:
        with pytest.raises(ValidationError, match="missing"):
            _inconclusive_thread(missing=None)

    def test_inconclusive_with_implication_rejected(self) -> None:
        with pytest.raises(ValidationError, match="implication"):
            _inconclusive_thread(implication="should not be set")

    def test_inconclusive_with_strengthens_rejected(self) -> None:
        with pytest.raises(ValidationError, match="strengthens"):
            _inconclusive_thread(strengthens=())

    def test_inconclusive_with_weakens_rejected(self) -> None:
        with pytest.raises(ValidationError, match="weakens"):
            _inconclusive_thread(weakens=())

    def test_inconclusive_with_dismissal_reason_rejected(self) -> None:
        with pytest.raises(ValidationError, match="dismissal_reason"):
            _inconclusive_thread(dismissal_reason="should not be set")


# ---------------------------------------------------------------------------
# 7. InvestigationThread — empty-tuple semantics for required-collection fields
# ---------------------------------------------------------------------------


class TestThreadEmptyCollections:
    def test_empty_tickers_accepted(self) -> None:
        """Macro/funding-stress anomalies may have no specific ticker."""
        t = _signal_thread(tickers=())
        assert t.tickers == ()

    def test_empty_tools_used_accepted(self) -> None:
        """A thread that triaged on the trigger payload alone is valid (rare but allowed)."""
        t = _signal_thread(tools_used=())
        assert t.tools_used == ()

    def test_empty_findings_accepted_for_noise(self) -> None:
        """A noise thread can dismiss based on trigger characteristics alone."""
        t = _noise_thread(findings=())
        assert t.findings == ()


# ---------------------------------------------------------------------------
# 8. InvestigationThread — basic field invariants
# ---------------------------------------------------------------------------


class TestThreadFieldInvariants:
    def test_immutable(self) -> None:
        t = _signal_thread()
        with pytest.raises((ValidationError, TypeError)):
            t.thread_id = "AR-99"  # type: ignore[misc]

    def test_empty_trigger_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _signal_thread(trigger="")

    def test_empty_question_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _signal_thread(question="")


# ---------------------------------------------------------------------------
# 9. AdaptiveBrief — happy path and quiet-cycle
# ---------------------------------------------------------------------------


class TestAdaptiveBrief:
    def test_valid_brief_with_one_thread(self) -> None:
        b = _make_brief()
        assert b.invocation_id == "inv-2026-04-23T14-30Z"
        assert b.threads_investigated_count == 1
        assert b.anomalies_triaged_count == 1

    def test_quiet_cycle_zero_threads_accepted(self) -> None:
        """Empty `threads` is valid; design doc explicitly names zero-thread output as correct."""
        b = AdaptiveBrief(
            invocation_id="inv-quiet",
            threads_investigated_count=0,
            anomalies_triaged_count=0,
            anomalies_deferred=(),
            threads=(),
        )
        assert b.threads == ()
        assert b.threads_investigated_count == 0

    def test_quiet_cycle_with_deferred_anomalies(self) -> None:
        """Header lists deferred anomalies even when zero threads were investigated."""
        b = AdaptiveBrief(
            invocation_id="inv-quiet",
            threads_investigated_count=0,
            anomalies_triaged_count=2,
            anomalies_deferred=("[SA-ENERGY-ANOM-1]", "[SA-FIN-ANOM-3]"),
            threads=(),
        )
        assert len(b.anomalies_deferred) == 2

    def test_threads_investigated_count_must_match_threads_length(self) -> None:
        with pytest.raises(ValidationError, match=r"threads_investigated_count"):
            _make_brief(threads_investigated_count=2)

    def test_anomalies_triaged_must_be_at_least_threads_investigated(self) -> None:
        with pytest.raises(ValidationError, match=r"anomalies_triaged_count"):
            _make_brief(threads_investigated_count=1, anomalies_triaged_count=0)

    def test_anomalies_triaged_can_exceed_threads_investigated(self) -> None:
        """Some anomalies are triaged but deferred — triaged count >= investigated count."""
        b = _make_brief(
            anomalies_triaged_count=5,
            anomalies_deferred=("[SA-ENERGY-ANOM-1]",),
        )
        assert b.anomalies_triaged_count == 5

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_brief(threads_investigated_count=-1)
        with pytest.raises(ValidationError):
            _make_brief(anomalies_triaged_count=-1)

    def test_immutable(self) -> None:
        b = _make_brief()
        with pytest.raises((ValidationError, TypeError)):
            b.invocation_id = "changed"  # type: ignore[misc]

    def test_brief_with_three_threads_one_per_assessment(self) -> None:
        threads = (_signal_thread(), _noise_thread(), _inconclusive_thread())
        b = AdaptiveBrief(
            invocation_id="inv-mixed",
            threads_investigated_count=3,
            anomalies_triaged_count=4,
            anomalies_deferred=("[SA-FIN-ANOM-2]",),
            threads=threads,
        )
        assert len(b.threads) == 3
        assert {t.assessment for t in b.threads} == set(Assessment)


# ---------------------------------------------------------------------------
# 10. __all__ completeness
# ---------------------------------------------------------------------------


class TestDunderAll:
    def test_all_lists_exact_public_names(self) -> None:
        import alphamind.analysis.adaptive_research.models as m

        assert set(m.__all__) == {
            "AdaptiveBrief",
            "Assessment",
            "Confidence",
            "InvestigationThread",
        }
