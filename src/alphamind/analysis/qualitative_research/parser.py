"""Markdown brief parser for qualitative-researcher agent — story 03a (ALP-243).

Output contract (verbatim from prompts/analysis/qualitative_researcher.md
§ output_contract):

    Emit the brief as plain text with no surrounding prose, no markdown code
    fences, no preface. Section markers are literal; preserve them exactly.

    QUALITATIVE BRIEF
    Invocation: {invocation_id}
    Signal quality: {HIGH | MODERATE | LOW | DEGRADED}
      [If DEGRADED: reason]

    === NARRATIVE THREADS ===
    [QR-1] {one-sentence thread summary}
      Relevance: {sectors and/or tickers this thread touches}
      Direction: {bullish | bearish | mixed | uncertain} for {specific subject}
      Time horizon: {immediate (<24h) | near-term (24-72h) | developing (>72h)}
      Evidence:
        - {source type}: {specific observation} [from {digest ref or tool pull}]
        - {source type}: {specific observation}
      Implication: {1-2 sentences}

    [QR-2] ...

    === CATALYST WATCH ===
    [QR-CW-1] {ticker}: {catalyst name} in {hours}h
      Thesis impact: {1 sentence}

    [QR-CW-2] ...

    === SENTIMENT SNAPSHOT ===
    Extremes: {tickers at extreme readings with direction, or "none"}
    Divergences: {tickers where sentiment contradicts price action, or "none"}
    Regime: {overall market sentiment characterization in 1 sentence}

    Sequential indexing restarts within each section. Reference IDs match
    QR-N (narrative threads) and QR-CW-N (catalyst watch).

    Empty CATALYST WATCH section is valid. The narrative-threads section
    always carries at least one entry; the sentiment snapshot's three fields
    are always present. Stop after the sentiment snapshot's Regime: line.

Public names
------------
- :exc:`ParseError`
- :func:`parse_qualitative_brief`
"""

from __future__ import annotations

import re

from alphamind.analysis.qualitative_research.models import (
    _TIME_HORIZON_DISPLAY,
    CatalystWatch,
    EvidenceLine,
    NarrativeThread,
    QualitativeBrief,
    SentimentSnapshot,
    SignalQuality,
    ThreadDirection,
    TimeHorizon,
)

