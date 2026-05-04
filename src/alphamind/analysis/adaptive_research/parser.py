"""JSON-payload coercion for adaptive-researcher agent — ALP-288 (B).

Post-migration the SDK delivers the adaptive brief as a dict on
``ResultMessage.structured_output`` (``output_format = {"type": "json_schema",
"schema": ...}`` mode); this module replaces the legacy text-format parser.

The parser now reduces to:

1. A pre-validate normalization pass that strips trailing parenthetical
   commentary from ``tools_used`` entries and extracts bracketed reference
   IDs from ``strengthens``/``weakens``. Sonnet still emits these wire habits
   despite the schema only declaring ``array<string>``; the validator's
   tools_used-allowlist and reference-resolution checks would otherwise
   reject the brief on these benign drifts.
2. Inject the canonical ``invocation_id`` (overrides whatever the model emits).
3. ``AdaptiveBrief.model_validate(payload)``.
4. Wrap any :class:`pydantic.ValidationError` as :class:`ParseError` to
   preserve the harness's corrective-retry contract — the harness's first-
   error → retry-message construction reads ``field_path``/``message`` from
   the same exception shape both this and the qualitative/domain parsers
   raise.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from alphamind.analysis.adaptive_research.models import AdaptiveBrief

__all__ = ["ParseError", "parse_adaptive_brief"]


_REFERENCE_RE = re.compile(r"\[([^\[\]]+)\]")


class ParseError(Exception):
    """Raised when the structured payload cannot be coerced to an :class:`AdaptiveBrief`.

    Mirrors the qualitative-researcher and domain-researcher ``ParseError`` shape
    so the harness's corrective-retry message construction sees a uniform error
    shape across all three analysis-layer agents.
    """

    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message


def parse_adaptive_brief(payload: Any, *, invocation_id: str) -> AdaptiveBrief:
    """Coerce *payload* (the SDK's ``structured_output``) to an :class:`AdaptiveBrief`.

    Parameters
    ----------
    payload:
        The dict returned by ``ResultMessage.structured_output``. Shape is
        API-enforced against ``AdaptiveBrief.model_json_schema()`` (with the
        conditional-field tightener applied); only Layer-2/3 invariants in
        Pydantic remain to fail.
    invocation_id:
        Canonical invocation identifier supplied by the harness. Always wins
        over the value (if any) in *payload*.

    Raises
    ------
    ParseError
        If *payload* is ``None``, not a dict, fails Pydantic validation, or
        violates a model invariant.
    """
    if payload is None:
        raise ParseError(
            field_path="envelope",
            message="ResultMessage.structured_output was not populated",
        )
    if not isinstance(payload, dict):
        raise ParseError(
            field_path="envelope",
            message=f"expected dict payload, got {type(payload).__name__}",
        )

    normalized = _normalize_payload(dict(payload))
    normalized["invocation_id"] = invocation_id

    try:
        return AdaptiveBrief.model_validate(normalized)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = first["loc"]
            field_path = ".".join(str(p) for p in loc) or "payload"
            message = first["msg"]
        else:
            field_path = "payload"
            message = str(exc)
        raise ParseError(field_path=field_path, message=message) from exc


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip parens/bracket-ref commentary from each thread's wire-shape fields.

    Sonnet still emits ``tools_used`` entries with parenthetical commentary
    (``"news_search (NVDA)"``) and ``strengthens``/``weakens`` entries with
    bracket+free-text suffixes (``"[SA-TECH-1] (rationale)"``) under load —
    behaviors the legacy text parser tolerated and the validator's
    tools-allowlist + reference-resolution checks otherwise reject. Lift the
    normalization here so the JSON path inherits the same tolerance.
    """
    threads = payload.get("threads")
    if not isinstance(threads, list):
        return payload
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        if isinstance(thread.get("tools_used"), list):
            thread["tools_used"] = _normalize_tool_names(thread["tools_used"])
        for ref_field in ("strengthens", "weakens"):
            value = thread.get(ref_field)
            if isinstance(value, list):
                thread[ref_field] = _normalize_references(value)
    return payload


def _normalize_tool_names(values: list[Any]) -> list[str]:
    """Strip trailing ``(...)`` commentary; de-duplicate preserved-order entries.

    The validator's distinct-within-thread + tools-used-in-allowlist checks
    treat ``tools_used`` as the descriptive list of which tools the thread
    invoked, not a call-count log; collapsing repeated entries with different
    parenthetical targets is sound.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        s = raw.strip()
        paren_idx = s.find("(")
        if paren_idx >= 0:
            s = s[:paren_idx].strip()
        if s and s not in seen:
            out.append(s)
            seen.add(s)
    return out


def _normalize_references(values: list[Any]) -> list[str]:
    """Extract bracketed reference IDs; pass bare IDs through unchanged.

    Three wire shapes the model produces under load:
      * ``"SA-TECH-1"``                 — bare ID, pass through.
      * ``"[SA-TECH-1]"``               — bracketed wire form, strip brackets.
      * ``"[SA-TECH-1] (rationale)"``   — bracketed + free-text, strip both.
    """
    out: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        matches = _REFERENCE_RE.findall(raw)
        if matches:
            out.extend(matches)
            continue
        stripped = raw.strip()
        if stripped:
            out.append(stripped)
    return out
