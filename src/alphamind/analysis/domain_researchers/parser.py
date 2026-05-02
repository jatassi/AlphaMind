"""Markdown brief parser for domain-researcher agents — story 04 (ALP-192).

Turns a domain researcher's raw text response into a typed
:class:`~alphamind.analysis.domain_researchers.models.SectorBrief`.
This is the first transform in the LLM-output validation stack for text-format
agents: malformed input raises :class:`ParseError` rather than being silently
coerced.

Strict-parse stance mirrors the JSON-format Layer-1 envelope parse in
llm-output-validation.md: a single leading/trailing markdown code fence is
stripped; nested fences are a :class:`ParseError`; prose-wrapped content is a
:class:`ParseError`.
"""

from __future__ import annotations

import re
from typing import cast

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.models import (
    SECTOR_PREFIX,
    Anomaly,
    AnomalyType,
    ConvictionSketch,
    Direction,
    Finding,
    SectorBrief,
    SetupType,
    SignalQuality,
    SignalType,
    Strength,
    ThesisCandidate,
)
from alphamind.distillation.output import AnomalySeverity

__all__ = ["ParseError", "parse_brief"]

# ---------------------------------------------------------------------------
# Sector label ↔ Sector mapping (authoritative in tech-semis.md)
# ---------------------------------------------------------------------------

_SECTOR_LABEL: dict[Sector, str] = {
    Sector.TECH_SEMIS: "Tech & Semis",
    Sector.FINANCIALS: "Financials",
    Sector.ENERGY: "Energy",
}

# AnomalySeverity is a Literal type, not an enum — validate against the known string set.
_VALID_SEVERITY: frozenset[str] = frozenset(
    ["investigate_now", "investigate_if_persists", "note_for_context"]
)

_DEGRADED_REASON_RE = re.compile(r"^\s*\[If DEGRADED:\s*reason\s*[—\-]\s*(.+?)\]\s*$")


# ---------------------------------------------------------------------------
# ParseError
# ---------------------------------------------------------------------------


class ParseError(Exception):
    """Raised when a domain researcher brief cannot be structurally parsed.

    Attributes mirror the JSON Schema validator's error shape so that story 05's
    corrective-retry message construction can use them uniformly.
    """

    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_brief(text: str, sector: Sector) -> SectorBrief:
    """Parse *text* into a :class:`SectorBrief` for *sector*.

    Parameters
    ----------
    text:
        Raw text response from the domain researcher agent.  Leading and
        trailing whitespace is tolerated.  A single surrounding markdown code
        fence (` ``` `, ` ```json `, ` ```text `) is stripped before parsing;
        nested fences are a :class:`ParseError`.
    sector:
        The sector the agent is configured for.  Briefs do not self-identify
        their sector; the harness (story 07) supplies this value.

    Raises
    ------
    ParseError
        On any structural malformation: missing sections, unknown enum values,
        wrong-sector reference IDs, sector-label/sector mismatch.
    """
    text = text.strip()
    text = _strip_code_fence(text)
    lines = text.splitlines()
    invocation_id, signal_quality, signal_quality_reason = _parse_header(lines, sector)
    sections = _split_sections(lines)
    findings = _parse_findings(sections["key_findings"], sector)
    anomalies = _parse_anomalies(sections["flagged_anomalies"], sector)
    thesis_candidates = _parse_thesis_candidates(sections["thesis_candidates"], sector)
    return SectorBrief(
        invocation_id=invocation_id,
        sector=sector,
        signal_quality=signal_quality,
        signal_quality_reason=signal_quality_reason,
        findings=tuple(findings),
        anomalies=tuple(anomalies),
        thesis_candidates=tuple(thesis_candidates),
    )


