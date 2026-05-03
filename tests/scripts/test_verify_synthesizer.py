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
- ``test_missing_auth_raises_sdk_failure`` — CLI entry surfaces a
  missing CLAUDE_CODE_OAUTH_TOKEN as a clean SDKFailure rather than a
  stack trace.
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


def test_missing_auth_raises_sdk_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing CLAUDE_CODE_OAUTH_TOKEN surfaces as a clean SDKFailure.

    The script must fail fast with the documented exception type rather
    than crashing inside the SDK or surfacing a stack trace from the
    harness's CLI-connection error path.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    with pytest.raises(SDKFailure) as exc_info:
        main(argv=[])

    assert exc_info.value.agent_name == _AGENT_NAME
    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc_info.value)
