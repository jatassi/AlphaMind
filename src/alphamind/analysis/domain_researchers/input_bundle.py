"""Per-sector input bundle assembler — story ALP-188.

Composes the user-message text the harness sends to a domain researcher:
the sector's slice of the distillation output (already rendered as text by
``assemble_sector_output``) plus the rendered sector-specific qualitative
input.  Output is a single deterministic string the LLM reads as its full
input.

The volatility regime label is **not** rendered separately — it is already
inside ``SectorOutput.text`` as a UNIVERSAL_BROADCAST block.  This module
wraps the existing distillation text rather than re-decorating it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.qualitative_input import (
    EventEntry,
    HeadlineEntry,
    SectorQualitativeInput,
)
from alphamind.analysis.news_freshness import render_empty_reason_text

__all__ = [
    "InputBundle",
    "assemble_input_bundle",
]


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InputBundle:
    """The composed bundle as a value object; also used for diagnostic preservation."""

    sector: Sector
    invocation_id: str
    as_of: datetime
    distillation_text: str  # SectorOutput.text — passed through verbatim
    qualitative_text: str  # rendered by this module
    bundle_text: str  # full concatenated user message ready for the LLM


# ---------------------------------------------------------------------------
# Public assembler
# ---------------------------------------------------------------------------


def assemble_input_bundle(
    *,
    sector: Sector,
    invocation_id: str,
    as_of: datetime,
    distillation_text: str,
    qualitative_input: SectorQualitativeInput,
) -> InputBundle:
    """Compose and return the full user-message bundle for a domain researcher.

    Pure function — no I/O, no logging, deterministic.  The caller
    (story 10's runner) extracts ``distillation_text`` from
    ``DistillationOutputs.sector_outputs[_SECTOR_AUDIENCE_MAP[sector]].text``
    and passes it through.
    """
    qualitative_text = _render_qualitative(qualitative_input)
    bundle_text = _render_bundle(
        sector=sector,
        invocation_id=invocation_id,
        as_of=as_of,
        distillation_text=distillation_text,
        qualitative_text=qualitative_text,
    )
    return InputBundle(
        sector=sector,
        invocation_id=invocation_id,
        as_of=as_of,
        distillation_text=distillation_text,
        qualitative_text=qualitative_text,
        bundle_text=bundle_text,
    )


# ---------------------------------------------------------------------------
# Internal renderers
# ---------------------------------------------------------------------------


def _fmt_utc(dt: datetime) -> str:
    """Format a datetime as ISO 8601 UTC string (YYYY-MM-DDTHH:MM:SSZ)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_headline(index: int, entry: HeadlineEntry) -> str:
    """Render one headline entry as a single compact line (ALP-272).

    Format: ``[i] <iso> [<tier>] <outlet> | <tickers> | <tags> | <headline>``.
    The headline trails because it carries variable-length prose; metadata
    sits up front so a scanning consumer (or LLM) can reject the row before
    reading the prose. ``(macro)`` / ``(none)`` placeholders preserve the
    semantic distinction between "no tickers extracted" and "tag-only match".
    """
    tickers_str = ", ".join(entry.tickers) if entry.tickers else "(macro)"
    tags_str = ", ".join(tag.value for tag in entry.tags) if entry.tags else "(none)"
    return (
        f"[{index}] {_fmt_utc(entry.published_at)} [{entry.tier}]"
        f" {entry.source_outlet} | {tickers_str} | {tags_str} | {entry.headline}"
    )


def _render_event(entry: EventEntry) -> str:
    """Render one event entry as a single bullet line."""
    tickers_str = ", ".join(entry.tickers) if entry.tickers else "(macro)"
    sectors_str = (
        ", ".join(sorted(s.value for s in entry.sectors)) if entry.sectors else "(cross-sector)"
    )
    consensus_str = entry.consensus if entry.consensus is not None else "(none)"
    return (
        f"- {_fmt_utc(entry.event_time)} | {entry.event_name}"
        f" | sectors: {sectors_str}"
        f" | tickers: {tickers_str}"
        f" | consensus: {consensus_str}"
    )


def _render_qualitative(qi: SectorQualitativeInput) -> str:
    """Render the qualitative section body (without the ## header)."""
    lines: list[str] = [
        f"Lookback window: last {qi.lookback_window_hours} hours",
        f"Data freshness: {_fmt_utc(qi.data_freshness)}",
        "",
        f"### HEADLINES (top {len(qi.headlines)} by composite score)",
    ]
    if qi.headlines:
        for i, headline in enumerate(qi.headlines, start=1):
            lines.append(_render_headline(i, headline))
    else:
        lines.append("(no qualifying headlines in window)")
        if qi.headlines_empty_diagnosis is not None:
            lines.append(f"Reason: {render_empty_reason_text(qi.headlines_empty_diagnosis)}")

    lines.append("")
    lines.append("### SCHEDULED EVENTS (next 72h)")
    if qi.events:
        for event in qi.events:
            lines.append(_render_event(event))
    else:
        lines.append("(no scheduled events in 72h window)")

    return "\n".join(lines)


_DISTILLATION_SECTION_HEADER = (
    "## DISTILLATION OUTPUT (sector slice"
    " — includes regime, anomaly summary, universal context, sector indicators)"
)


def _render_bundle(
    *,
    sector: Sector,
    invocation_id: str,
    as_of: datetime,
    distillation_text: str,
    qualitative_text: str,
) -> str:
    """Assemble the full bundle text from its parts."""
    parts: list[str] = [
        "# SECTOR RESEARCHER INPUT BUNDLE",
        f"Invocation: {invocation_id}",
        f"Sector: {sector.value}",
        f"As of: {_fmt_utc(as_of)}",
        "",
        _DISTILLATION_SECTION_HEADER,
        "",
        distillation_text,
        "## SECTOR-SPECIFIC QUALITATIVE INPUT",
        qualitative_text,
        "",
    ]
    return "\n".join(parts)
