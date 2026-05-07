"""Round-trip verification tests for the PM system prompt (ALP-352).

Mirrors ``tests/decision/strategist/test_prompt_round_trip.py``.

Acceptance criteria exercised here:

* AC1: ``prompts/decision/pm.md`` contains every required XML envelope section.
* AC2: The envelope passed to ``submit_envelope(...)`` inside ``<example_output>``
  round-trips cleanly through the ``PMEnvelope`` discriminated-union — the
  Layer-1 contract the engine-stub ``submit_envelope`` MCP wrapper enforces at
  receipt. Regression guard for ALP-352, where the prompt's prescribed shape
  diverged from ``PMEnvelope`` and every PM emission bounced off Layer-1 with
  no submission_log entry (compounded by ALP-353).
* AC3: The ``<output>`` sentinel inside ``<example_output>`` validates as a
  ``PMCompletionRecord``.
* AC4: Canonical anti-pattern strings appear verbatim in the prompt.
* AC5: Tool names ``validate_guardrail``, ``retrieve_brief``, and
  ``submit_envelope`` appear inside ``<tool_policy>``.
* AC6: ``verdict``, ``concerns``, and ``rationale_narrative`` are documented as
  top-level envelope fields in ``<output_contract>`` — pinning the structural
  fix so a future edit cannot silently re-nest them under ``evaluation``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from alphamind.decision.portfolio_manager.models import (
    PMCompletionRecord,
    PMEnvelope,
)

_REPO_ROOT = Path(__file__).parents[3]
_PROMPT_PATH = _REPO_ROOT / "prompts" / "decision" / "pm.md"

# All required XML envelope sections.
_REQUIRED_SECTIONS = [
    "role",
    "operating_context",
    "inputs",
    "task",
    "method",
    "tool_policy",
    "output_contract",
    "example_output",
    "constraints",
]

# Canonical anti-pattern strings the PM's feedback loop aggregates on.
# Source: docs/design/04-decision-layer/portfolio-manager.md § Anti-patterns
# and the ``anti_patterns_identified`` enum in pm-envelope-schema.md.
_CANONICAL_ANTIPATTERNS = [
    "conviction_inflation",
    "sunk_cost_persistence",
    "rationalized_continuation",
    "thesis_contradiction_suppression",
    "engine_originated_closure_signal",
]

# Text-mode discipline phrases incompatible with output_format=json_schema.
# See feedback memory ``feedback_prompt_output_format_compat`` — the analyst
# and strategist were each hit by this; the PM prompt must stay clear too.
_TEXT_MODE_PHRASES = [
    "Begin your response with",
    "begin your response with",
    "Do not wrap the JSON in markdown fences",
    "do not wrap the JSON in markdown fences",
    "first-token prefill",
    "Do not emit prose before, after, or within",
    "do not emit prose before, after, or within",
]

# Top-level required fields per pm-envelope-schema.md. ``verdict``, ``concerns``,
# and ``rationale_narrative`` were nested under ``evaluation`` before ALP-352;
# this test pins the corrected top-level documentation in <output_contract>.
_TOP_LEVEL_REQUIRED_FIELDS = [
    "verdict",
    "evaluation",
    "modifications",
    "concerns",
    "rationale_narrative",
    "commands",
]

_EXAMPLE_OUTPUT_RE = re.compile(r"<example_output>(.*?)</example_output>", re.DOTALL)
_OUTPUT_BLOCK_RE = re.compile(r"<output>(.*?)</output>", re.DOTALL)
_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)

_ENVELOPE_ADAPTER: TypeAdapter[PMEnvelope] = TypeAdapter(PMEnvelope)


def _read_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _extract_section(prompt: str, tag: str) -> str | None:
    """Return the content between ``<tag>`` and ``</tag>``, or None."""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", prompt, re.DOTALL)
    return m.group(1) if m else None


def _extract_submit_envelope_payload(prompt: str) -> dict[str, Any]:
    """Return the JSON object passed to the example ``submit_envelope(...)`` call.

    The prompt embeds the envelope as a literal ``submit_envelope({ ... })``
    call inside the ``<tool_call>`` block so the LLM sees an API-call
    demonstration. Extraction is scoped to the ``<tool_call>`` block — there
    are unrelated ``submit_envelope(`` literals (in ``<tool_policy>``) and
    bracketed inline JSON-ish prose elsewhere in the prompt that would mislead
    a global search. Inside the block, we find ``submit_envelope(`` then walk
    balanced braces from the next ``{`` to the matching ``}`` (respecting
    JSON string boundaries and escape sequences).
    """
    tool_call_match = _TOOL_CALL_BLOCK_RE.search(prompt)
    assert tool_call_match is not None, (
        "prompts/decision/pm.md is missing a <tool_call>...</tool_call> block "
        "inside <example_output>."
    )
    block = tool_call_match.group(1)
    needle = "submit_envelope("
    idx = block.find(needle)
    assert idx >= 0, (
        "The <tool_call> block in prompts/decision/pm.md is missing a literal "
        "`submit_envelope(...)` call."
    )
    brace_start = block.find("{", idx + len(needle))
    assert brace_start > idx, (
        "Could not find a `{` after `submit_envelope(` inside the <tool_call> block of pm.md."
    )
    depth = 0
    in_string = False
    escape = False
    end = -1
    for i in range(brace_start, len(block)):
        ch = block[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end > brace_start, (
        "Could not balance the braces of the `submit_envelope({...})` payload "
        "in the <tool_call> block of pm.md."
    )
    return dict(json.loads(block[brace_start:end]))


# ---------------------------------------------------------------------------
# Test 1: required XML sections
# ---------------------------------------------------------------------------


def test_prompt_has_all_required_sections() -> None:
    """Every required XML envelope section is present in the prompt."""
    content = _read_prompt()
    missing = [
        tag for tag in _REQUIRED_SECTIONS if f"<{tag}>" not in content or f"</{tag}>" not in content
    ]
    assert not missing, (
        f"prompts/decision/pm.md is missing required sections: {missing}.  "
        f"Each must appear as both an opening and closing XML envelope tag."
    )


# ---------------------------------------------------------------------------
# Test 2: example envelope round-trips through PMEnvelope (ALP-352 regression)
# ---------------------------------------------------------------------------


def test_example_envelope_round_trips_through_pm_envelope() -> None:
    """The prompt's example ``submit_envelope(...)`` payload validates as PMEnvelope.

    Regression guard for ALP-352: the prior shape nested ``verdict``,
    ``concerns``, and ``rationale_narrative`` inside ``evaluation``, and used
    bare strings for ``concerns`` plus a ``criteria`` map of pass/fail
    primitives. PMEnvelope rejected every such envelope at Layer-1 with
    ``Unable to extract tag using discriminator 'source_provenance'``.
    """
    prompt = _read_prompt()
    payload = _extract_submit_envelope_payload(prompt)
    envelope = _ENVELOPE_ADAPTER.validate_python(payload)

    # Spot-check the structural expectations that drove ALP-352:
    # verdict / concerns / rationale_narrative must have parsed at the top
    # level (not nested under evaluation), and modifications must carry the
    # required ``phase`` field.
    assert envelope.verdict in {"approve", "approve_with_modification", "reject"}
    assert envelope.rationale_narrative
    assert envelope.source_provenance == "pm_analyst"
    assert envelope.modifications, (
        "The example demonstrates approve_with_modification and must carry "
        "at least one modification record."
    )
    assert envelope.modifications[0].phase == "pre_submission"


# ---------------------------------------------------------------------------
# Test 3: example output sentinel validates as PMCompletionRecord
# ---------------------------------------------------------------------------


def test_example_output_validates_as_completion_record() -> None:
    """The ``<output>`` JSON inside ``<example_output>`` is a valid PMCompletionRecord."""
    prompt = _read_prompt()
    eo = _EXAMPLE_OUTPUT_RE.search(prompt)
    assert eo is not None, "prompts/decision/pm.md is missing an <example_output> block."
    body = eo.group(1)
    out = _OUTPUT_BLOCK_RE.search(body)
    assert out is not None, (
        "The <example_output> block does not contain an <output>...</output> "
        "wrapper with a raw JSON object for the PMCompletionRecord."
    )
    instance = json.loads(out.group(1).strip())
    record = PMCompletionRecord.model_validate(instance)
    total = (
        record.verdict_summary.approve
        + record.verdict_summary.approve_with_modification
        + record.verdict_summary.reject
    )
    assert total == record.envelopes_submitted


# ---------------------------------------------------------------------------
# Test 4: canonical anti-pattern strings present
# ---------------------------------------------------------------------------


def test_canonical_antipattern_strings_present() -> None:
    """Each canonical anti-pattern string appears verbatim somewhere in the prompt."""
    content = _read_prompt()
    missing = [name for name in _CANONICAL_ANTIPATTERNS if name not in content]
    assert not missing, (
        f"Canonical anti-pattern strings absent from prompts/decision/pm.md: "
        f"{missing}.  Add each name verbatim — the feedback loop aggregates "
        f"on these exact strings via the `anti_patterns_identified` field."
    )


# ---------------------------------------------------------------------------
# Test 5: tool names appear in <tool_policy>
# ---------------------------------------------------------------------------


def test_tool_names_in_tool_policy() -> None:
    """The PM tool names appear inside <tool_policy>.

    ``validate_guardrail``, ``retrieve_brief``, and ``submit_envelope`` are
    wired for the PM in agents.yaml; the policy must reference each.
    """
    content = _read_prompt()
    body = _extract_section(content, "tool_policy")
    assert body is not None, "prompts/decision/pm.md is missing a <tool_policy> section."
    for tool in ("validate_guardrail", "retrieve_brief", "submit_envelope"):
        assert tool in body, (
            f"Tool '{tool}' is not referenced in the <tool_policy> section of "
            f"prompts/decision/pm.md.  agents.yaml wires this tool for the PM; "
            f"the policy must document when and how to call it."
        )


# ---------------------------------------------------------------------------
# Test 6: prompt omits text-mode output discipline phrases
# ---------------------------------------------------------------------------


def test_prompt_omits_text_mode_output_directives() -> None:
    """The PM prompt must NOT contain text-mode output-discipline directives.

    The harness uses ``output_format={"type": "json_schema", ...}`` mode for the
    PMCompletionRecord sentinel; phrases like "begin your response with `{`" or
    "no markdown fences" contradict that mode and silently hang the model in
    extended thinking. The same hazard hit the analyst and strategist prompts
    before being fixed in PR #22.
    """
    content = _read_prompt()
    found = [phrase for phrase in _TEXT_MODE_PHRASES if phrase in content]
    assert not found, (
        f"prompts/decision/pm.md contains text-mode output-discipline phrases "
        f"that contradict the harness's `output_format=json_schema` mode: "
        f"{found}.  Remove them; the schema does the shape work."
    )


# ---------------------------------------------------------------------------
# Test 7: top-level required fields documented in <output_contract> (ALP-352)
# ---------------------------------------------------------------------------


def test_output_contract_documents_top_level_required_fields() -> None:
    """``verdict``, ``concerns``, ``rationale_narrative`` are top-level bullets.

    Regression guard for ALP-352: the prior <output_contract> nested these
    three fields inside ``evaluation:`` so they appeared as deeper bullets,
    not as top-level envelope fields. This test asserts each appears as a
    bullet at the root level of the envelope description (matched at the
    start of a line).
    """
    content = _read_prompt()
    body = _extract_section(content, "output_contract")
    assert body is not None, "prompts/decision/pm.md is missing an <output_contract> section."
    for field in _TOP_LEVEL_REQUIRED_FIELDS:
        # Top-level bullet pattern: line starts with "- `<field>` (".
        # The pre-ALP-352 nested shape would have indented these one level,
        # so the leading "- " at column 0 distinguishes top-level documentation.
        pattern = re.compile(rf"^- `{field}` \(", re.MULTILINE)
        assert pattern.search(body) is not None, (
            f"<output_contract> in prompts/decision/pm.md does not document "
            f"`{field}` as a top-level envelope field. The canonical shape per "
            f"pm-envelope-schema.md places this field at the envelope root."
        )
