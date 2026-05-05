"""Round-trip verification tests for the strategist system prompt (ALP-302).

Mirrors the shape of ``tests/decision/analyst/test_prompt_round_trip.py``.

Acceptance criteria exercised here:

* AC1: ``prompts/decision/strategist.md`` contains all nine required XML
  envelope sections.
* AC2: The ``<example_output>`` JSON validates against the strategist output
  schema (Draft 2020-12).
* AC3: Canonical anti-pattern strings appear verbatim in the prompt.
* AC4: Tool names ``validate_guardrail`` and ``retrieve_brief`` appear inside
  the ``<tool_policy>`` section.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema

_REPO_ROOT = Path(__file__).parents[3]
_PROMPT_PATH = _REPO_ROOT / "prompts" / "decision" / "strategist.md"
_SCHEMA_PATH = _REPO_ROOT / "docs" / "design" / "04-decision-layer" / "strategist-output-schema.md"

# All nine required XML envelope sections.
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
# and docs/design/04-decision-layer/strategist.md § LLM failure mode avoidance.
_CANONICAL_ANTIPATTERNS = [
    "sunk_cost_persistence",
    "rationalized_continuation",
    "thesis_contradiction_suppression",
    "engine_originated_closure_signal",
    "generic_rationale",
]

_JSON_FENCE_RE = re.compile(r"```json(.*?)```", re.DOTALL)
_EXAMPLE_OUTPUT_RE = re.compile(r"<example_output>(.*?)</example_output>", re.DOTALL)
_OUTPUT_BLOCK_RE = re.compile(r"<output>(.*?)</output>", re.DOTALL)


def _read_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _read_schema_md() -> str:
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def _extract_section(prompt: str, tag: str) -> str | None:
    """Return the content between ``<tag>`` and ``</tag>``, or None."""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", prompt, re.DOTALL)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Test 1: All required sections are present
# ---------------------------------------------------------------------------


def test_prompt_has_all_required_sections() -> None:
    """Every required XML envelope section is present in the prompt.

    AC1: nine structurally required sections (role, operating_context,
    inputs, task, method, tool_policy, output_contract, example_output,
    constraints) must all be present.
    """
    content = _read_prompt()
    missing = [
        tag for tag in _REQUIRED_SECTIONS if f"<{tag}>" not in content or f"</{tag}>" not in content
    ]
    assert not missing, (
        f"prompts/decision/strategist.md is missing the following required "
        f"sections: {missing}.  Each must appear as an opening and closing XML "
        f"envelope tag (e.g., <role>...</role>)."
    )


# ---------------------------------------------------------------------------
# Test 2: example_output validates against the schema
# ---------------------------------------------------------------------------


def test_example_output_validates_against_schema() -> None:
    """The JSON in ``<example_output>`` validates against the Draft 2020-12 schema.

    AC2: schema validation ensures the example is internally consistent with
    the output contract — required fields, enum values, allOf conditionals.
    """
    prompt = _read_prompt()
    schema_md = _read_schema_md()

    # Extract schema from the markdown's JSON fence.
    schema_match = _JSON_FENCE_RE.search(schema_md)
    assert schema_match is not None, (
        "strategist-output-schema.md does not contain a ```json...``` fenced block."
    )
    schema = json.loads(schema_match.group(1))

    # Extract example_output block.
    eo_match = _EXAMPLE_OUTPUT_RE.search(prompt)
    assert eo_match is not None, (
        "prompts/decision/strategist.md is missing an <example_output> block."
    )
    eo_body = eo_match.group(1)

    # Extract the raw JSON from the <output> tag inside <example_output>.
    out_match = _OUTPUT_BLOCK_RE.search(eo_body)
    assert out_match is not None, (
        "The <example_output> block does not contain an <output>...</output> "
        "wrapper with a raw JSON object."
    )
    instance = json.loads(out_match.group(1).strip())

    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, (
        "The <example_output> JSON does not validate against the strategist "
        "output schema (Draft 2020-12).  Errors:\n"
        + "\n".join(f"  [{list(e.path)}] {e.message}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Test 3: Canonical anti-pattern strings appear verbatim
# ---------------------------------------------------------------------------


def test_canonical_antipattern_strings_present() -> None:
    """Each canonical anti-pattern string appears verbatim in the prompt.

    AC3: the PM's feedback loop aggregates on these exact strings; if the
    strategist prompt names a pattern differently the aggregation breaks.
    """
    content = _read_prompt()
    missing = [name for name in _CANONICAL_ANTIPATTERNS if name not in content]
    assert not missing, (
        f"The following canonical anti-pattern strings are absent from "
        f"prompts/decision/strategist.md: {missing}.  "
        f"Add each name verbatim — in the <constraints> or <output_contract> "
        f"section — so the PM feedback loop can aggregate on them."
    )


# ---------------------------------------------------------------------------
# Test 4: Tool names appear in <tool_policy>
# ---------------------------------------------------------------------------


def test_tool_names_in_tool_policy() -> None:
    """Both ``validate_guardrail`` and ``retrieve_brief`` appear in <tool_policy>.

    AC4: agents.yaml wires these two tools for the strategist; the prompt's
    tool_policy section must reference them by exact name.
    """
    content = _read_prompt()
    tool_policy_body = _extract_section(content, "tool_policy")
    assert tool_policy_body is not None, (
        "prompts/decision/strategist.md is missing a <tool_policy> section."
    )
    for tool in ("validate_guardrail", "retrieve_brief"):
        assert tool in tool_policy_body, (
            f"Tool '{tool}' is not referenced in the <tool_policy> section of "
            f"prompts/decision/strategist.md.  "
            f"agents.yaml lists this tool for the strategist; the policy must "
            f"document when and how to call it."
        )
