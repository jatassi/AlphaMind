"""Input-bundle assembler for the synthesizer agent — story 07 (ALP-207).

Composes the user-message text the synthesizer's harness (story 08) sends to
the LLM: the volatility regime label, a reminder of the portfolio-state tools,
and the upstream brief texts in the canonical reading order declared by the
system prompt's ``<inputs>`` block (CR → SA-TECH → SA-FIN → SA-ENERGY → QR
→ AR).

Pure function — no I/O, no logging, deterministic.

See ``docs/design/03-analysis-layer/synthesizer.md`` § Inputs and
``prompts/analysis/synthesizer.md`` for the source-order contract.
"""

from __future__ import annotations

from datetime import datetime

from alphamind.analysis.synthesizer.models import BriefBundle, BriefSource

__all__ = ["assemble_input_bundle"]


_SOURCE_LABELS: dict[BriefSource, str] = {
    BriefSource.CR: "Correlation/regime brief",
    BriefSource.SA_TECH: "Tech & semis sector brief",
    BriefSource.SA_FIN: "Financials sector brief",
    BriefSource.SA_ENERGY: "Energy sector brief",
    BriefSource.QR: "Baseline qualitative brief",
    BriefSource.AR: "Adaptive research findings",
}


# Canonical render order — matches the system prompt's ``<inputs>`` block.
_RENDER_ORDER: tuple[BriefSource, ...] = (
    BriefSource.CR,
    BriefSource.SA_TECH,
    BriefSource.SA_FIN,
    BriefSource.SA_ENERGY,
    BriefSource.QR,
    BriefSource.AR,
)


def _format_freshness(freshness: datetime, now_utc: datetime) -> str:
    """Render an "Nm ago" string. Negative deltas (clock skew) clamp to 0m."""
    delta_seconds = (now_utc - freshness).total_seconds()
    minutes = max(0, int(delta_seconds // 60))
    return f"{minutes}m ago"


def _source_label(source: BriefSource) -> str:
    """Human-readable label matching the system prompt's ``<inputs>`` block."""
    return _SOURCE_LABELS[source]


def assemble_input_bundle(
    *,
    regime_label: str,
    brief_bundles: tuple[BriefBundle, ...],
    portfolio_tool_names: tuple[str, ...],
    now_utc: datetime,
) -> str:
    """Compose the synthesizer's user-message text.

    Bundles are rendered in canonical order (CR → SA-TECH → SA-FIN →
    SA-ENERGY → QR → AR); sources missing from ``brief_bundles`` are
    skipped. The assembler does not validate the bundle texts — that's
    upstream's job.
    """
    by_source: dict[BriefSource, BriefBundle] = {b.source: b for b in brief_bundles}

    sections = [
        f"Volatility regime: {regime_label}",
        "",
        "=== AVAILABLE TOOLS ===",
        "Portfolio-state tools (call only when an upstream finding is portfolio-relevant):",
        f"  {', '.join(portfolio_tool_names)}",
    ]

    for source in _RENDER_ORDER:
        bundle = by_source.get(source)
        if bundle is None:
            continue
        freshness = _format_freshness(bundle.freshness, now_utc)
        sections.append("")
        sections.append(f"=== {_source_label(source)} (freshness: {freshness}) ===")
        sections.append(bundle.text)

    return "\n".join(sections)
