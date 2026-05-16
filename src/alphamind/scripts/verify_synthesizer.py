"""Synthesizer end-to-end live-SDK verification (ALP-211).

Operator entry point: build a canonical fixture set spanning all six
upstream brief sources (three sector briefs, the correlation/regime
brief, the qualitative brief, the adaptive brief), wire an inline stub
:class:`SynthesizerPortfolioStateReader`, load the synthesizer's
:class:`BaseAgentConfig` from ``config/agents.yaml``, and call
:func:`alphamind.analysis.synthesizer.runner.run_synthesizer` against
the real Claude Agent SDK.

Two reporting paths:

- The runner returns a :class:`SynthesizerResult` → the script prints
  the full operator report (token usage, tool-call breakdown, synthesis
  text, reference-coverage analysis), classifies the result via
  :func:`verdict_for_success` (PASS / WARN), and exits 0.
- The runner raises a :class:`HarnessFailure` → the script prints the
  failure block, classifies the result via :func:`verdict_for_failure`
  (always FAIL), and exits non-zero.

Missing ``CLAUDE_CODE_OAUTH_TOKEN`` surfaces as a clean :class:`SDKFailure`
rather than a stack trace from inside the SDK's CLI bridge.

The reference-coverage classifier and the verdict rubric are unit-tested
under ``tests/scripts/test_verify_synthesizer.py``; the live-SDK path
is verified manually by the operator.
"""

from __future__ import annotations

