"""Per-upstream brief → :class:`BriefBundle` adapters — story 05a (ALP-203).

Renders each upstream's native value object into the canonical
``[<prefix>-<index>]`` text layout the synthesizer expects, then wraps
the rendered text in a :class:`BriefBundle`. Rendering lives here, not on
the upstream models, so the upstream layer remains pure data and the
text-layout contract is co-located with the rest of the synthesizer's
code. See ``docs/design/03-analysis-layer/synthesizer.md`` § Inputs and
``docs/design/03-analysis-layer/domain-researchers/tech-semis.md`` § Domain
researcher output contract for the canonical layouts.
"""

from __future__ import annotations

from datetime import datetime

from alphamind.analysis.adaptive_research.models import (
    AdaptiveBrief,
    Assessment,
    InvestigationThread,
)
from alphamind.analysis.domain_researchers.models import (
    Anomaly,
    Finding,
    SectorBrief,
    ThesisCandidate,
)
from alphamind.analysis.qualitative_research.models import (
    TIME_HORIZON_DISPLAY,
    CatalystWatch,
    NarrativeThread,
    QualitativeBrief,
)
from alphamind.analysis.synthesizer.models import BriefBundle, BriefSource
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief

__all__ = [
    "adaptive_brief_to_bundle",
    "correlation_regime_brief_to_bundle",
    "qualitative_brief_to_bundle",
    "sector_brief_to_bundle",
]


# ---------------------------------------------------------------------------
# Sector adapter
# ---------------------------------------------------------------------------


def _render_finding(finding: Finding) -> str:
    """Render a :class:`Finding` to one section of the canonical layout."""
    return (
        f"[{finding.finding_id}] {finding.headline}\n"
        f"  Tickers: {', '.join(finding.tickers)}\n"
        f"  Signal type: {finding.signal_type.value}\n"
        f"  Strength: {finding.strength.value}\n"
        f"  Detail: {finding.detail}\n"
    )


def _render_anomaly(anomaly: Anomaly) -> str:
    """Render an :class:`Anomaly` to one section of the canonical layout."""
    return (
        f"[{anomaly.anomaly_id}] {anomaly.description}\n"
        f"  Anomaly type: {anomaly.anomaly_type.value}\n"
        f"  Tickers: {', '.join(anomaly.tickers)}\n"
        f"  Severity: {anomaly.severity}\n"
        f"  Suggested question: {anomaly.suggested_question}\n"
    )


def _render_thesis_candidate(tc: ThesisCandidate) -> str:
    """Render a :class:`ThesisCandidate` to one section of the canonical layout."""
    return (
        f"[{tc.thesis_candidate_id}]\n"
        f"  Ticker: {tc.ticker}\n"
        f"  Direction: {tc.direction.value}\n"
        f"  Setup type: {tc.setup_type.value}\n"
        f"  Catalyst: {tc.catalyst}\n"
        f"  Time horizon: {tc.time_horizon_hours}\n"
        f"  Conviction: {tc.conviction_sketch.value} — {tc.conviction_justification}\n"
        f"  Key risk: {tc.key_risk}\n"
    )


def sector_brief_to_bundle(brief: SectorBrief, freshness: datetime) -> BriefBundle:
    """Render a :class:`SectorBrief` into a :class:`BriefBundle`.

    The text layout is one ``[<prefix>-<index>]`` header per finding,
    anomaly, and thesis candidate, in document order. The bundle's
    ``source`` is derived from ``brief.sector`` via :class:`BriefSource`
    (which carries each :class:`Sector` value verbatim).
    """
    sections = [
        *(_render_finding(f) for f in brief.findings),
        *(_render_anomaly(a) for a in brief.anomalies),
        *(_render_thesis_candidate(tc) for tc in brief.thesis_candidates),
    ]
    return BriefBundle(
        source=BriefSource(brief.sector.value),
        text="".join(sections),
        freshness=freshness,
    )


# ---------------------------------------------------------------------------
# Correlation/regime adapter
# ---------------------------------------------------------------------------


def correlation_regime_brief_to_bundle(brief: CorrelationRegimeBrief) -> BriefBundle:
    """Wrap a :class:`CorrelationRegimeBrief` into a :class:`BriefBundle`.

    The CR brief is the only upstream whose body is already pre-rendered
    to the canonical layout (story 02-distillation/11b assembles it
    directly), so this adapter is a near-trivial wrapper: pass through
    ``brief.text`` and use ``brief.freshness_min`` for the bundle's
    freshness.
    """
    return BriefBundle(
        source=BriefSource.CR,
        text=brief.text,
        freshness=brief.freshness_min,
    )


