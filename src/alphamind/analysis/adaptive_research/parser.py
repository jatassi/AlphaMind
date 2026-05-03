"""Markdown brief parser for adaptive-researcher agent — story 03b (ALP-257).

Output contract (verbatim from prompts/analysis/adaptive_researcher.md
§ output_contract):

    ADAPTIVE RESEARCH FINDINGS
    Invocation: {invocation_id}
    Threads investigated: {N} of {M} anomalies triaged
    Anomalies deferred: {anomaly refs not investigated this cycle, or "none"}

    === INVESTIGATION THREADS ===
    [AR-1]
      Trigger: {reference to originating anomaly}
      Question: {the specific research question investigated}
      Tickers: {affected tickers}
      Sector: {primary sector}
      Tools used: {list of tool IDs called during investigation}
      Findings:
        - {factual finding with source attribution}
      Assessment: {signal | noise | inconclusive}
      Confidence: {high | moderate | low}
      If signal:
        Implication: {1-2 sentences}
        Strengthens: {refs or "none"}
        Weakens:     {refs or "none"}
      If noise:
        Dismissal reason: {1 sentence}
      If inconclusive:
        Missing: {what data would resolve this}

Sequential indexing on AR-N starting at 1; an empty
``=== INVESTIGATION THREADS ===`` section is valid (zero threads).

Public names
------------
- :exc:`ParseError`
- :func:`parse_adaptive_brief`
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from alphamind.analysis._shared import Sector
from alphamind.analysis.adaptive_research.models import (
    AdaptiveBrief,
    Assessment,
    Confidence,
    InvestigationThread,
)

__all__ = ["ParseError", "parse_adaptive_brief"]


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^```\w*\s*$")
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")
_THREAD_HEADER_RE = re.compile(r"^\[AR-(\d+)\]\s*$")
_HEADER_COUNTS_RE = re.compile(
    r"^Threads investigated:\s*(\d+)\s+of\s+(\d+)\s+anomalies triaged\s*$"
)
_INVESTIGATION_THREADS_MARKER = "=== INVESTIGATION THREADS ==="


# ---------------------------------------------------------------------------
# ParseError
# ---------------------------------------------------------------------------


class ParseError(Exception):
    """Raised when an adaptive-researcher brief cannot be structurally parsed.

    Attributes mirror the qualitative-researcher and domain-researcher
    ``ParseError`` shape so the harness's corrective-retry message construction
    sees a uniform error shape across text-format agents.
    """

    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_adaptive_brief(raw_response: str, *, invocation_id: str) -> AdaptiveBrief:
    """Parse *raw_response* into an :class:`AdaptiveBrief`.

    Parameters
    ----------
    raw_response:
        Raw text response from the adaptive researcher agent. Leading and
        trailing whitespace is tolerated. A single surrounding markdown code
        fence (bare ``` or ``text-tagged) is stripped before parsing.
    invocation_id:
        Canonical invocation identifier supplied by the harness. The brief's
        own ``Invocation:`` header is parsed and cross-checked; a mismatch
        raises :class:`ParseError`.

    Raises
    ------
    ParseError
        On any structural malformation: missing sections, unknown enum values,
        invocation_id mismatch, out-of-sequence thread IDs, missing required
        conditional fields, or model-invariant violation.
    """
    text = raw_response.strip()
    text = _strip_code_fence(text)
    lines = text.splitlines()

    threads_investigated_count, anomalies_triaged_count, anomalies_deferred, marker_idx = (
        _parse_header(lines, invocation_id)
    )
    threads = _parse_threads(lines[marker_idx + 1 :])

    try:
        return AdaptiveBrief(
            invocation_id=invocation_id,
            threads_investigated_count=threads_investigated_count,
            anomalies_triaged_count=anomalies_triaged_count,
            anomalies_deferred=anomalies_deferred,
            threads=tuple(threads),
        )
    except ValidationError as exc:
        raise ParseError(field_path="header", message=str(exc)) from exc


# ---------------------------------------------------------------------------
# Fence stripping
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """Strip a single surrounding markdown code fence if present."""
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


def _parse_header(
    lines: list[str], expected_invocation_id: str
) -> tuple[int, int, tuple[str, ...], int]:
    """Parse the four-line header and locate the INVESTIGATION THREADS marker.

    Returns ``(threads_investigated_count, anomalies_triaged_count,
    anomalies_deferred, marker_line_index)``.
    """
    i = _expect_literal(
        lines, 0, "ADAPTIVE RESEARCH FINDINGS", "envelope", "ADAPTIVE RESEARCH FINDINGS"
    )
    invocation_value, i = _expect_prefix(lines, i, "Invocation:", "header.invocation_id")
    if invocation_value != expected_invocation_id:
        raise ParseError(
            field_path="header.invocation_id",
            message=(
                f"Invocation header {invocation_value!r} does not match "
                f"caller-supplied invocation_id {expected_invocation_id!r}"
            ),
        )

    threads_investigated_count, anomalies_triaged_count, i = _expect_counts_line(lines, i)

    deferred_value, i = _expect_prefix(lines, i, "Anomalies deferred:", "header.anomalies_deferred")
    anomalies_deferred = _parse_deferred_value(deferred_value)

    marker_idx = _skip_blanks(lines, i)
    if marker_idx >= len(lines) or lines[marker_idx].strip() != _INVESTIGATION_THREADS_MARKER:
        raise ParseError(
            field_path="header.investigation_threads_marker",
            message=f"missing required section marker {_INVESTIGATION_THREADS_MARKER!r}",
        )
    return threads_investigated_count, anomalies_triaged_count, anomalies_deferred, marker_idx


def _skip_blanks(lines: list[str], start: int) -> int:
    """Return the index of the first non-blank line at or after ``start``."""
    n = len(lines)
    i = start
    while i < n and not lines[i].strip():
        i += 1
    return i


def _expect_literal(lines: list[str], start: int, literal: str, field_path: str, what: str) -> int:
    """Skip blanks, require ``lines[i].strip() == literal``, return next index."""
    i = _skip_blanks(lines, start)
    if i >= len(lines) or lines[i].strip() != literal:
        raise ParseError(
            field_path=field_path,
            message=f"missing required line {what!r}",
        )
    return i + 1


def _expect_prefix(lines: list[str], start: int, prefix: str, field_path: str) -> tuple[str, int]:
    """Skip blanks, require ``lines[i].strip().startswith(prefix)``, return
    (value-after-prefix, next index)."""
    i = _skip_blanks(lines, start)
    if i >= len(lines) or not lines[i].strip().startswith(prefix):
        raise ParseError(
            field_path=field_path,
            message=f"missing {prefix!r} line",
        )
    value = lines[i].strip().removeprefix(prefix).strip()
    return value, i + 1


def _expect_counts_line(lines: list[str], start: int) -> tuple[int, int, int]:
    """Parse the ``Threads investigated: N of M anomalies triaged`` line.

    Returns ``(threads_investigated_count, anomalies_triaged_count, next_index)``.
    """
    i = _skip_blanks(lines, start)
    if i >= len(lines):
        raise ParseError(
            field_path="header.threads_investigated_count",
            message="missing 'Threads investigated:' line",
        )
    counts_match = _HEADER_COUNTS_RE.match(lines[i].strip())
    if counts_match is None:
        raise ParseError(
            field_path="header.threads_investigated_count",
            message=(
                "expected 'Threads investigated: {N} of {M} anomalies triaged'; "
                f"got {lines[i].strip()!r}"
            ),
        )
    return int(counts_match.group(1)), int(counts_match.group(2)), i + 1


def _parse_deferred_value(value: str) -> tuple[str, ...]:
    """Parse the `Anomalies deferred:` value.

    The literal string ``none`` (case-insensitive) yields ``()``.
    Anything else is split on commas and whitespace-stripped per element.
    """
    if value.lower() == "none":
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


# ---------------------------------------------------------------------------
# Thread parsing
# ---------------------------------------------------------------------------


def _parse_threads(section_lines: list[str]) -> list[InvestigationThread]:
    """Parse the body of the INVESTIGATION THREADS section into a list."""
    blocks = _split_thread_blocks(section_lines)
    threads: list[InvestigationThread] = []
    for index, (header_match, block_lines) in enumerate(blocks):
        thread_num = int(header_match.group(1))
        expected_num = index + 1
        if thread_num != expected_num:
            raise ParseError(
                field_path=f"threads[{index}].thread_id",
                message=(
                    f"expected sequential thread id 'AR-{expected_num}'; got 'AR-{thread_num}'"
                ),
            )
        threads.append(_parse_single_thread(index, f"AR-{thread_num}", block_lines))
    return threads


def _split_thread_blocks(
    lines: list[str],
) -> list[tuple[re.Match[str], list[str]]]:
    """Split section lines into (header_match, body_lines) pairs."""
    blocks: list[tuple[re.Match[str], list[str]]] = []
    current_match: re.Match[str] | None = None
    current_body: list[str] = []
    for line in lines:
        m = _THREAD_HEADER_RE.match(line.strip())
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


def _parse_single_thread(index: int, thread_id: str, body_lines: list[str]) -> InvestigationThread:
    """Parse a single ``[AR-N]`` block into an :class:`InvestigationThread`.

    The five conditional fields are populated branchwise on ``Assessment``:

    * required-by-assessment fields raise :class:`ParseError` if missing;
    * forbidden-by-assessment fields, if present in the wire input, are
      passed through unchanged so the model's ``_assessment_invariant``
      rejects them — ParseError surfaces from the ValidationError handler.
    """
    field_prefix = f"threads[{index}]"
    fields, findings = _parse_thread_body(body_lines)

    assessment = _parse_assessment(_require(fields, "assessment", field_prefix), field_prefix)

    implication: str | None
    strengthens: tuple[str, ...] | None
    weakens: tuple[str, ...] | None
    dismissal_reason: str | None
    missing: str | None

    if assessment is Assessment.SIGNAL:
        implication = _require(fields, "implication", field_prefix)
        strengthens = _parse_csv_or_none(_require(fields, "strengthens", field_prefix))
        weakens = _parse_csv_or_none(_require(fields, "weakens", field_prefix))
        dismissal_reason = fields.get("dismissal reason")
        missing = fields.get("missing")
    else:
        # noise / inconclusive: only one required field; signal-only fields
        # passed through (if present) so the model invariant rejects.
        implication = fields.get("implication")
        strengthens = _parse_csv_or_none(fields["strengthens"]) if "strengthens" in fields else None
        weakens = _parse_csv_or_none(fields["weakens"]) if "weakens" in fields else None
        if assessment is Assessment.NOISE:
            dismissal_reason = _require(fields, "dismissal reason", field_prefix)
            missing = fields.get("missing")
        else:  # INCONCLUSIVE
            dismissal_reason = fields.get("dismissal reason")
            missing = _require(fields, "missing", field_prefix)

    try:
        return InvestigationThread(
            thread_id=thread_id,
            trigger=_require(fields, "trigger", field_prefix),
            question=_require(fields, "question", field_prefix),
            tickers=_parse_csv_or_none(fields.get("tickers", "")),
            sector=_parse_sector(_require(fields, "sector", field_prefix), field_prefix),
            tools_used=_parse_csv_or_none(fields.get("tools used", "")),
            findings=findings,
            assessment=assessment,
            confidence=_parse_confidence(
                _require(fields, "confidence", field_prefix), field_prefix
            ),
            implication=implication,
            strengthens=strengthens,
            weakens=weakens,
            dismissal_reason=dismissal_reason,
            missing=missing,
        )
    except ValidationError as exc:
        raise ParseError(field_path=field_prefix, message=str(exc)) from exc


def _parse_thread_body(body_lines: list[str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse the body of a thread block into (fields, findings).

    Field lines are ``  Key: value`` (lower-cased key for lookup); the
    ``Findings:`` line opens a sub-block of ``    - text`` lines that runs
    until the next field line.
    """
    fields: dict[str, str] = {}
    findings: list[str] = []
    in_findings = False

    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            continue

        if in_findings and stripped.startswith("-"):
            findings.append(stripped.lstrip("-").strip())
            continue
        in_findings = False

        if ":" not in stripped:
            continue
        key_raw, _, value = stripped.partition(":")
        key = key_raw.strip().lower()
        value = value.strip()
        if key == "findings":
            in_findings = True
            continue
        fields[key] = value

    return fields, tuple(findings)


# ---------------------------------------------------------------------------
# Field-value helpers
# ---------------------------------------------------------------------------


def _require(fields: dict[str, str], key: str, context: str) -> str:
    """Return ``fields[key]`` or raise :class:`ParseError`.

    ``key`` is already lower-cased to match :func:`_parse_thread_body` storage.
    """
    field_slug = key.replace(" ", "_")
    if key not in fields:
        raise ParseError(
            field_path=f"{context}.{field_slug}",
            message=f"missing required field {key!r}",
        )
    value = fields[key]
    if not value:
        raise ParseError(
            field_path=f"{context}.{field_slug}",
            message=f"field {key!r} is present but empty",
        )
    return value


def _parse_csv_or_none(value: str) -> tuple[str, ...]:
    """Parse a comma-separated value with ``none`` → ``()`` semantics.

    An empty string also yields ``()`` (LLMs sometimes emit ``Tickers:`` with
    no content for "no tickers"). The literal ``none`` is case-insensitive.
    """
    stripped = value.strip()
    if not stripped or stripped.lower() == "none":
        return ()
    return tuple(part.strip() for part in stripped.split(",") if part.strip())


def _parse_sector(value: str, context: str) -> Sector:
    """Parse a Sector enum value or raise :class:`ParseError`."""
    try:
        return Sector(value.strip().lower())
    except ValueError:
        raise ParseError(
            field_path=f"{context}.sector",
            message=f"invalid sector value: {value!r}",
        ) from None


def _parse_assessment(value: str, context: str) -> Assessment:
    """Parse an Assessment enum value or raise :class:`ParseError`."""
    try:
        return Assessment(value.strip().lower())
    except ValueError:
        raise ParseError(
            field_path=f"{context}.assessment",
            message=f"invalid assessment value: {value!r}",
        ) from None


def _parse_confidence(value: str, context: str) -> Confidence:
    """Parse a Confidence enum value or raise :class:`ParseError`."""
    try:
        return Confidence(value.strip().lower())
    except ValueError:
        raise ParseError(
            field_path=f"{context}.confidence",
            message=f"invalid confidence value: {value!r}",
        ) from None