# ---------------------------------------------------------------------------
# Fence stripping
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^```(?:json|text)?\s*$")
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")


def _strip_code_fence(text: str) -> str:
    """Strip a single surrounding markdown code fence, if present.

    Nested or multiple fences raise :class:`ParseError`.
    """
    lines = text.splitlines()
    if not lines:
        return text

    has_open = bool(_FENCE_OPEN_RE.match(lines[0]))
    has_close = lines and bool(_FENCE_CLOSE_RE.match(lines[-1]))

    if has_open and has_close:
        inner = lines[1:-1]
        # Check for nested fences inside
        for line in inner:
            if _FENCE_OPEN_RE.match(line) or _FENCE_CLOSE_RE.match(line):
                raise ParseError(
                    field_path="envelope.fence",
                    message="nested markdown code fence detected — prose-wrapped content",
                )
        return "\n".join(inner)

    if has_open and not has_close:
        raise ParseError(
            field_path="envelope.fence",
            message="unclosed markdown code fence",
        )

    if not has_open and has_close:
        raise ParseError(
            field_path="envelope.fence",
            message="closing fence without opening fence",
        )

    # No fence — check there are no stray fence lines anywhere
    for line in lines:
        if _FENCE_OPEN_RE.match(line) or _FENCE_CLOSE_RE.match(line):
            raise ParseError(
                field_path="envelope.fence",
                message="unexpected markdown code fence in content",
            )

    return text


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def _parse_header(lines: list[str], sector: Sector) -> tuple[str, SignalQuality, str | None]:
    """Extract header fields from *lines*.

    Returns ``(invocation_id, signal_quality, signal_quality_reason)``.
    """
    i = 0
    n = len(lines)

    while i < n and not lines[i].strip():
        i += 1
    if i >= n or not lines[i].strip().startswith("SECTOR BRIEF:"):
        raise ParseError(
            field_path="header.sector_label",
            message="missing 'SECTOR BRIEF:' line",
        )
    sector_label = lines[i].strip().removeprefix("SECTOR BRIEF:").strip()
    i += 1

    while i < n and not lines[i].strip():
        i += 1
    if i >= n or not lines[i].strip().startswith("Invocation:"):
        raise ParseError(
            field_path="header.invocation_id",
            message="missing 'Invocation:' line",
        )
    invocation_id = lines[i].strip().removeprefix("Invocation:").strip()
    i += 1

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

    signal_quality_reason = _parse_degraded_reason(lines, i, signal_quality)

    expected_label = _SECTOR_LABEL[sector]
    if sector_label != expected_label:
        raise ParseError(
            field_path="header.sector_label",
            message=(
                f"sector label {sector_label!r} does not match "
                f"expected {expected_label!r} for sector {sector}"
            ),
        )

    return invocation_id, signal_quality, signal_quality_reason


def _parse_degraded_reason(
    lines: list[str], start: int, signal_quality: SignalQuality
) -> str | None:
    """Check for and validate the optional DEGRADED reason line.

    The reason line is required when ``signal_quality`` is DEGRADED and
    prohibited otherwise.  Returns the reason string or ``None``.
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
        return reason_match.group(1).strip()

    if signal_quality == SignalQuality.DEGRADED:
        raise ParseError(
            field_path="header.signal_quality_reason",
            message="signal_quality is DEGRADED but no reason line found",
        )
    return None


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------

_SECTION_HEADERS: dict[str, str] = {
    "key_findings": "=== KEY FINDINGS ===",
    "flagged_anomalies": "=== FLAGGED ANOMALIES ===",
    "thesis_candidates": "=== THESIS CANDIDATES ===",
}


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    """Split *lines* into named sections.

    Raises :class:`ParseError` if any required section header is missing.
    """
    positions: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        for key, header in _SECTION_HEADERS.items():
            if stripped == header:
                positions[key] = i
                break

    for key in _SECTION_HEADERS:
        if key not in positions:
            raise ParseError(
                field_path=f"sections.{key}",
                message=f"missing section header: {_SECTION_HEADERS[key]!r}",
            )

    ordered_positions = sorted(positions.items(), key=lambda x: x[1])
    sections: dict[str, list[str]] = {}
    for idx, (key, start) in enumerate(ordered_positions):
        end = ordered_positions[idx + 1][1] if idx + 1 < len(ordered_positions) else len(lines)
        sections[key] = lines[start + 1 : end]

    return sections


# ---------------------------------------------------------------------------
# Finding parsing
# ---------------------------------------------------------------------------

_FINDING_HEADER_RE = re.compile(r"^\[SA-(TECH|FIN|ENERGY)-(\d+)\]\s+(.+)$")


def _parse_findings(section_lines: list[str], sector: Sector) -> list[Finding]:
    """Parse KEY FINDINGS section into a list of :class:`Finding` records."""
    expected_prefix = SECTOR_PREFIX[sector]
    blocks = _split_blocks(section_lines, _FINDING_HEADER_RE)
    findings: list[Finding] = []

    for header_match, block_lines in blocks:
        sector_code = header_match.group(1)
        ref_id = f"SA-{sector_code}-{header_match.group(2)}"
        headline = header_match.group(3).strip()

        # Validate sector prefix
        if f"SA-{sector_code}" != expected_prefix:
            raise ParseError(
                field_path=f"findings[{ref_id}].finding_id",
                message=(
                    f"reference ID prefix SA-{sector_code} does not match "
                    f"expected {expected_prefix} for sector {sector}"
                ),
            )

        fields = _parse_field_lines(block_lines)

        tickers = _require_field(fields, "Tickers", f"findings[{ref_id}]")
        ticker_list = tuple(t.strip() for t in tickers.split(",") if t.strip())

        signal_type_raw = _require_field(fields, "Signal type", f"findings[{ref_id}]")
        try:
            signal_type = SignalType(signal_type_raw.strip().lower())
        except ValueError:
            raise ParseError(
                field_path=f"findings[{ref_id}].signal_type",
                message=f"invalid signal_type value: {signal_type_raw!r}",
            ) from None

        strength_raw = _require_field(fields, "Strength", f"findings[{ref_id}]")
        try:
            strength = Strength(strength_raw.strip().lower())
        except ValueError:
            raise ParseError(
                field_path=f"findings[{ref_id}].strength",
                message=f"invalid strength value: {strength_raw!r}",
            ) from None

        detail = _require_field(fields, "Detail", f"findings[{ref_id}]")

        findings.append(
            Finding(
                finding_id=ref_id,
                headline=headline,
                tickers=ticker_list,
                signal_type=signal_type,
                strength=strength,
                detail=detail.strip(),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Anomaly parsing
# ---------------------------------------------------------------------------

_ANOMALY_HEADER_RE = re.compile(r"^\[SA-(TECH|FIN|ENERGY)-ANOM-(\d+)\]\s+(.+)$")


def _parse_anomalies(section_lines: list[str], sector: Sector) -> list[Anomaly]:
    """Parse FLAGGED ANOMALIES section into a list of :class:`Anomaly` records."""
    expected_prefix = SECTOR_PREFIX[sector]
    blocks = _split_blocks(section_lines, _ANOMALY_HEADER_RE)
    anomalies: list[Anomaly] = []

    for header_match, block_lines in blocks:
        sector_code = header_match.group(1)
        ref_id = f"SA-{sector_code}-ANOM-{header_match.group(2)}"
        description = header_match.group(3).strip()

        if f"SA-{sector_code}" != expected_prefix:
            raise ParseError(
                field_path=f"anomalies[{ref_id}].anomaly_id",
                message=(
                    f"reference ID prefix SA-{sector_code} does not match "
                    f"expected {expected_prefix} for sector {sector}"
                ),
            )

        fields = _parse_field_lines(block_lines)

        anomaly_type_raw = _require_field(fields, "Anomaly type", f"anomalies[{ref_id}]")
        try:
            anomaly_type = AnomalyType(anomaly_type_raw.strip().lower())
        except ValueError:
            raise ParseError(
                field_path=f"anomalies[{ref_id}].anomaly_type",
                message=f"invalid anomaly_type value: {anomaly_type_raw!r}",
            ) from None

        tickers_raw = _require_field(fields, "Tickers", f"anomalies[{ref_id}]")
        tickers = tuple(t.strip() for t in tickers_raw.split(",") if t.strip())

        severity_raw = _require_field(fields, "Severity", f"anomalies[{ref_id}]")
        severity_normalized = severity_raw.strip().lower()
        if severity_normalized not in _VALID_SEVERITY:
            raise ParseError(
                field_path=f"anomalies[{ref_id}].severity",
                message=f"invalid severity value: {severity_raw!r}",
            )
        severity = cast(AnomalySeverity, severity_normalized)

        suggested_question = _require_field(fields, "Suggested question", f"anomalies[{ref_id}]")

        anomalies.append(
            Anomaly(
                anomaly_id=ref_id,
                description=description,
                anomaly_type=anomaly_type,
                tickers=tickers,
                severity=severity,
                suggested_question=suggested_question.strip(),
            )
        )

    return anomalies


# ---------------------------------------------------------------------------
# Thesis candidate parsing
# ---------------------------------------------------------------------------

_TC_HEADER_RE = re.compile(r"^\[SA-(TECH|FIN|ENERGY)-TC-(\d+)\]\s*$")
_CONVICTION_RE = re.compile(r"^(low|moderate|high)\s+with\s+(.+)$", re.IGNORECASE)


def _parse_thesis_candidates(section_lines: list[str], sector: Sector) -> list[ThesisCandidate]:
    """Parse THESIS CANDIDATES section into :class:`ThesisCandidate` records."""
    expected_prefix = SECTOR_PREFIX[sector]
    blocks = _split_blocks(section_lines, _TC_HEADER_RE)
    candidates: list[ThesisCandidate] = []

    for header_match, block_lines in blocks:
        sector_code = header_match.group(1)
        ref_id = f"SA-{sector_code}-TC-{header_match.group(2)}"

        if f"SA-{sector_code}" != expected_prefix:
            raise ParseError(
                field_path=f"thesis_candidates[{ref_id}].thesis_candidate_id",
                message=(
                    f"reference ID prefix SA-{sector_code} does not match "
                    f"expected {expected_prefix} for sector {sector}"
                ),
            )

        fields = _parse_field_lines(block_lines)

        ticker = _require_field(fields, "Ticker", f"thesis_candidates[{ref_id}]")

        direction_raw = _require_field(fields, "Direction", f"thesis_candidates[{ref_id}]")
        try:
            direction = Direction(direction_raw.strip().lower())
        except ValueError:
            raise ParseError(
                field_path=f"thesis_candidates[{ref_id}].direction",
                message=f"invalid direction value: {direction_raw!r}",
            ) from None

        setup_type_raw = _require_field(fields, "Setup type", f"thesis_candidates[{ref_id}]")
        try:
            setup_type = SetupType(setup_type_raw.strip().lower())
        except ValueError:
            raise ParseError(
                field_path=f"thesis_candidates[{ref_id}].setup_type",
                message=f"invalid setup_type value: {setup_type_raw!r}",
            ) from None

        catalyst = _require_field(fields, "Catalyst/driver", f"thesis_candidates[{ref_id}]")
        time_horizon = _require_field(fields, "Time horizon", f"thesis_candidates[{ref_id}]")

        conviction_raw = _require_field(fields, "Conviction sketch", f"thesis_candidates[{ref_id}]")
        conviction_match = _CONVICTION_RE.match(conviction_raw.strip())
        if not conviction_match:
            raise ParseError(
                field_path=f"thesis_candidates[{ref_id}].conviction_sketch",
                message=(
                    f"conviction sketch must follow format "
                    f"'{{low|moderate|high}} with justification'; got: {conviction_raw!r}"
                ),
            )
        try:
            conviction_sketch = ConvictionSketch(conviction_match.group(1).lower())
        except ValueError:
            raise ParseError(
                field_path=f"thesis_candidates[{ref_id}].conviction_sketch",
                message=f"invalid conviction_sketch value: {conviction_match.group(1)!r}",
            ) from None
        conviction_justification = conviction_match.group(2).strip()

        key_risk = _require_field(fields, "Key risk", f"thesis_candidates[{ref_id}]")

        candidates.append(
            ThesisCandidate(
                thesis_candidate_id=ref_id,
                ticker=ticker.strip(),
                direction=direction,
                setup_type=setup_type,
                catalyst=catalyst.strip(),
                time_horizon_hours=time_horizon.strip(),
                conviction_sketch=conviction_sketch,
                conviction_justification=conviction_justification,
                key_risk=key_risk.strip(),
            )
        )

    return candidates


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _split_blocks(
    lines: list[str], header_re: re.Pattern[str]
) -> list[tuple[re.Match[str], list[str]]]:
    """Split *lines* into (header_match, body_lines) pairs.

    Each block starts when *header_re* matches a line.  Body lines run until
    the next matching header or end of section.
    """
    blocks: list[tuple[re.Match[str], list[str]]] = []
    current_match: re.Match[str] | None = None
    current_body: list[str] = []

    for line in lines:
        m = header_re.match(line.strip())
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


def _parse_field_lines(lines: list[str]) -> dict[str, str]:
    """Parse ``Key: value`` indented field lines into a lowercase-keyed dict.

    Multi-word keys are supported (e.g. "Signal type", "Anomaly type").
    The first colon is the separator; keys are stored in lowercase so
    :func:`_require_field` can do O(1) lookups.
    """
    fields: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        colon_pos = stripped.find(":")
        if colon_pos < 0:
            continue
        key = stripped[:colon_pos].strip().lower()
        value = stripped[colon_pos + 1 :].strip()
        fields[key] = value
    return fields


def _require_field(fields: dict[str, str], key: str, context: str) -> str:
    """Return ``fields[key.lower()]`` or raise :class:`ParseError` with *context*."""
    field_slug = key.lower().replace(" ", "_").replace("/", "_")
    value = fields.get(key.lower())
    if value is None:
        raise ParseError(
            field_path=f"{context}.{field_slug}",
            message=f"missing required field {key!r}",
        )
    if not value:
        raise ParseError(
            field_path=f"{context}.{field_slug}",
            message=f"field {key!r} is present but empty",
        )
    return value
