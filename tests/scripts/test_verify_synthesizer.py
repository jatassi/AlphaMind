"""Unit tests for ``scripts/verify_synthesizer.py`` (ALP-211).

The real-SDK live verification script's testable logic — the
reference-coverage classifier and the verdict rubric — is exercised
here without touching the Anthropic API. The live SDK invocation is
verified manually by the operator running the script after this PR
lands.

Coverage map per ALP-211 § Tests:

- ``test_reference_coverage_classifier_correct`` — partitions cited
  reference IDs into resolved (present in the retrieval store) and
  invented (absent).
- ``test_verdict_pass_when_zero_invented`` — non-empty response, zero
  invented, end_turn → PASS.
- ``test_verdict_warn_when_invented_present_but_response_nonempty`` —
  non-empty response, ≥1 invented, end_turn → WARN.
- ``test_verdict_fail_when_harness_failure`` — any HarnessFailure → FAIL.
- ``test_missing_auth_renders_failure_report`` — CLI entry surfaces a
  missing CLAUDE_CODE_OAUTH_TOKEN as a clean failure-report block on
  stdout with exit code 1, rather than a stack trace.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.harness import (
    ContextOverflowFailure,
    EmptyResponseFailure,
    HarnessFailure,
    SDKFailure,
    TimeoutFailure,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.analysis.synthesizer.runner import SynthesizerResult
from alphamind.config.models.agents import AgentName
from alphamind.scripts.verify_synthesizer import (
    Verdict,
    classify_reference_coverage,
    main,
    verdict_for_failure,
    verdict_for_success,
)

_INVOCATION_ID = "20260503T120000Z-verify-synthesizer"
_AGENT_NAME = AgentName.synthesizer.value
_NOW = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_retrieval_store(ref_ids: tuple[str, ...]) -> RetrievalStore:
    """Build a RetrievalStore populated with stub section text for ``ref_ids``."""
    return RetrievalStore(
        entries={ref_id: f"[{ref_id}] stub.\n" for ref_id in ref_ids},
        freshness_by_source={},
    )


def _make_synthesizer_result(
    *,
    synthesis_text: str = "synthesis prose.",
    stop_reason: str = "end_turn",
    retrieval_store: RetrievalStore | None = None,
) -> SynthesizerResult:
    return SynthesizerResult(
        synthesis_text=synthesis_text,
        retrieval_store=retrieval_store or _make_retrieval_store(()),
        tokens_used=TokensUsed(
            input_tokens=10_000,
            output_tokens=1_500,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        tool_calls_used=2,
        wall_clock_seconds=12.3,
        stop_reason=stop_reason,
    )


# ---------------------------------------------------------------------------
# Reference-coverage classifier
# ---------------------------------------------------------------------------


def test_reference_coverage_classifier_correct() -> None:
    """The classifier partitions cited refs into resolved vs invented.

    Given a synthesis text that cites a mix of valid and invented
    reference IDs and a retrieval store keyed by the valid IDs only,
    the classifier returns the cited set, the resolved subset, and
    the invented complement — each ID appearing exactly once.
    """
    store = _make_retrieval_store(
        ("SA-TECH-1", "SA-TECH-3", "CR-1", "QR-2", "QR-CW-1", "AR-1", "AR-2")
    )
    synthesis_text = (
        "The macro frame [CR-1] reinforces the tech-flow signal [SA-TECH-1] and "
        "the catalyst [QR-CW-1]. The contradicting flow note [SA-TECH-3] is "
        "moderated by [QR-2]. Adaptive research [AR-1] strengthens [SA-TECH-1] "
        "while [AR-2] dismisses noise. An invented reference [SA-FIN-99] should "
        "be flagged. Repeated [CR-1] should not double-count."
    )

    coverage = classify_reference_coverage(synthesis_text, store)

    assert coverage.cited == frozenset(
        {"CR-1", "SA-TECH-1", "QR-CW-1", "SA-TECH-3", "QR-2", "AR-1", "AR-2", "SA-FIN-99"}
    )
    assert coverage.resolved == frozenset(
        {"CR-1", "SA-TECH-1", "QR-CW-1", "SA-TECH-3", "QR-2", "AR-1", "AR-2"}
    )
    assert coverage.invented == frozenset({"SA-FIN-99"})


def test_reference_coverage_classifier_flags_bare_prefix() -> None:
    """Bare-prefix citations land in ``invented`` (ALP-290).

    The synthesizer prompt forbids `[CR]`, `[SA-TECH]`, and other
    bracketed-prefix-without-index forms; the verifier's classifier must
    surface them as contract violations so the verdict rubric WARNs
    rather than passing silently. The 2026-05-04 E2E run produced four
    bare `[CR]` references and the original regex (which required the
    trailing index) treated them as unfalsifiable prose.
    """
    store = _make_retrieval_store(("CR-1", "CR-3", "CR-4", "SA-TECH-2"))
    synthesis_text = (
        "Correlation divergences (2.30 [CR]) and (2.29 [CR]) confirm META "
        "broke from peer group. INTC:MRVL (2.40 [CR]) and a paired "
        "AMZN:DDOG (2.38 [CR]) point in the same direction. The valid "
        "[CR-1], [CR-3], and [CR-4] anchor the broader frame; [SA-TECH-2] "
        "corroborates from the sector vantage."
    )

    coverage = classify_reference_coverage(synthesis_text, store)

    # The bare `CR` lands in `cited` and `invented` (deduplicated to a
    # single token regardless of how many times it appears in prose).
    assert "CR" in coverage.cited
    assert "CR" in coverage.invented
    assert "CR" not in coverage.resolved
    # The well-formed citations resolve normally.
    assert {"CR-1", "CR-3", "CR-4", "SA-TECH-2"}.issubset(coverage.resolved)


def test_reference_coverage_classifier_ignores_non_prefix_brackets() -> None:
    """Bracketed tokens that are not synthesizer prefixes are ignored.

    `[note]` (lowercase) does not match the regex; `[FOO]` matches the
    regex but is not a known synthesizer prefix, so the classifier
    drops it. The carve-out keeps the violation surface scoped to
    actual prefix-shaped citations rather than every bracketed string.
    """
    store = _make_retrieval_store(("CR-1",))
    synthesis_text = (
        "The macro frame [CR-1] anchors the read. A footnote [note] and an "
        "unknown bracket [FOO] should not contaminate the cited set."
    )

    coverage = classify_reference_coverage(synthesis_text, store)

    assert coverage.cited == frozenset({"CR-1"})
    assert coverage.invented == frozenset()


def test_verdict_warn_on_bare_prefix_citation() -> None:
    """A response containing only a bare `[CR]` produces WARN, not PASS (ALP-290).

    Reproduces the gap in the 2026-05-04 E2E run where the runbook
    reported PASS while the synthesizer output contained four bare
    `[CR]` references. After the regex fix, such a response routes
    through the WARN branch of the verdict rubric.
    """
    store = _make_retrieval_store(("CR-1",))
    result = _make_synthesizer_result(
        synthesis_text="Correlation divergence (2.30 [CR]) sits against [CR-1].",
        stop_reason="end_turn",
        retrieval_store=store,
    )
    coverage = classify_reference_coverage(result.synthesis_text, result.retrieval_store)

    verdict = verdict_for_success(result, coverage)

    assert verdict is Verdict.WARN
    assert "CR" in coverage.invented


# ---------------------------------------------------------------------------
# Verdict rubric
# ---------------------------------------------------------------------------


def test_verdict_pass_when_zero_invented() -> None:
    """Non-empty response, zero invented, end_turn → PASS."""
    store = _make_retrieval_store(("CR-1", "SA-TECH-1"))
    result = _make_synthesizer_result(
        synthesis_text="Body cites [CR-1] and [SA-TECH-1].",
        stop_reason="end_turn",
        retrieval_store=store,
    )
    coverage = classify_reference_coverage(result.synthesis_text, result.retrieval_store)

    verdict = verdict_for_success(result, coverage)

    assert verdict is Verdict.PASS


def test_verdict_pass_when_zero_invented_max_tokens() -> None:
    """Non-empty response with stop_reason=max_tokens still passes if no inventions."""
    store = _make_retrieval_store(("CR-1",))
    result = _make_synthesizer_result(
        synthesis_text="Body cites [CR-1] and was truncated.",
        stop_reason="max_tokens",
        retrieval_store=store,
    )
    coverage = classify_reference_coverage(result.synthesis_text, result.retrieval_store)

    verdict = verdict_for_success(result, coverage)

    assert verdict is Verdict.PASS


def test_verdict_warn_when_invented_present_but_response_nonempty() -> None:
    """Non-empty response, ≥1 invented, end_turn → WARN."""
    store = _make_retrieval_store(("CR-1",))
    result = _make_synthesizer_result(
        synthesis_text="Body cites [CR-1] and an invented [SA-FIN-99].",
        stop_reason="end_turn",
        retrieval_store=store,
    )
    coverage = classify_reference_coverage(result.synthesis_text, result.retrieval_store)

    verdict = verdict_for_success(result, coverage)

    assert verdict is Verdict.WARN


@pytest.mark.parametrize(
    "failure",
    [
        EmptyResponseFailure(
            "empty",
            agent_name=_AGENT_NAME,
            invocation_id=_INVOCATION_ID,
        ),
        ContextOverflowFailure(
            "max_tokens",
            agent_name=_AGENT_NAME,
            invocation_id=_INVOCATION_ID,
        ),
        SDKFailure(
            "sdk",
            agent_name=_AGENT_NAME,
            invocation_id=_INVOCATION_ID,
        ),
        TimeoutFailure(
            "latency",
            agent_name=_AGENT_NAME,
            invocation_id=_INVOCATION_ID,
        ),
    ],
)
def test_verdict_fail_when_harness_failure(failure: HarnessFailure) -> None:
    """Any HarnessFailure subclass classifies as FAIL."""
    assert verdict_for_failure(failure) is Verdict.FAIL


# ---------------------------------------------------------------------------
# Missing auth
# ---------------------------------------------------------------------------


def test_missing_auth_renders_failure_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing CLAUDE_CODE_OAUTH_TOKEN renders a clean failure-report block.

    The script must fail fast and route the missing-token case through
    the same ``_render_failure_report`` path as a runtime harness
    failure: a clean operator-facing report on stdout and exit code 1,
    never a stack trace from inside the SDK or harness CLI bridge.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    exit_code = main(argv=[])

    assert exit_code == 1
    captured = capsys.readouterr().out
    assert "Synthesizer live-SDK verification" in captured
    assert "Harness failure" in captured
    assert "type: SDKFailure" in captured
    assert f"agent_name: {_AGENT_NAME}" in captured
    assert "CLAUDE_CODE_OAUTH_TOKEN" in captured
    assert "Verdict: FAIL" in captured
