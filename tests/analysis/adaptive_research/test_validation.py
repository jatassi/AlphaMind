"""Tests for AdaptiveBrief structural validator — ALP-258.

Layer-2 (structural) and Layer-3 (referential integrity) tests.
The Layer-3 referential resolution against three upstream brief contexts is
the structural check unique to this agent — qualitative-research's validator
has no equivalent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.analysis._shared import Sector, SignalQuality
from alphamind.analysis.adaptive_research.models import (
    AdaptiveBrief,
    Assessment,
    Confidence,
    InvestigationThread,
)
from alphamind.analysis.adaptive_research.validation import (
    ValidationError,
    ValidationResult,
    validate_adaptive_brief,
)
from alphamind.analysis.domain_researchers.models import (
    Anomaly,
    AnomalyType,
    ConvictionSketch,
    Direction,
    Finding,
    SectorBrief,
    SetupType,
    SignalType,
    Strength,
    ThesisCandidate,
)
from alphamind.analysis.qualitative_research.models import (
    CatalystWatch,
    EvidenceLine,
    NarrativeThread,
    QualitativeBrief,
    SentimentSnapshot,
    ThreadDirection,
    TimeHorizon,
)
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief

# ---------------------------------------------------------------------------
# Upstream-brief fixture builders
# ---------------------------------------------------------------------------


def _sector_brief(sector: Sector, prefix: str) -> SectorBrief:
    """Build a SectorBrief with one of each (finding, anomaly, thesis_candidate).

    Reference IDs follow the prefix (e.g., ``SA-TECH-1``, ``SA-TECH-ANOM-1``,
    ``SA-TECH-TC-1``).
    """
    return SectorBrief(
        invocation_id="inv-001",
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(
            Finding(
                finding_id=f"{prefix}-1",
                headline="Headline",
                tickers=("NVDA",),
                signal_type=SignalType.PRICE_ACTION,
                strength=Strength.MODERATE,
                detail="Detail",
            ),
        ),
        anomalies=(
            Anomaly(
                anomaly_id=f"{prefix}-ANOM-1",
                description="Anomaly description",
                anomaly_type=AnomalyType.VOLUME,
                tickers=("NVDA",),
                severity="note_for_context",
                suggested_question="What caused this?",
            ),
        ),
        thesis_candidates=(
            ThesisCandidate(
                thesis_candidate_id=f"{prefix}-TC-1",
                ticker="NVDA",
                direction=Direction.LONG,
                setup_type=SetupType.CATALYST,
                catalyst="Earnings",
                time_horizon_hours="48",
                conviction_sketch=ConvictionSketch.MODERATE,
                conviction_justification="Justification",
                key_risk="Risk",
            ),
        ),
    )


def _sector_briefs() -> tuple[SectorBrief, ...]:
    return (
        _sector_brief(Sector.TECH_SEMIS, "SA-TECH"),
        _sector_brief(Sector.FINANCIALS, "SA-FIN"),
        _sector_brief(Sector.ENERGY, "SA-ENERGY"),
    )


def _evidence_pair() -> tuple[EvidenceLine, EvidenceLine]:
    return (
        EvidenceLine(source_type="news", observation="Obs", citation="[ND-M1]"),
        EvidenceLine(source_type="social", observation="Obs2", citation="social_sentiment"),
    )


def _qualitative_brief() -> QualitativeBrief:
    return QualitativeBrief(
        invocation_id="inv-001",
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(
            NarrativeThread(
                thread_id="QR-1",
                summary="Rate hawks",
                relevance="rate-sensitive",
                direction=ThreadDirection.BEARISH,
                subject="rate-sensitive equities",
                time_horizon=TimeHorizon.IMMEDIATE,
                evidence=_evidence_pair(),
                implication="Headwinds.",
            ),
        ),
        catalyst_watches=(
            CatalystWatch(
                catalyst_id="QR-CW-1",
                ticker="NVDA",
                catalyst_name="Earnings",
                hours_to_event=12,
                thesis_impact="Impact.",
            ),
        ),
        sentiment_snapshot=SentimentSnapshot(
            extremes="none",
            divergences="none",
            regime="neutral",
        ),
    )


def _correlation_brief() -> CorrelationRegimeBrief:
    return CorrelationRegimeBrief(
        text="CORRELATION & REGIME BRIEF\n[CR-1] regime\n",
        reference_index={"CR-1": "regime.label", "CR-2": "q7.intermarket_regime.dxy_to_spx"},
        freshness_min=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# AdaptiveBrief fixture builders
# ---------------------------------------------------------------------------


def _signal_thread(
    thread_id: str = "AR-1",
    *,
    strengthens: tuple[str, ...] = ("SA-TECH-1",),
    weakens: tuple[str, ...] = (),
    **overrides: object,
) -> InvestigationThread:
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
        "implication": "Pre-earnings repositioning.",
        "strengthens": strengthens,
        "weakens": weakens,
    }
    return InvestigationThread(**(defaults | overrides))  # type: ignore[arg-type]


def _noise_thread(thread_id: str = "AR-2", **overrides: object) -> InvestigationThread:
    defaults: dict[str, object] = {
        "thread_id": thread_id,
        "trigger": "Distillation: volume spike, NVDA, 3.2 sigma",
        "question": "Single-name flow event?",
        "tickers": ("NVDA",),
        "sector": Sector.TECH_SEMIS,
        "tools_used": ("news_search",),
        "findings": ("no headlines",),
        "assessment": Assessment.NOISE,
        "confidence": Confidence.HIGH,
        "dismissal_reason": "Routine end-of-quarter rebalance flow.",
    }
    return InvestigationThread(**(defaults | overrides))  # type: ignore[arg-type]


def _inconclusive_thread(thread_id: str = "AR-3", **overrides: object) -> InvestigationThread:
    defaults: dict[str, object] = {
        "thread_id": thread_id,
        "trigger": "[SA-ENERGY-ANOM-1]",
        "question": "Refining-group correlation break?",
        "tickers": ("VLO",),
        "sector": Sector.ENERGY,
        "tools_used": ("news_search",),
        "findings": ("no recent unplanned-outage headlines",),
        "assessment": Assessment.INCONCLUSIVE,
        "confidence": Confidence.LOW,
        "missing": "Per-name capacity-utilization data.",
    }
    return InvestigationThread(**(defaults | overrides))  # type: ignore[arg-type]


def _make_brief(threads: tuple[InvestigationThread, ...] | None = None) -> AdaptiveBrief:
    threads = threads if threads is not None else (_signal_thread(),)
    return AdaptiveBrief(
        invocation_id="inv-2026-04-23T14-30Z",
        threads_investigated_count=len(threads),
        anomalies_triaged_count=len(threads),
        anomalies_deferred=(),
        threads=threads,
    )


# ---------------------------------------------------------------------------
# 1. Tracer bullet — public types resolve and a valid brief passes
# ---------------------------------------------------------------------------


def test_public_names_importable() -> None:
    """The acceptance-criterion public surface resolves cleanly."""
    assert ValidationError is not None
    assert ValidationResult is not None
    assert validate_adaptive_brief is not None


def test_valid_brief_passes() -> None:
    """A well-formed brief with resolvable references returns is_valid=True."""
    result = validate_adaptive_brief(
        _make_brief(),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert result.is_valid is True
    assert result.errors == ()


# ---------------------------------------------------------------------------
# 2. Layer-3 — invented Strengthens reference
# ---------------------------------------------------------------------------


def test_signal_thread_invented_strengthens_fails() -> None:
    """A SIGNAL thread citing SA-TECH-99 (not in upstream) fails referentially."""
    bad = _signal_thread(strengthens=("SA-TECH-99",))
    result = validate_adaptive_brief(
        _make_brief(threads=(bad,)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert result.is_valid is False
    [err] = [e for e in result.errors if e.rule == "referential_integrity"]
    assert err.field_path == "threads[0].strengthens[0]"
    assert "SA-TECH-99" in err.message


def test_signal_thread_invented_weakens_fails() -> None:
    """A SIGNAL thread citing QR-99 in Weakens (not in upstream) fails referentially."""
    bad = _signal_thread(strengthens=("SA-TECH-1",), weakens=("QR-99",))
    result = validate_adaptive_brief(
        _make_brief(threads=(bad,)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert result.is_valid is False
    [err] = [e for e in result.errors if e.rule == "referential_integrity"]
    assert err.field_path == "threads[0].weakens[0]"
    assert "QR-99" in err.message


# ---------------------------------------------------------------------------
# 3. Layer-3 — NOISE / INCONCLUSIVE threads skipped (no referential check)
# ---------------------------------------------------------------------------


def test_noise_thread_no_referential_check() -> None:
    """A NOISE thread (strengthens=None) yields no referential-integrity error."""
    result = validate_adaptive_brief(
        _make_brief(threads=(_noise_thread("AR-1"),)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    refs = [e for e in result.errors if e.rule == "referential_integrity"]
    assert refs == []
    assert result.is_valid is True


def test_signal_thread_empty_strengthens_tuple_valid() -> None:
    """A SIGNAL thread with strengthens=() (explicit empty 'none') is valid."""
    result = validate_adaptive_brief(
        _make_brief(threads=(_signal_thread(strengthens=(), weakens=()),)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert result.is_valid is True
    assert result.errors == ()


def test_inconclusive_thread_no_referential_check() -> None:
    """An INCONCLUSIVE thread (strengthens=None) yields no referential-integrity error."""
    result = validate_adaptive_brief(
        _make_brief(threads=(_inconclusive_thread("AR-1"),)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    refs = [e for e in result.errors if e.rule == "referential_integrity"]
    assert refs == []
    assert result.is_valid is True


# ---------------------------------------------------------------------------
# 4. Layer-2 — duplicate findings/tools_used inside a thread
# ---------------------------------------------------------------------------


def test_duplicate_findings_within_thread_fails() -> None:
    """Two byte-identical findings entries in one thread fire findings_distinct."""
    bad = _signal_thread(findings=("dup line", "dup line"))
    result = validate_adaptive_brief(
        _make_brief(threads=(bad,)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "findings_distinct_within_thread" in rules


def test_duplicate_tools_used_within_thread_fails() -> None:
    """Two byte-identical tools_used entries in one thread fire tools_used_distinct."""
    bad = _signal_thread(tools_used=("news_search", "news_search"))
    result = validate_adaptive_brief(
        _make_brief(threads=(bad,)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "tools_used_distinct_within_thread" in rules


# ---------------------------------------------------------------------------
# 5. Layer-2 — non-sequential thread_id indexing
# ---------------------------------------------------------------------------


def test_non_sequential_thread_ids_fails() -> None:
    """AR-1, AR-3 (gap at 2) fires threads_sequential_indexing.

    The model's threads_investigated_count == len(threads) invariant only
    counts threads, not their IDs, so AR-1 + AR-3 with count=2 is constructible.
    """
    threads = (_signal_thread("AR-1"), _signal_thread("AR-3"))
    result = validate_adaptive_brief(
        _make_brief(threads=threads),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "threads_sequential_indexing" in rules
    paths = {e.field_path for e in result.errors}
    assert "threads[*].thread_id" in paths


# ---------------------------------------------------------------------------
# 6. Layer-2 — tickers_in_universe membership
# ---------------------------------------------------------------------------


def test_ticker_not_in_universe_fails() -> None:
    """When `universe` is supplied and a thread cites an out-of-universe ticker, fails."""
    bad = _signal_thread(tickers=("UNKNOWN",))
    result = validate_adaptive_brief(
        _make_brief(threads=(bad,)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
        universe=frozenset({"NVDA", "AAPL"}),
    )
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "tickers_in_universe" in rules


def test_ticker_universe_none_skips_check() -> None:
    """When `universe` is None, no membership check runs even on unknown tickers."""
    bad = _signal_thread(tickers=("UNKNOWN",))
    result = validate_adaptive_brief(
        _make_brief(threads=(bad,)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
        universe=None,
    )
    rules = {e.rule for e in result.errors}
    assert "tickers_in_universe" not in rules


# ---------------------------------------------------------------------------
# 7. _build_reference_universe — every six-family ID is included
# ---------------------------------------------------------------------------


def test_build_reference_universe_covers_all_six_families() -> None:
    """`_build_reference_universe` returns a frozenset[str] containing IDs from
    all six prefix families: SA-{SECTOR}-N, SA-{SECTOR}-ANOM-N, SA-{SECTOR}-TC-N,
    QR-N, QR-CW-N, CR-N.
    """
    from alphamind.analysis.adaptive_research.validation import _build_reference_universe

    valid = _build_reference_universe(
        _sector_briefs(),
        _qualitative_brief(),
        _correlation_brief(),
    )
    expected = frozenset(
        {
            # SA-{SECTOR}-N findings
            "SA-TECH-1",
            "SA-FIN-1",
            "SA-ENERGY-1",
            # SA-{SECTOR}-ANOM-N anomalies
            "SA-TECH-ANOM-1",
            "SA-FIN-ANOM-1",
            "SA-ENERGY-ANOM-1",
            # SA-{SECTOR}-TC-N thesis candidates
            "SA-TECH-TC-1",
            "SA-FIN-TC-1",
            "SA-ENERGY-TC-1",
            # QR-N narrative threads
            "QR-1",
            # QR-CW-N catalyst watches
            "QR-CW-1",
            # CR-N correlation/regime brief
            "CR-1",
            "CR-2",
        }
    )
    assert isinstance(valid, frozenset)
    assert valid == expected


# ---------------------------------------------------------------------------
# 8. Aggregation — all checks combine; is_valid <=> errors == ()
# ---------------------------------------------------------------------------


def test_multiple_errors_aggregate_into_result() -> None:
    """A brief that violates several checks accumulates all errors in result.errors.

    The fail-fast/fail-once policy applies at the harness level; the validator
    itself returns the *full* list so the diagnostic record sees the inventory.
    """
    bad = _signal_thread(
        findings=("dup", "dup"),
        tools_used=("t1", "t1"),
        strengthens=("SA-TECH-99",),
    )
    result = validate_adaptive_brief(
        _make_brief(threads=(bad,)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "findings_distinct_within_thread" in rules
    assert "tools_used_distinct_within_thread" in rules
    assert "referential_integrity" in rules


def test_is_valid_iff_errors_empty() -> None:
    """`is_valid=True` exactly when `errors=()`."""
    ok = validate_adaptive_brief(
        _make_brief(),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert ok.is_valid is True and ok.errors == ()

    bad = _signal_thread(strengthens=("MISSING",))
    not_ok = validate_adaptive_brief(
        _make_brief(threads=(bad,)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert not_ok.is_valid is False and not_ok.errors != ()


# ---------------------------------------------------------------------------
# 9. Verification — perturb each Strengthens/Weakens reference one at a time
# ---------------------------------------------------------------------------


def test_perturb_each_reference_produces_exactly_one_new_error() -> None:
    """Per the story's verification step: a fixture exercising every reference-ID
    family passes; perturbing each Strengthens/Weakens reference one at a time
    produces exactly one new ValidationError vs. the baseline.
    """
    # Baseline: a brief whose SIGNAL thread cites one ID from every family.
    references_under_test = (
        "SA-TECH-1",  # SA-{SECTOR}-N finding
        "SA-FIN-ANOM-1",  # SA-{SECTOR}-ANOM-N anomaly
        "SA-ENERGY-TC-1",  # SA-{SECTOR}-TC-N thesis candidate
        "QR-1",  # QR-N narrative thread
        "QR-CW-1",  # QR-CW-N catalyst watch
        "CR-2",  # CR-N correlation/regime brief
    )
    baseline_thread = _signal_thread(
        strengthens=references_under_test[:3],
        weakens=references_under_test[3:],
    )
    baseline = validate_adaptive_brief(
        _make_brief(threads=(baseline_thread,)),
        sector_briefs=_sector_briefs(),
        qualitative_brief=_qualitative_brief(),
        correlation_regime_brief=_correlation_brief(),
    )
    assert baseline.is_valid is True, baseline.errors
    baseline_count = len(baseline.errors)

    # Perturb each reference one at a time → exactly one new ValidationError.
    for index in range(len(references_under_test)):
        perturbed_refs = list(references_under_test)
        perturbed_refs[index] = perturbed_refs[index] + "-INVALID"
        perturbed_thread = _signal_thread(
            strengthens=tuple(perturbed_refs[:3]),
            weakens=tuple(perturbed_refs[3:]),
        )
        result = validate_adaptive_brief(
            _make_brief(threads=(perturbed_thread,)),
            sector_briefs=_sector_briefs(),
            qualitative_brief=_qualitative_brief(),
            correlation_regime_brief=_correlation_brief(),
        )
        assert result.is_valid is False, f"perturbation {index} should fail"
        delta = len(result.errors) - baseline_count
        assert delta == 1, (
            f"perturbation of reference {references_under_test[index]!r} (index {index}) "
            f"produced {delta} new errors; expected exactly 1"
        )
        # And the new error is a referential_integrity error pointing at the
        # perturbed slot.
        new_errors = [e for e in result.errors if e.rule == "referential_integrity"]
        assert len(new_errors) == 1
        if index < 3:
            assert new_errors[0].field_path == f"threads[0].strengthens[{index}]"
        else:
            assert new_errors[0].field_path == f"threads[0].weakens[{index - 3}]"