import argparse
import asyncio
import enum
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alphamind._kernel.ids import PositionId, Symbol
from alphamind.analysis._shared import Sector, SignalQuality
from alphamind.analysis.adaptive_research.models import (
    AdaptiveBrief,
    Assessment,
    Confidence,
    InvestigationThread,
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
from alphamind.analysis.synthesizer.harness import HarnessFailure, SDKFailure
from alphamind.analysis.synthesizer.models import ReferencePrefix, parse_reference_id
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.analysis.synthesizer.runner import (
    SynthesizerResult,
    load_synthesizer_agent_config,
    run_synthesizer,
)
from alphamind.config.models.agents import (
    AgentName,
    BaseAgentConfig,
)
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.portfolio_state.consumers.synthesizer import (
    SynthesizerExposureSnapshot,
    SynthesizerPortfolioStateReader,
    SynthesizerPositionSummary,
    SynthesizerThesisSummary,
)
from alphamind.portfolio_state.records.positions import Direction as PositionDirection
from alphamind.scripts._artifact_io import (
    dump_retrieval_store,
    load_adaptive_brief,
    load_correlation_regime_brief,
    load_qualitative_brief,
    load_sector_briefs,
    load_universal_regime_label,
    stage_artifacts_dir,
)
from alphamind.scripts._stdio import configure_utf8_stdio

__all__ = [
    "ReferenceCoverage",
    "Verdict",
    "build_fixture_adaptive_brief",
    "build_fixture_correlation_regime_brief",
    "build_fixture_portfolio_reader",
    "build_fixture_qualitative_brief",
    "build_fixture_sector_briefs",
    "classify_reference_coverage",
    "main",
    "verdict_for_failure",
    "verdict_for_success",
]


_AGENT_NAME = AgentName.synthesizer.value


# ---------------------------------------------------------------------------
# Verdict rubric
# ---------------------------------------------------------------------------


class Verdict(enum.StrEnum):
    """Verdict label printed in the operator report and mapped to exit code.

    PASS and WARN both exit 0 — WARN is a prompt-tightening signal, not a
    failure. FAIL exits non-zero (any :class:`HarnessFailure` subclass).
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


# ---------------------------------------------------------------------------
# Reference-coverage analysis
# ---------------------------------------------------------------------------


# Reference-ID literal in the synthesis text: an uppercase prefix of one or
# more `-`-joined segments, optionally hyphen-bound to a positive integer
# index. The `[` / `]` brackets bound the citation form the synthesizer
# prompt requires. The trailing index is optional in the regex so the
# classifier can also surface bare-prefix forms (`[CR]`, `[SA-TECH]`) as
# contract violations — see `classify_reference_coverage` for the partition
# rule.
_CITATION_RE = re.compile(r"\[([A-Z]+(?:-[A-Z]+)*(?:-\d+)?)\]")

# Known synthesizer-side reference prefixes — the set the synthesizer can
# legally cite. A bracketed token whose body equals one of these but carries
# no index is the bare-prefix-citation contract violation ALP-290 surfaced.
_KNOWN_PREFIXES: frozenset[str] = frozenset(p.value for p in ReferencePrefix)


@dataclass(frozen=True)
class ReferenceCoverage:
    """Reference-coverage breakdown for one synthesis-text invocation.

    ``cited`` is the union of every citation parsed out of the synthesis
    text; ``resolved`` are those present as keys in the per-invocation
    retrieval store; ``invented`` are the complement (cited but absent
    from the store — the prompt-tightening signal).
    """

    cited: frozenset[str]
    resolved: frozenset[str]
    invented: frozenset[str]


def classify_reference_coverage(synthesis_text: str, store: RetrievalStore) -> ReferenceCoverage:
    """Partition the citations in *synthesis_text* against *store*.

    Extracts every bracketed reference-shaped token, drops bracketed prose
    like ``[note]`` (lowercase, never matches the regex) and unknown
    uppercase tokens like ``[FOO]`` (not a synthesizer-side prefix), then
    partitions the survivors into resolved (present as a key in
    ``store.entries``) and invented (absent).

    Bare-prefix forms — ``[CR]``, ``[SA-TECH]``, ``[QR]`` — are surfaced
    in ``cited`` whenever the body equals a known synthesizer prefix
    (ALP-290). Such forms always land in ``invented`` because the
    retrieval store keys are always indexed (``CR-1``, ``SA-TECH-3``, …);
    a bare prefix can never resolve, and the verdict rubric correctly
    treats the violation as a prompt-tightening signal (WARN).
    """
    cited: set[str] = set()
    for match in _CITATION_RE.finditer(synthesis_text):
        ref_id = match.group(1)
        if parse_reference_id(ref_id) is not None or ref_id in _KNOWN_PREFIXES:
            cited.add(ref_id)
    resolved = {ref_id for ref_id in cited if ref_id in store.entries}
    invented = cited - resolved
    return ReferenceCoverage(
        cited=frozenset(cited),
        resolved=frozenset(resolved),
        invented=frozenset(invented),
    )


def verdict_for_success(result: SynthesizerResult, coverage: ReferenceCoverage) -> Verdict:
    """Apply the success-path rubric.

    PASS: response non-empty, zero invented references.
    WARN: response non-empty, ≥1 invented reference (prompt-tightening
    signal — the live-SDK call still produced usable prose).

    The harness's stop-reason check already rejects empty responses
    before the runner returns success, so any :class:`SynthesizerResult`
    delivered here carries non-empty text. ``stop_reason`` is permitted
    to be either ``end_turn`` or ``max_tokens`` per the rubric.
    """
    del result  # success path: response is non-empty by harness contract.
    if coverage.invented:
        return Verdict.WARN
    return Verdict.PASS


def verdict_for_failure(failure: HarnessFailure) -> Verdict:
    """Apply the failure-path rubric: any HarnessFailure → FAIL."""
    del failure
    return Verdict.FAIL


# ---------------------------------------------------------------------------
# Canonical-fixture builders
# ---------------------------------------------------------------------------


def build_fixture_sector_briefs(invocation_id: str) -> tuple[SectorBrief, ...]:
    """Three sector briefs covering tech-semis, financials, energy.

    Each brief carries one finding, one anomaly, and one thesis candidate
    so the retrieval store sees the full ``SA-TECH-N`` / ``SA-TECH-ANOM-N``
    / ``SA-TECH-TC-N`` sub-typed prefix family per sector.
    """

    def _brief(sector: Sector, prefix: str, ticker: str) -> SectorBrief:
        return SectorBrief(
            invocation_id=invocation_id,
            sector=sector,
            signal_quality=SignalQuality.HIGH,
            signal_quality_reason=None,
            findings=(
                Finding(
                    finding_id=f"{prefix}-1",
                    headline=f"{ticker} unusual volume vs. peers.",
                    tickers=(ticker,),
                    signal_type=SignalType.FLOW,
                    strength=Strength.STRONG,
                    detail=f"{ticker} spent the session printing 2.4x its 30-day average volume.",
                ),
                Finding(
                    finding_id=f"{prefix}-3",
                    headline=f"{sector.value} relative strength vs. SPY.",
                    tickers=(ticker,),
                    signal_type=SignalType.PRICE_ACTION,
                    strength=Strength.MODERATE,
                    detail=f"{sector.value} held the open while SPY faded.",
                ),
            ),
            anomalies=(
                Anomaly(
                    anomaly_id=f"{prefix}-ANOM-1",
                    description=f"{ticker} options flow tilted heavily call-side.",
                    anomaly_type=AnomalyType.OPTIONS_SKEW,
                    tickers=(ticker,),
                    severity="investigate_now",
                    suggested_question="What does the news cycle say?",
                ),
            ),
            thesis_candidates=(
                ThesisCandidate(
                    thesis_candidate_id=f"{prefix}-TC-1",
                    ticker=ticker,
                    direction=Direction.LONG,
                    setup_type=SetupType.MOMENTUM,
                    catalyst="Earnings within 24h.",
                    time_horizon_hours="24-72h",
                    conviction_sketch=ConvictionSketch.MODERATE,
                    conviction_justification="Volume + relative strength.",
                    key_risk="Earnings miss reverses thesis.",
                ),
            ),
        )

    return (
        _brief(Sector.TECH_SEMIS, "SA-TECH", "NVDA"),
        _brief(Sector.FINANCIALS, "SA-FIN", "JPM"),
        _brief(Sector.ENERGY, "SA-ENERGY", "XOM"),
    )


def build_fixture_correlation_regime_brief(now_utc: datetime) -> CorrelationRegimeBrief:
    """A correlation/regime brief with two CR-N references and a regime label."""
    text = (
        "[CR-1] Cross-asset framing.\n"
        "  Tech-semis decoupled from broader equities through the session;\n"
        "  rates remained tightly bound to dollar.\n"
        "[CR-2] Intra-sector divergence.\n"
        "  Financials lagged tech by 60bps despite shared duration sensitivity.\n"
    )
    return CorrelationRegimeBrief(
        text=text,
        reference_index={"CR-1": "regime.cross_asset", "CR-2": "regime.intra_sector"},
        freshness_min=now_utc - timedelta(minutes=15),
    )


def build_fixture_qualitative_brief(invocation_id: str) -> QualitativeBrief:
    """A qualitative brief carrying narrative threads and a catalyst watch."""
    return QualitativeBrief(
        invocation_id=invocation_id,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(
            NarrativeThread(
                thread_id="QR-1",
                summary="Hawkish rates narrative re-asserted after FOMC minutes.",
                relevance="cross-sector",
                direction=ThreadDirection.BEARISH,
                subject="rate-sensitive tech",
                time_horizon=TimeHorizon.NEAR_TERM,
                evidence=(
                    EvidenceLine(
                        source_type="news_digest",
                        observation="FOMC minutes signaled hold-longer stance.",
                        citation="ND-T1",
                    ),
                    EvidenceLine(
                        source_type="prediction_market",
                        observation="Hold-rate odds +12pp on Kalshi.",
                        citation="kalshi:fomc-hold",
                    ),
                ),
                implication="Pressure on duration-heavy growth names.",
            ),
            NarrativeThread(
                thread_id="QR-2",
                summary="Energy supply-side tightness.",
                relevance="energy",
                direction=ThreadDirection.BULLISH,
                subject="integrated energy majors",
                time_horizon=TimeHorizon.DEVELOPING,
                evidence=(
                    EvidenceLine(
                        source_type="news_digest",
                        observation="OPEC+ extended production cuts.",
                        citation="ND-E1",
                    ),
                    EvidenceLine(
                        source_type="prediction_market",
                        observation="Brent prediction-market odds tightened.",
                        citation="kalshi:brent-q3",
                    ),
                ),
                implication="Supportive for XOM-like cash-flow names.",
            ),
        ),
        catalyst_watches=(
            CatalystWatch(
                catalyst_id="QR-CW-1",
                ticker=Symbol("NVDA"),
                catalyst_name="Q1 earnings",
                hours_to_event=18,
                thesis_impact="Direct test of the SA-TECH-TC-1 momentum setup.",
            ),
        ),
        sentiment_snapshot=SentimentSnapshot(
            extremes="none",
            divergences="none",
            regime="risk-on with rates concern",
        ),
    )


def build_fixture_adaptive_brief(invocation_id: str) -> AdaptiveBrief:
    """An adaptive brief with two investigation threads, each cross-referenced."""
    return AdaptiveBrief(
        invocation_id=invocation_id,
        threads_investigated_count=2,
        anomalies_triaged_count=2,
        anomalies_deferred=(),
        threads=(
            InvestigationThread(
                thread_id="AR-1",
                trigger="[SA-TECH-ANOM-1]",
                question="What drove the call-side options flow?",
                tickers=("NVDA",),
                sector=Sector.TECH_SEMIS,
                tools_used=("news_search",),
                findings=("Pre-earnings positioning.",),
                assessment=Assessment.SIGNAL,
                confidence=Confidence.MODERATE,
                implication="Upside skew confirmed.",
                strengthens=("SA-TECH-1",),
                weakens=(),
            ),
            InvestigationThread(
                thread_id="AR-2",
                trigger="[SA-FIN-ANOM-1]",
                question="Is the financials options flow noise?",
                tickers=("JPM",),
                sector=Sector.FINANCIALS,
                tools_used=("news_search",),
                findings=("No catalyst within window.",),
                assessment=Assessment.NOISE,
                confidence=Confidence.MODERATE,
                dismissal_reason="Background-rate level activity.",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Stub portfolio reader
# ---------------------------------------------------------------------------


class _FixturePortfolioReader:
    """Inline ``SynthesizerPortfolioStateReader`` stub returning fixture data.

    The fixture portfolio holds two NVDA-related positions and one
    associated thesis so the synthesizer's portfolio-state tools have
    realistic-shaped data to read when wiring contradictions and
    intersections back to the live book.
    """

    def __init__(self) -> None:
        self._positions: tuple[SynthesizerPositionSummary, ...] = (
            SynthesizerPositionSummary(
                ticker=Symbol("NVDA"),
                direction=PositionDirection.LONG,
                sector="tech_semis",
                size_pct=4.5,
                position_age_hours=36.0,
            ),
            SynthesizerPositionSummary(
                ticker=Symbol("JPM"),
                direction=PositionDirection.LONG,
                sector="financials",
                size_pct=2.0,
                position_age_hours=120.0,
            ),
        )
        self._theses: tuple[SynthesizerThesisSummary, ...] = (
            SynthesizerThesisSummary(
                position_id=PositionId("pos-nvda-001"),
                ticker=Symbol("NVDA"),
                summary="AI-capex acceleration via hyperscaler reads.",
                key_catalyst="Q1 earnings",
                time_expectation_hours=48.0,
            ),
        )
        self._exposure: SynthesizerExposureSnapshot = SynthesizerExposureSnapshot(
            sector_exposure_pct={"tech_semis": 4.5, "financials": 2.0},
            net_directional_pct=6.5,
            gross_exposure_pct=6.5,
        )

    def get_positions_summary(self) -> tuple[SynthesizerPositionSummary, ...]:
        return self._positions

    def get_active_theses_summary(self) -> tuple[SynthesizerThesisSummary, ...]:
        return self._theses

    def get_exposure_snapshot(self) -> SynthesizerExposureSnapshot:
        return self._exposure


def build_fixture_portfolio_reader() -> SynthesizerPortfolioStateReader:
    """Public factory mirroring the other ``build_fixture_*`` shapes."""
    return _FixturePortfolioReader()


# ---------------------------------------------------------------------------
# Operator-report rendering
# ---------------------------------------------------------------------------


_BANNER = "=== Synthesizer live-SDK verification ==="


def _render_success_report(
    *,
    invocation_id: str,
    agent_config: BaseAgentConfig,
    result: SynthesizerResult,
    coverage: ReferenceCoverage,
    verdict: Verdict,
) -> str:
    """Render the operator report block for a successful runner return."""
    cited_sorted = sorted(coverage.cited)
    invented_sorted = sorted(coverage.invented)
    cited_render = ", ".join(cited_sorted) if cited_sorted else "(none)"
    invented_render = ", ".join(invented_sorted) if invented_sorted else "(none)"
    lines = [
        _BANNER,
        f"invocation_id: {invocation_id}",
        f"model: {agent_config.model.value}",
        f"wall_clock: {result.wall_clock_seconds:.2f}s",
        (
            f"tokens: input={result.tokens_used.input_tokens} "
            f"output={result.tokens_used.output_tokens} "
            f"cache_read={result.tokens_used.cache_read_tokens} "
            f"cache_write={result.tokens_used.cache_write_tokens}"
        ),
        f"tool_calls: {result.tool_calls_used}",
        f"stop_reason: {result.stop_reason}",
        "",
        "--- Synthesis text ---",
        result.synthesis_text,
        "",
        "--- Reference-coverage analysis ---",
        f"Cited references: {len(cited_sorted)} ({cited_render})",
        f"Resolved against retrieval store: {len(coverage.resolved)}",
        f"Invented references: {len(invented_sorted)} ({invented_render})",
        "",
        f"--- Verdict: {verdict.value} ---",
    ]
    return "\n".join(lines) + "\n"


def _render_failure_report(
    *,
    invocation_id: str,
    agent_config: BaseAgentConfig,
    failure: HarnessFailure,
    verdict: Verdict,
) -> str:
    """Render the operator report block when the runner raises."""
    lines = [
        _BANNER,
        f"invocation_id: {invocation_id}",
        f"model: {agent_config.model.value}",
        "",
        "--- Harness failure ---",
        f"type: {type(failure).__name__}",
        f"agent_name: {failure.agent_name}",
        f"invocation_id: {failure.invocation_id}",
        f"message: {failure}",
        "",
        f"--- Verdict: {verdict.value} ---",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Auth check
# ---------------------------------------------------------------------------


def _check_oauth_token_set(invocation_id: str) -> None:
    """Surface missing CLAUDE_CODE_OAUTH_TOKEN as a clean SDKFailure."""
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise SDKFailure(
            "CLAUDE_CODE_OAUTH_TOKEN is not set in the environment. The "
            "real-SDK synthesizer verification cannot run without it. "
            "Generate a token via `claude setup-token` per "
            "docs/architecture/llm-integration.md § Authentication, then re-run.",
            agent_name=_AGENT_NAME,
            invocation_id=invocation_id,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _format_invocation_id(now: datetime) -> str:
    """``YYYYMMDDTHHMMSSZ-verify-synthesizer``."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-verify-synthesizer"


def _parse_iso8601(value: str) -> datetime:
    """Parse an ISO-8601 timestamp accepting either ``Z`` or explicit offset."""
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the exit code (0 on PASS/WARN, 1 on FAIL).

    A missing ``CLAUDE_CODE_OAUTH_TOKEN`` is reported via the same
    failure-report rendering path as a runtime :class:`HarnessFailure` —
    the operator sees a clean failure block and exit code 1, never a
    bare stack trace.
    """
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description=(
            "Run the synthesizer end-to-end against a real "
            "CLAUDE_CODE_OAUTH_TOKEN and report verdict + reference coverage."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--invocation-id",
        type=str,
        default=None,
        help=(
            "Override the invocation_id used for the diagnostic archive layout. "
            "Defaults to YYYYMMDDTHHMMSSZ-verify-synthesizer."
        ),
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help=("ISO-8601 timestamp for the current market snapshot. Defaults to now (UTC)."),
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=None,
        help=(
            "Path to the invocation-archive root. Defaults to "
            "skipping the diagnostic write (the live-SDK script does not "
            "need an archive — pass an explicit path to keep one)."
        ),
    )
    parser.add_argument(
        "--upstream-from",
        type=Path,
        default=None,
        help=(
            "Path to a stage-artifacts directory produced by earlier phases. "
            "When supplied, the six-way upstream-brief tuple (sector_briefs, "
            "correlation_regime_brief, qualitative_brief, adaptive_brief, "
            "universal_regime_label) is loaded from this directory instead of "
            "the in-script fixture builders — proving today's actual upstream "
            "artifacts flow through the synthesizer."
        ),
    )
    args = parser.parse_args(argv)

    now = datetime.now(tz=UTC)
    as_of = _parse_iso8601(args.as_of) if args.as_of else now
    invocation_id = args.invocation_id or _format_invocation_id(now)

    # Load the agent config first so the pre-flight failure path has the same
    # `agent_config` shape available for `_render_failure_report` as the
    # harness-failure path does.
    agent_config = load_synthesizer_agent_config()

    try:
        _check_oauth_token_set(invocation_id)
        if args.upstream_from is not None:
            sector_briefs = load_sector_briefs(args.upstream_from)
            correlation_regime_brief = load_correlation_regime_brief(args.upstream_from)
            qualitative_brief = load_qualitative_brief(args.upstream_from)
            adaptive_brief = load_adaptive_brief(args.upstream_from)
            regime_label_payload = load_universal_regime_label(args.upstream_from)
            regime_label = str(regime_label_payload.get("regime_label", ""))
            upstream_source = f"stage artifacts at {args.upstream_from}"
        else:
            sector_briefs = build_fixture_sector_briefs(invocation_id)
            correlation_regime_brief = build_fixture_correlation_regime_brief(as_of)
            qualitative_brief = build_fixture_qualitative_brief(invocation_id)
            adaptive_brief = build_fixture_adaptive_brief(invocation_id)
            regime_label = "vol_expansion"
            upstream_source = "in-script fixture builders"
        portfolio_reader = build_fixture_portfolio_reader()
        # Announce SDK invocation up front so the operator sees activity rather
        # than 10-30s of silence between script start and the SDK return.
        print(
            f"=== Synthesizer live-SDK verification: "
            f"invocation_id={invocation_id}, model={agent_config.model.value} ===\n"
            f"Upstream: {upstream_source}\n"
            "Invoking SDK...",
            flush=True,
        )
        result = asyncio.run(
            run_synthesizer(
                regime_label=regime_label,
                sector_briefs=sector_briefs,
                correlation_regime_brief=correlation_regime_brief,
                qualitative_brief=qualitative_brief,
                adaptive_brief=adaptive_brief,
                portfolio_reader=portfolio_reader,
                invocation_id=invocation_id,
                now_utc=as_of,
                archive_root=args.archive_root,
            )
        )
    except HarnessFailure as failure:
        verdict = verdict_for_failure(failure)
        print(
            _render_failure_report(
                invocation_id=invocation_id,
                agent_config=agent_config,
                failure=failure,
                verdict=verdict,
            )
        )
        return 1

    coverage = classify_reference_coverage(result.synthesis_text, result.retrieval_store)
    verdict = verdict_for_success(result, coverage)
    print(
        _render_success_report(
            invocation_id=invocation_id,
            agent_config=agent_config,
            result=result,
            coverage=coverage,
            verdict=verdict,
        )
    )
    # Write the retrieval store on PASS *and* WARN — WARN means the synthesizer
    # produced usable prose with a few invented references; the store still
    # carries the resolved ones and is useful to any decision-layer follow-up.
    # FAIL means the harness raised before producing a result, so there's
    # nothing to dump. The synthesizer's archive is opt-in (no default), so
    # operators running ad hoc without --archive-root skip the dump.
    if verdict is not Verdict.FAIL and args.archive_root is not None:
        stage_dir = stage_artifacts_dir(args.archive_root, invocation_id)
        dump_retrieval_store(result.retrieval_store, stage_dir)
        print(f"[verify_synthesizer] stage artifacts written to {stage_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
