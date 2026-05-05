"""Structural verification tests for the PM system prompt (ALP-322).

Mirrors the shape of ``tests/decision/strategist/test_prompt_round_trip.py``.

Acceptance criteria exercised here:

* AC1: All eight required XML envelope sections are present.
* AC2: The prefill directives "Begin your response with `{`" and
  "Do not wrap the JSON in markdown fences" are absent.
* AC3: ``<task>`` section names submit_envelope tool and completion-sentinel.
* AC4: ``<output_contract>`` names ``PMCompletionRecord`` with four fields.
* AC5: ``<tool_policy>`` submit_envelope block uses "envelope" as unit.
* AC6: ``<example_output>`` shows sentinel shape and inline tool-call example.
* AC7: ``<constraints>`` retains all required rules and canonical anti-pattern strings.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[2]
_PROMPT_PATH = _REPO_ROOT / "prompts" / "decision" / "pm.md"

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

_CANONICAL_ANTIPATTERNS = [
    "conviction_inflation",
    "sunk_cost_persistence",
    "rationalized_continuation",
    "thesis_contradiction_suppression",
    "engine_originated_closure_signal",
]


def _read_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _extract_section(prompt: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", prompt, re.DOTALL)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# AC1: All required sections are present
# ---------------------------------------------------------------------------


def test_prompt_has_all_required_sections() -> None:
    """Every required XML envelope section is present in the prompt."""
    content = _read_prompt()
    missing = [
        tag for tag in _REQUIRED_SECTIONS if f"<{tag}>" not in content or f"</{tag}>" not in content
    ]
    assert not missing, (
        f"prompts/decision/pm.md is missing the following required sections: {missing}. "
        "Each must appear as an opening and closing XML envelope tag."
    )


# ---------------------------------------------------------------------------
# AC2: Prefill directives are absent
# ---------------------------------------------------------------------------


def test_prompt_omits_prefill_directives() -> None:
    """Prompt must NOT contain prefill directives (incompatible with JSON-Schema output mode)."""
    content = _read_prompt()
    banned = [
        "Begin your response with `{`",
        "Do not wrap the JSON in markdown fences",
    ]
    found = [phrase for phrase in banned if phrase in content]
    assert not found, (
        f"prompts/decision/pm.md contains banned prefill directives: {found}. "
        "These are incompatible with output_format=json_schema mode and must be removed."
    )


# ---------------------------------------------------------------------------
# AC3: <task> names submit_envelope and sentinel emission
# ---------------------------------------------------------------------------


def test_task_names_submit_envelope_and_sentinel() -> None:
    """<task> must reference submit_envelope tool and completion-sentinel emission."""
    content = _read_prompt()
    task_body = _extract_section(content, "task")
    assert task_body is not None, "prompts/decision/pm.md is missing a <task> section."
    assert "submit_envelope" in task_body, "<task> section must reference the submit_envelope tool."
    assert "sentinel" in task_body.lower() or "PMCompletionRecord" in task_body, (
        "<task> section must describe the completion-sentinel emission."
    )


# ---------------------------------------------------------------------------
# AC4: <output_contract> names PMCompletionRecord with four fields
# ---------------------------------------------------------------------------


def test_output_contract_names_pm_completion_record() -> None:
    """<output_contract> must name PMCompletionRecord."""
    content = _read_prompt()
    oc_body = _extract_section(content, "output_contract")
    assert oc_body is not None, "prompts/decision/pm.md is missing an <output_contract> section."
    assert "PMCompletionRecord" in oc_body, "<output_contract> must name PMCompletionRecord."


def test_output_contract_has_four_sentinel_fields() -> None:
    """<output_contract> must name all four PMCompletionRecord fields."""
    content = _read_prompt()
    oc_body = _extract_section(content, "output_contract")
    assert oc_body is not None, "prompts/decision/pm.md is missing an <output_contract> section."
    required_fields = ["invocation_id", "timestamp", "envelopes_submitted", "verdict_summary"]
    missing = [f for f in required_fields if f not in oc_body]
    assert not missing, f"<output_contract> is missing sentinel fields: {missing}."


# ---------------------------------------------------------------------------
# AC5: <tool_policy> submit_envelope block uses "envelope" not "command"
# ---------------------------------------------------------------------------


def test_tool_policy_submit_envelope_uses_envelope_unit() -> None:
    """<tool_policy>'s submit_envelope section must use 'envelope' as the unit of submission."""
    content = _read_prompt()
    tp_body = _extract_section(content, "tool_policy")
    assert tp_body is not None, "prompts/decision/pm.md is missing a <tool_policy> section."
    assert "submit_envelope" in tp_body, "<tool_policy> must reference submit_envelope."
    # The old text said "Submit commands one at a time" — verify that's gone
    assert "Submit commands one at a time" not in tp_body, (
        "<tool_policy> still contains the old 'Submit commands one at a time' text. "
        "Update to use envelope as the unit of submission."
    )
    # The new text should reference envelope submission
    assert "envelope" in tp_body.lower(), (
        "<tool_policy> must use 'envelope' as the unit of submission in the submit_envelope block."
    )