# ---------------------------------------------------------------------------
# Qualitative adapter
# ---------------------------------------------------------------------------


def _render_narrative_thread(thread: NarrativeThread) -> str:
    """Render a :class:`NarrativeThread` to one section of the canonical layout."""
    evidence_lines = "".join(
        f"    - {ev.source_type}: {ev.observation} [{ev.citation}]\n" for ev in thread.evidence
    )
    return (
        f"[{thread.thread_id}] {thread.summary}\n"
        f"  Relevance: {thread.relevance}\n"
        f"  Direction: {thread.direction.value} for {thread.subject}\n"
        f"  Time horizon: {TIME_HORIZON_DISPLAY[thread.time_horizon]}\n"
        f"  Evidence:\n"
        f"{evidence_lines}"
        f"  Implication: {thread.implication}\n"
    )


def _render_catalyst_watch(catalyst: CatalystWatch) -> str:
    """Render a :class:`CatalystWatch` to one section of the canonical layout."""
    return (
        f"[{catalyst.catalyst_id}] {catalyst.ticker}: {catalyst.catalyst_name} "
        f"in {catalyst.hours_to_event}h\n"
        f"  Thesis impact: {catalyst.thesis_impact}\n"
    )


def qualitative_brief_to_bundle(brief: QualitativeBrief, freshness: datetime) -> BriefBundle:
    """Render a :class:`QualitativeBrief` into a :class:`BriefBundle`.

    Renders every QR-N narrative thread followed by every QR-CW-N
    catalyst-watch entry. The :class:`SentimentSnapshot` footer carries
    no reference IDs so it does not contribute a section here — the
    decision-layer agents read the synthesizer's prose; sentiment lands
    via the synthesizer's narrative, not as a retrievable section.
    """
    sections = [
        *(_render_narrative_thread(thread) for thread in brief.threads),
        *(_render_catalyst_watch(cw) for cw in brief.catalyst_watches),
    ]
    return BriefBundle(
        source=BriefSource.QR,
        text="".join(sections),
        freshness=freshness,
    )


# ---------------------------------------------------------------------------
# Adaptive adapter
# ---------------------------------------------------------------------------


def _render_investigation_thread(thread: InvestigationThread) -> str:
    """Render an :class:`InvestigationThread` to one section of the canonical layout."""
    findings_lines = "".join(f"    - {finding}\n" for finding in thread.findings)
    body = (
        f"[{thread.thread_id}]\n"
        f"  Trigger: {thread.trigger}\n"
        f"  Question: {thread.question}\n"
        f"  Tickers: {', '.join(thread.tickers)}\n"
        f"  Sector: {thread.sector.value}\n"
        f"  Tools used: {', '.join(thread.tools_used)}\n"
        f"  Findings:\n"
        f"{findings_lines}"
        f"  Assessment: {thread.assessment.value}\n"
        f"  Confidence: {thread.confidence.value}\n"
    )
    if thread.assessment is Assessment.SIGNAL:
        # Per the conditional-field discipline in
        # ``alphamind.analysis.adaptive_research.models``, a SIGNAL thread
        # has these three fields populated and the others ``None``.
        assert thread.implication is not None
        assert thread.strengthens is not None
        assert thread.weakens is not None
        body += (
            f"  Implication: {thread.implication}\n"
            f"  Strengthens: {', '.join(thread.strengthens) if thread.strengthens else 'none'}\n"
            f"  Weakens: {', '.join(thread.weakens) if thread.weakens else 'none'}\n"
        )
    elif thread.assessment is Assessment.NOISE:
        assert thread.dismissal_reason is not None
        body += f"  Dismissal reason: {thread.dismissal_reason}\n"
    else:  # Assessment.INCONCLUSIVE
        assert thread.missing is not None
        body += f"  Missing: {thread.missing}\n"
    return body


def adaptive_brief_to_bundle(brief: AdaptiveBrief, freshness: datetime) -> BriefBundle:
    """Render an :class:`AdaptiveBrief` into a :class:`BriefBundle`.

    Renders every AR-N investigation thread to the canonical layout. A
    zero-thread brief (quiet-cycle output) produces a bundle with empty
    body — a valid bundle that contributes nothing to the retrieval
    store, mirroring the design doc's "0 threads is correct" guarantee.
    """
    sections = [_render_investigation_thread(thread) for thread in brief.threads]
    return BriefBundle(
        source=BriefSource.AR,
        text="".join(sections),
        freshness=freshness,
    )