__all__ = ["ParseError", "parse_qualitative_brief"]

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^```(?:json|text|markdown)?\s*$")
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")
_DEGRADED_REASON_RE = re.compile(r"^\s*\[If DEGRADED:\s*reason\s*[—\-]\s*(.+?)\]\s*$")
_THREAD_HEADER_RE = re.compile(r"^\[QR-(\d+)\]\s+(.+)$")
_CATALYST_HEADER_RE = re.compile(r"^\[QR-CW-(\d+)\]\s+(\S+?):\s+(.+?)\s+in\s+~?(\d+)h\s*$")

# Reverse-lookup from display string to TimeHorizon.
# Keys are the canonical display strings from _TIME_HORIZON_DISPLAY (hyphens only).
# _parse_time_horizon_field normalises em-dashes to hyphens before lookup.
_DISPLAY_TO_HORIZON: dict[str, TimeHorizon] = {
    display: horizon for horizon, display in _TIME_HORIZON_DISPLAY.items()
}

_SECTION_MARKERS = [
    "=== NARRATIVE THREADS ===",
    "=== CATALYST WATCH ===",
    "=== SENTIMENT SNAPSHOT ===",
]

_MARKER_TO_KEY: dict[str, str] = {
    "=== NARRATIVE THREADS ===": "narrative_threads",
    "=== CATALYST WATCH ===": "catalyst_watch",
    "=== SENTIMENT SNAPSHOT ===": "sentiment_snapshot",
}


# ---------------------------------------------------------------------------
# ParseError
# ---------------------------------------------------------------------------


class ParseError(Exception):
    """Raised when a qualitative-researcher brief cannot be structurally parsed.

    Attributes mirror the domain-researcher ``ParseError`` shape so that
    story 04b's corrective-retry harness can build corrective-retry messages
    from a uniform shape.
    """

    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_qualitative_brief(text: str, *, invocation_id: str) -> QualitativeBrief:
    """Parse *text* into a :class:`QualitativeBrief`.

    Parameters
    ----------
    text:
        Raw text response from the qualitative researcher agent.  Leading and
        trailing whitespace is tolerated.  A single surrounding markdown code
        fence (`` ``` ``, `` ```text ``, `` ```markdown ``) is stripped before
        parsing; nested fences are a :class:`ParseError`.
    invocation_id:
        Canonical invocation identifier supplied by the harness.  The brief's
        own ``Invocation:`` header is parsed but discarded; this value wins.

    Raises
    ------
    ParseError
        On any structural malformation.
    """
    text = text.strip()
    text = _strip_code_fence(text)
    lines = text.splitlines()
    signal_quality, signal_quality_reason, header_end = _parse_header(lines)
    sections = _split_sections(lines, header_end)
    threads = _parse_threads(sections["narrative_threads"])
    catalyst_watches = _parse_catalyst_watch(sections["catalyst_watch"])
    sentiment_snapshot = _parse_sentiment_snapshot(sections["sentiment_snapshot"])
    return QualitativeBrief(
        invocation_id=invocation_id,
        signal_quality=signal_quality,
        signal_quality_reason=signal_quality_reason,
        threads=tuple(threads),
        catalyst_watches=tuple(catalyst_watches),
        sentiment_snapshot=sentiment_snapshot,
    )


# ---------------------------------------------------------------------------
# Envelope stripping
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """Strip a single surrounding markdown code fence if present.

    Nested or multiple fences raise :class:`ParseError`.
    """
    lines = text.splitlines()
    if not lines:
        return text

    has_open = bool(_FENCE_OPEN_RE.match(lines[0]))
    has_close = bool(_FENCE_CLOSE_RE.match(lines[-1]))

    if has_open and has_close:
        inner = lines[1:-1]
        for line in inner:
            if _FENCE_OPEN_RE.match(line) or _FENCE_CLOSE_RE.match(line):
                raise ParseError(
                    field_path="envelope",
                    message="nested markdown code fence — prose-wrapped content not accepted",
                )
        return "\n".join(inner)

    if has_open and not has_close:
        raise ParseError(
            field_path="envelope",
            message="unclosed markdown code fence",
        )

    if not has_open and has_close:
        raise ParseError(
            field_path="envelope",
            message="closing fence without opening fence",
        )

    # No outer fence — ensure no stray fence markers
    for line in lines:
        if _FENCE_OPEN_RE.match(line) or _FENCE_CLOSE_RE.match(line):
            raise ParseError(
                field_path="envelope",
                message="unexpected markdown code fence in content",
            )
    return text


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def _parse_header(lines: list[str]) -> tuple[SignalQuality, str | None, int]:
    """Parse header lines.

    Returns ``(signal_quality, signal_quality_reason, first_non-header_line_index)``.
    """
    i = 0
    n = len(lines)

    # Skip leading blank lines
    while i < n and not lines[i].strip():
        i += 1

    # First non-blank must be "QUALITATIVE BRIEF"
    if i >= n or lines[i].strip() != "QUALITATIVE BRIEF":
        raise ParseError(
            field_path="envelope",
            message="first non-blank line must be exactly 'QUALITATIVE BRIEF'",
        )
    i += 1

    # Invocation: <id> (parsed, discarded)
    while i < n and not lines[i].strip():
        i += 1
    if i >= n or not lines[i].strip().startswith("Invocation:"):
        raise ParseError(
            field_path="header.invocation_id",
            message="missing 'Invocation:' line",
        )
    i += 1  # discard

    # Signal quality:
    while i < n and not lines[i].strip():
        i += 1
    if i >= n or not lines[i].strip().startswith("Signal quality:"):
        raise ParseError(
            field_path="header.signal_quality",
            message="missing 'Signal quality:' line",
        )
    raw_quality = lines[i].strip().removeprefix("Signal quality:").strip().upper()
    try:
        signal_quality = SignalQuality(raw_quality.lower())
    except ValueError:
        raise ParseError(
            field_path="header.signal_quality",
            message=f"invalid signal quality value: {raw_quality!r}",
        ) from None
    i += 1

    # Optional DEGRADED reason line
    signal_quality_reason, i = _parse_degraded_reason(lines, i, signal_quality)

    return signal_quality, signal_quality_reason, i


def _parse_degraded_reason(
    lines: list[str], start: int, signal_quality: SignalQuality
) -> tuple[str | None, int]:
    """Check for and validate the optional DEGRADED reason line.

    Returns ``(reason_or_None, next_line_index)``.
    """
    n = len(lines)
    j = start
    while j < n and not lines[j].strip():
        j += 1

    if j < n and (reason_match := _DEGRADED_REASON_RE.match(lines[j])):
        if signal_quality != SignalQuality.DEGRADED:
            raise ParseError(
                field_path="header.signal_quality_reason",
                message="signal_quality_reason present but signal_quality is not DEGRADED",
            )
        return reason_match.group(1).strip(), j + 1

    if signal_quality == SignalQuality.DEGRADED:
        raise ParseError(
            field_path="header.signal_quality_reason",
            message="signal_quality is DEGRADED but no reason line found",
        )
    return None, start  # return original start — blank lines before section markers are ok


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------


def _split_sections(lines: list[str], header_end: int) -> dict[str, list[str]]:
    """Locate and extract the three required sections.

    Sections must appear in the required order.  Missing or out-of-order
    markers raise :class:`ParseError`.
    """
    # Find positions of all three markers
    positions: dict[str, int] = {}
    for i in range(header_end, len(lines)):
        stripped = lines[i].strip()
        if stripped in _SECTION_MARKERS:
            positions[_MARKER_TO_KEY[stripped]] = i

    required_keys = ["narrative_threads", "catalyst_watch", "sentiment_snapshot"]
    for key, marker in zip(required_keys, _SECTION_MARKERS, strict=True):
        if key not in positions:
            raise ParseError(
                field_path=f"sections.{key}",
                message=f"missing required section marker: {marker!r}",
            )

    # Validate order
    order = [positions[k] for k in required_keys]
    if order != sorted(order):
        # Identify which marker is out of place
        for i, (key, marker) in enumerate(zip(required_keys, _SECTION_MARKERS, strict=True)):
            if positions[key] != order[i]:
                raise ParseError(
                    field_path=f"sections.{key}",
                    message=f"section marker {marker!r} appears out of order",
                )
        # Fallback — shouldn't reach here
        raise ParseError(
            field_path="sections",
            message="section markers appear out of order",
        )

    # Extract section bodies
    n = len(lines)
    sections: dict[str, list[str]] = {}
    for i, key in enumerate(required_keys):
        start = positions[key] + 1
        end = positions[required_keys[i + 1]] if i + 1 < len(required_keys) else n
        sections[key] = lines[start:end]

    return sections


# ---------------------------------------------------------------------------
# Narrative threads parsing
# ---------------------------------------------------------------------------


def _parse_threads(section_lines: list[str]) -> list[NarrativeThread]:
    """Parse NARRATIVE THREADS section into a list of :class:`NarrativeThread`."""
    blocks = _split_thread_blocks(section_lines)

    if not blocks:
        raise ParseError(
            field_path="threads",
            message="narrative-threads section must contain at least one thread",
        )

    threads: list[NarrativeThread] = []
    for header_match, block_lines in blocks:
        thread_num = header_match.group(1)
        summary = header_match.group(2).strip()
        thread_id = f"QR-{thread_num}"
        threads.append(_parse_single_thread(thread_id, summary, block_lines))

    return threads


def _split_thread_blocks(
    lines: list[str],
) -> list[tuple[re.Match[str], list[str]]]:
    """Split section lines into (header_match, body_lines) pairs for each [QR-N] block."""
    blocks: list[tuple[re.Match[str], list[str]]] = []
    current_match: re.Match[str] | None = None
    current_body: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Check for a bad prefix (e.g., [SA-TECH-1])
        if re.match(r"^\[(?!QR-)\S+-\d+\]", stripped):
            raise ParseError(
                field_path="threads[?].thread_id",
                message=f"unexpected reference-ID prefix in threads section: {stripped!r}",
            )
        m = _THREAD_HEADER_RE.match(stripped)
        if m:
            if current_match is not None:
                blocks.append((current_match, current_body))
            current_match = m
            current_body = []
        else:
            if current_match is not None:
                current_body.append(line)

    if current_match is not None:
        blocks.append((current_match, current_body))

    return blocks


def _parse_single_thread(thread_id: str, summary: str, body_lines: list[str]) -> NarrativeThread:
    """Parse a single [QR-N] block into a :class:`NarrativeThread`."""
    relevance: str | None = None
    direction: ThreadDirection | None = None
    subject: str | None = None
    time_horizon: TimeHorizon | None = None
    evidence_lines: list[EvidenceLine] = []
    implication: str | None = None

    in_evidence = False

    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("Evidence:"):
            in_evidence = True
            continue

        if in_evidence and stripped.startswith("-"):
            ev = _parse_evidence_line(stripped)
            evidence_lines.append(ev)
            continue

        in_evidence = False

        if stripped.startswith("Relevance:"):
            relevance = stripped.removeprefix("Relevance:").strip()
        elif stripped.startswith("Direction:"):
            direction, subject = _parse_direction_field(stripped, thread_id)
        elif stripped.startswith("Time horizon:"):
            time_horizon = _parse_time_horizon_field(stripped, thread_id)
        elif stripped.startswith("Implication:"):
            implication = stripped.removeprefix("Implication:").strip()

    if len(evidence_lines) < 2:
        raise ParseError(
            field_path=f"threads[{thread_id}].evidence",
            message=(
                f"thread {thread_id} must have at least 2 evidence lines; got {len(evidence_lines)}"
            ),
        )

    # Use safe fallbacks for optional-but-required fields (missing fields caught by Pydantic)
    return NarrativeThread(
        thread_id=thread_id,
        summary=summary,
        relevance=relevance or "",
        direction=direction or ThreadDirection.UNCERTAIN,
        subject=subject or "",
        time_horizon=time_horizon or TimeHorizon.IMMEDIATE,
        evidence=tuple(evidence_lines),
        implication=implication or "",
    )


def _parse_direction_field(line: str, thread_id: str) -> tuple[ThreadDirection, str]:
    """Parse ``Direction: {dir} for {subject}`` returning (direction, subject)."""
    value = line.removeprefix("Direction:").strip()
    # Take the first token as the direction candidate
    first_token = value.split()[0].lower().rstrip(",") if value.split() else ""
    try:
        direction = ThreadDirection(first_token)
    except ValueError:
        raise ParseError(
            field_path=f"threads[{thread_id}].direction",
            message=(
                f"invalid direction value: {first_token!r}; "
                "valid: bullish, bearish, mixed, uncertain"
            ),
        ) from None

    # Subject is everything after the first " for " occurrence
    for_idx = value.lower().find(" for ")
    subject = value[for_idx + 5 :].strip() if for_idx >= 0 else value

    return direction, subject


def _parse_time_horizon_field(line: str, thread_id: str) -> TimeHorizon:
    """Parse ``Time horizon: {display}`` returning a :class:`TimeHorizon`."""
    raw = line.removeprefix("Time horizon:").strip()
    # Normalise Unicode en/em-dash to hyphen (LLMs sometimes emit U+2013/U+2014).
    normalised = raw.replace("\u2013", "-").replace("\u2014", "-")
    horizon = _DISPLAY_TO_HORIZON.get(normalised)
    if horizon is None:
        raise ParseError(
            field_path=f"threads[{thread_id}].time_horizon",
            message=f"invalid time horizon value: {raw!r}",
        )
    return horizon


def _parse_evidence_line(line: str) -> EvidenceLine:
    """Parse a single evidence sub-line ``- {source_type}: {observation} [from {citation}]``."""
    # Strip leading dash
    content = line.lstrip("-").strip()

    # Extract citation from trailing [from ...]
    citation = ""
    from_match = re.search(r"\[from\s+(.+?)\]\s*$", content)
    if from_match:
        citation = from_match.group(1).strip()
        content = content[: from_match.start()].strip()

    # Split on first ': ' for source_type
    colon_idx = content.find(": ")
    if colon_idx >= 0:
        source_type = content[:colon_idx].strip()
        observation = content[colon_idx + 2 :].strip()
    else:
        # Fallback: entire content is source_type, empty observation
        source_type = content.strip()
        observation = content.strip()

    return EvidenceLine(
        source_type=source_type,
        observation=observation,
        citation=citation or content,
    )


# ---------------------------------------------------------------------------
# Catalyst watch parsing
# ---------------------------------------------------------------------------


def _parse_catalyst_watch(section_lines: list[str]) -> list[CatalystWatch]:
    """Parse CATALYST WATCH section into a list of :class:`CatalystWatch`.

    Empty section is valid and returns an empty list.
    """
    catalysts: list[CatalystWatch] = []
    current_id: str | None = None
    current_ticker: str | None = None
    current_name: str | None = None
    current_hours: int | None = None

    for line in section_lines:
        stripped = line.strip()
        if not stripped:
            continue

        m = _CATALYST_HEADER_RE.match(stripped)
        if m:
            # Save previous entry
            if current_id is not None:
                catalysts.append(
                    CatalystWatch(
                        catalyst_id=current_id,
                        ticker=current_ticker or "",
                        catalyst_name=current_name or "",
                        hours_to_event=current_hours or 0,
                        thesis_impact="",
                    )
                )
            current_id = f"QR-CW-{m.group(1)}"
            current_ticker = m.group(2).strip()
            current_name = m.group(3).strip()
            current_hours = int(m.group(4))
        elif stripped.startswith("Thesis impact:") and current_id is not None:
            thesis_impact_text = stripped.removeprefix("Thesis impact:").strip()
            # Build the final entry now that we have thesis impact
            catalysts.append(
                CatalystWatch(
                    catalyst_id=current_id,
                    ticker=current_ticker or "",
                    catalyst_name=current_name or "",
                    hours_to_event=current_hours or 0,
                    thesis_impact=thesis_impact_text,
                )
            )
            current_id = None
            current_ticker = None
            current_name = None
            current_hours = None

    # Handle a catalyst entry with no thesis-impact line
    if current_id is not None:
        catalysts.append(
            CatalystWatch(
                catalyst_id=current_id,
                ticker=current_ticker or "",
                catalyst_name=current_name or "",
                hours_to_event=current_hours or 0,
                thesis_impact="",
            )
        )

    return catalysts


# ---------------------------------------------------------------------------
# Sentiment snapshot parsing
# ---------------------------------------------------------------------------


def _parse_sentiment_snapshot(section_lines: list[str]) -> SentimentSnapshot:
    """Parse SENTIMENT SNAPSHOT section into a :class:`SentimentSnapshot`.

    Requires exactly the three fields ``Extremes:``, ``Divergences:``, ``Regime:``
    in order.  Any non-whitespace content after ``Regime:`` raises
    :class:`ParseError`.
    """
    extremes: str | None = None
    divergences: str | None = None
    regime: str | None = None
    regime_line_idx: int | None = None

    non_blank = [(i, line) for i, line in enumerate(section_lines) if line.strip()]

    for i, line in non_blank:
        stripped = line.strip()
        if stripped.startswith("Extremes:"):
            extremes = stripped.removeprefix("Extremes:").strip()
        elif stripped.startswith("Divergences:"):
            divergences = stripped.removeprefix("Divergences:").strip()
        elif stripped.startswith("Regime:"):
            regime = stripped.removeprefix("Regime:").strip()
            regime_line_idx = i

    if regime_line_idx is not None:
        # Any non-whitespace content after the Regime line is a parse error
        after_regime = [line for _, line in non_blank if _ > regime_line_idx and line.strip()]
        if after_regime:
            raise ParseError(
                field_path="sentinel",
                message="unexpected content after Regime: line",
            )

    if extremes is None:
        raise ParseError(
            field_path="sentiment_snapshot.extremes",
            message="missing required 'Extremes:' line",
        )
    if divergences is None:
        raise ParseError(
            field_path="sentiment_snapshot.divergences",
            message="missing required 'Divergences:' line",
        )
    if regime is None:
        raise ParseError(
            field_path="sentiment_snapshot.regime",
            message="missing required 'Regime:' line",
        )

    return SentimentSnapshot(
        extremes=extremes,
        divergences=divergences,
        regime=regime,
    )