# ---------------------------------------------------------------------------
# AC6: <example_output> shows sentinel shape and inline tool-call example
# ---------------------------------------------------------------------------


def test_example_output_shows_sentinel_shape() -> None:
    """<example_output> must reference PMCompletionRecord sentinel fields."""
    content = _read_prompt()
    eo_body = _extract_section(content, "example_output")
    assert eo_body is not None, "prompts/decision/pm.md is missing an <example_output> section."
    assert "envelopes_submitted" in eo_body, (
        "<example_output> must show the sentinel shape with envelopes_submitted field."
    )
    assert "verdict_summary" in eo_body, (
        "<example_output> must show the sentinel shape with verdict_summary field."
    )


def test_example_output_has_inline_tool_call_example() -> None:
    """<example_output> must contain an inline tool-call example showing submit_envelope usage."""
    content = _read_prompt()
    eo_body = _extract_section(content, "example_output")
    assert eo_body is not None, "prompts/decision/pm.md is missing an <example_output> section."
    assert "submit_envelope" in eo_body, (
        "<example_output> must contain an inline tool-call example showing submit_envelope."
    )


# ---------------------------------------------------------------------------
# AC7: <constraints> retains required rules and anti-pattern strings
# ---------------------------------------------------------------------------


def test_constraints_retains_halt_mode_rule() -> None:
    """<constraints> must retain the halt-mode constraint."""
    content = _read_prompt()
    c_body = _extract_section(content, "constraints")
    assert c_body is not None, "prompts/decision/pm.md is missing a <constraints> section."
    assert "HALT" in c_body, "<constraints> must retain the halt-mode constraint (HALT)."
    assert "OPEN" in c_body and "ADD" in c_body, (
        "<constraints> must name OPEN and ADD as disallowed in halt mode."
    )


def test_constraints_retains_no_thesis_rewriting_rule() -> None:
    """<constraints> must retain the no-thesis-rewriting constraint."""
    content = _read_prompt()
    c_body = _extract_section(content, "constraints")
    assert c_body is not None, "prompts/decision/pm.md is missing a <constraints> section."
    assert "Do not rewrite" in c_body or "not rewrite" in c_body.lower(), (
        "<constraints> must retain the no-thesis-rewriting constraint."
    )


def test_constraints_retains_no_action_replacement_rule() -> None:
    """<constraints> must retain the no-action-type-replacement constraint."""
    content = _read_prompt()
    c_body = _extract_section(content, "constraints")
    assert c_body is not None, "prompts/decision/pm.md is missing a <constraints> section."
    assert "action type" in c_body.lower() or "action-type" in c_body.lower(), (
        "<constraints> must retain the no-action-type-replacement constraint."
    )


def test_constraints_retains_no_inventing_references_rule() -> None:
    """<constraints> must retain the no-inventing-reference-IDs constraint."""
    content = _read_prompt()
    c_body = _extract_section(content, "constraints")
    assert c_body is not None, "prompts/decision/pm.md is missing a <constraints> section."
    assert "invent" in c_body.lower(), (
        "<constraints> must retain the no-inventing-reference-IDs constraint."
    )


def test_constraints_retains_canonical_antipattern_strings() -> None:
    """Each canonical anti-pattern string must appear verbatim in the prompt."""
    content = _read_prompt()
    missing = [name for name in _CANONICAL_ANTIPATTERNS if name not in content]
    assert not missing, (
        f"The following canonical anti-pattern strings are absent from "
        f"prompts/decision/pm.md: {missing}. "
        "Add each name verbatim in the <constraints> section."
    )
